"""交易通道真值源适配 (P0-3): generated/traders/<id>/rtdata.json

为什么需要 (2026-09-15 源码复核):
  框架 CTA 策略侧读取到的持仓/资金**全是信号口径** —— stra_set_position
  只写信号 (CtaStraBaseCtx::append_signal, CtaStraBaseCtx.cpp:1354),
  下一个 tick 由 on_tick 消费信号并直接改写策略持仓簿 _pos_map 与累计
  手续费 (:811 → do_set_position, 费用累加 :1469), 与券商成交回报无关;
  stra_get_position/avgpx/fund_data(3) 都读这本账 (:1826/:1869/:1711)。
  因此"执行单元卡死但策略仓已达标"的幽灵仓位在框架层不可见
  (2026-09-09 实测: MA 幽灵加仓 4 手、p 幽灵 32 手)。

  交易通道 (TraderAdapter) 落盘的 rtdata.json 是独立真值源:
    {"positions": [{"code": "SHFE.ag.2609",
                    "long":  {"newvol","newavail","prevol","preavail"},
                    "short": {...}}, ...],
     "funds": {"CNY": {"balance","available","margin","fee",...}}}
  刷新时机: 每根K线收盘 (WtCtaEngine::on_calculate 末尾 refresh_funds)
  + 每笔成交回报后 queryAccount → 秒级; 2026-09-14 生产 simnow 实测
  文件在持续更新。

设计约束: 只读; 文件缺失/解析失败一律降级 None (调用方回退框架口径);
不依赖 wtpy/monitor; 按 (mtime, size) 缓存, 供 tick 级高频调用。
"""
import json
import os


TRUTH_ENV = "WT_TRUTH_JSON"
DEFAULT_MAX_AGE = 1800.0        # 秒; 超过视为陈旧 (仅告警/保守处理)


def trader_ids(config_path: str) -> list:
    """从 tdtraders.yaml 读 trader id 列表 (无 yaml/无文件 → [])"""
    try:
        import yaml
        with open(config_path, encoding='utf-8') as f:
            raw = yaml.safe_load(f) or {}
    except Exception:
        return []
    out = []
    for t in (raw.get('traders') or []):
        if isinstance(t, dict) and t.get('id'):
            out.append(str(t['id']))
    return out


def resolve_truth_path(group_dir: str, trader_cfg: str = "tdtraders.yaml",
                       enabled: str = "", env: dict = None) -> str:
    """定位真值文件路径; 无法定位/显式关闭 → ""。

    enabled 取值: "off"/"0"/"none" 关闭; 非空其他值=显式路径;
    空=自动探测 <group_dir>/generated/traders/<id>/rtdata.json。
    环境变量 WT_TRUTH_JSON 覆盖 (部署侧应急开关)。
    """
    env = os.environ if env is None else env
    if str(enabled).strip().lower() in ("off", "0", "none", "disable"):
        return ""
    explicit = str(enabled).strip() or env.get(TRUTH_ENV, "").strip()
    if explicit:
        p = os.path.expanduser(explicit)
        return p if os.path.isfile(p) else ""
    cfg = trader_cfg if os.path.isabs(trader_cfg) \
        else os.path.join(group_dir, trader_cfg)
    for tid in trader_ids(cfg):
        p = os.path.join(group_dir, "generated", "traders", tid,
                         "rtdata.json")
        if os.path.isfile(p):
            return p
    return ""


def load_rtdata(path: str) -> dict:
    """读取通道快照; 失败返回 {} (零崩溃)"""
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _vol(item) -> float:
    """通道持仓条目 → 总量 (当日新开 + 昨仓); 非法值按 0"""
    if not isinstance(item, dict):
        return 0.0
    tot = 0.0
    for k in ("newvol", "prevol"):
        v = item.get(k)
        if isinstance(v, (int, float)) and v == v:
            tot += float(v)
    return tot


def net_position(data: dict, std_code: str) -> float:
    """通道真值的净持仓 (多-空); 未匹配到该品种 → 0.0。

    HOT 主力连续在换月窗口可能同时持有两个月份合约, 故按
    "<EXCHG>.<product>." 前缀汇总。
    """
    prefix = code_prefix(std_code)
    if not prefix:
        return 0.0
    net = 0.0
    for p in (data.get("positions") or []):
        if not isinstance(p, dict):
            continue
        code = str(p.get("code", ""))
        if not code.startswith(prefix):
            continue
        net += _vol(p.get("long")) - _vol(p.get("short"))
    return net


def account_funds(data: dict) -> dict:
    """通道资金 (多币种取 CNY, 否则取首项); 不可用 → {}"""
    funds = data.get("funds")
    if not isinstance(funds, dict) or not funds:
        return {}
    v = funds.get("CNY")
    if not isinstance(v, dict):
        v = next((x for x in funds.values() if isinstance(x, dict)), {})
    return v if isinstance(v, dict) else {}


# 非真实成交的 action (撤单行带 volume/price 但并非成交)
_NON_FILL_ACTIONS = ("", "CNL", "CANCEL", "CANCELLED", "C")


def code_prefix(std_code: str) -> str:
    """'SHFE.rb.HOT' → 'SHFE.rb.' (按品种匹配月份合约/trades 行)"""
    parts = str(std_code).split(".")
    if len(parts) < 2:
        return ""
    return f"{parts[0]}.{parts[1]}."


class TradeJournal:
    """交易通道逐笔成交 (generated/traders/<id>/trades.csv) 增量读取。

    列: localid,date,time,code,action,volume,price,tradeid,orderid
    (TraderAdapter::logTrade 落盘, 每笔真实成交追加一行; 撤单行 action
    为 CNL, 带 volume/price 但不是成交 → 必须剔除)。

    用途 (P2): 成交价回写只有"净均价"一个方程, 多笔待回写时**欠定**
    (N 个未知层价, 1 个方程) — 逐笔成交给出每层真实成交均价, 使回写
    对每层都精确, 且不再依赖 fees/avgpx 的间接推断。
    无该文件 (回测/未部署) → 不可用, 调用方回退原 avgpx 反推口径。
    """

    def __init__(self, path: str):
        self.path = path or ""
        self._rows = []          # [(idx, code, volume, price, action)]
        self._read = 0           # 已解析行数 (增量)
        self._stamp = None

    # ---------------------------------------------------------------- io
    def _refresh(self) -> bool:
        if not self.path:
            return False
        try:
            st = os.stat(self.path)
        except OSError:
            self._rows, self._read, self._stamp = [], 0, None
            return False
        stamp = (st.st_mtime, st.st_size)
        if stamp == self._stamp:
            return bool(self._rows) or self._read > 0
        if self._stamp is not None and st.st_size < self._stamp[1]:
            # 文件被轮转/截断 (按交易日切分) → 重新从头读
            self._rows, self._read = [], 0
        self._stamp = stamp
        try:
            with open(self.path, encoding="utf-8",
                      errors="replace") as f:
                lines = f.readlines()
        except OSError:
            return False
        if self._read > len(lines):     # 截断兜底
            self._rows, self._read = [], 0
        for i in range(self._read, len(lines)):
            row = self._parse(lines[i], i)
            if row is not None:
                self._rows.append(row)
        self._read = len(lines)
        return True

    @staticmethod
    def _parse(line: str, idx: int):
        cells = line.strip().split(",")
        if len(cells) < 7:
            return None
        try:
            volume = float(cells[5])
            price = float(cells[6])
        except (TypeError, ValueError):
            return None
        action = cells[4].strip().upper()
        if action in _NON_FILL_ACTIONS or volume <= 0 or price <= 0:
            return None
        return (idx, cells[3].strip(), volume, price, action)

    # ------------------------------------------------------------- 查询
    @property
    def available(self) -> bool:
        return self._refresh()

    def cursor(self):
        """当前已解析行号 (登记待回写时的基准点); 不可用 → None"""
        if not self._refresh():
            return None
        return self._read

    def layer_price(self, code: str, cursor: int, lots: float,
                    skip: float = 0.0):
        """cursor 之后该合约的成交里, 跳过 skip 手后**紧接着 lots 手**的
        加权均价。

        同一合约多笔待回写 (双系统/连加仓) 时, trades.csv 无子系统标签,
        故按"确认顺序 = 成交顺序"逐笔分配成交量 (总成交量与总均价因此
        精确, 逐层归属为该顺序假设)。未读满 (仍在分批成交) → None。

        code 支持 `.HOT`/月份合约: 按 EXCHG.product. 前缀匹配 (trades.csv
        落的是真实月份合约, 策略侧代码是主力连续)。
        """
        if not self._refresh() or cursor is None or lots <= 0:
            return None
        prefix = code_prefix(code)
        got = 0.0
        amount = 0.0
        left_skip = float(skip)
        for idx, rcode, vol, price, _act in self._rows:
            if idx < cursor or not (rcode == code or
                                    (prefix and rcode.startswith(prefix))):
                continue
            if left_skip > 0:
                cut = min(vol, left_skip)
                left_skip -= cut
                vol -= cut
                if vol <= 0:
                    continue
            take = min(vol, lots - got)
            amount += take * price
            got += take
            if got >= lots:
                break
        if got < lots or amount <= 0:
            return None
        return amount / got


class ChannelTruth:
    """真值源访问器 (按 mtime+size 缓存)。

    用法: t = ChannelTruth(path); t.net_position("SHFE.rb.HOT") →
    float|None (None = 真值不可用, 调用方回退框架口径)。

    ⚠️ net_position 本身不做时效判断 (调用方可能只用于观测); **任何用于
    决策的读取都必须先看 stale()** — 陈旧券商仓位会触发错误的重建/平仓
    (策略层 _truth_position 已内置该闸门)。
    """

    def __init__(self, path: str, max_age: float = DEFAULT_MAX_AGE):
        self.path = path or ""
        self.max_age = float(max_age)
        self._stamp = None          # (mtime, size)
        self._data = {}
        self._loaded = False

    # ---------------------------------------------------------------- io
    def _refresh(self) -> None:
        try:
            st = os.stat(self.path)
        except OSError:
            self._data, self._loaded = {}, False
            self._stamp = None
            return
        stamp = (st.st_mtime, st.st_size)
        if self._loaded and stamp == self._stamp:
            return
        self._stamp = stamp
        self._data = load_rtdata(self.path)
        self._loaded = bool(self._data)

    def reload(self) -> bool:
        """强制重读 (mtime 未变也重读); 返回是否可用"""
        self._loaded = False
        self._stamp = None
        self._refresh()
        return self.ok

    # ----------------------------------------------------------- 属性/查询
    @property
    def ok(self) -> bool:
        self._refresh()
        return bool(self._loaded)

    @property
    def mtime(self):
        try:
            return os.stat(self.path).st_mtime
        except OSError:
            return None

    def age(self, now: float = None):
        m = self.mtime
        if m is None:
            return None
        if now is None:
            import time as _t
            now = _t.time()
        return max(0.0, float(now) - m)

    def stale(self, now: float = None) -> bool:
        """文件陈旧 (或不存在); 用于保守动作闸门 (不作废读取)"""
        age = self.age(now)
        return age is None or age > self.max_age

    def net_position(self, std_code: str):
        """净持仓 (多-空); 真值不可用 → None"""
        if not self.ok:
            return None
        return net_position(self._data, std_code)

    def positions(self) -> list:
        self._refresh()
        return list(self._data.get("positions") or [])

    def funds(self) -> dict:
        """通道资金 (真实账户口径); 不可用 → {}"""
        if not self.ok:
            return {}
        return account_funds(self._data)

    def available(self):
        """可用资金 (共享账户口径, 见 risk 侧 P1 议题); 不可用 → None"""
        v = self.funds().get("available")
        if isinstance(v, (int, float)) and v == v:
            return float(v)
        return None
