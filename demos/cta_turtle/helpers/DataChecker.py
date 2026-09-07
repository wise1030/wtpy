"""数据可用性预检 (P1-4)

品种启用前提 (缺任一项 C++ 回放器段错误):
1. common/hots.json 含该品种键;
2. storage 有对应周期 K 线 (d1→his/day, m5→his/min5; h1 由引擎聚合分钟线);
3. intrabar_stop 另需盘中主K数据。

设计约束: 本模块必须零崩溃 — DSB 解析走 WtDtHelper 并全程 try/except,
不可用时降级为文件存在性检查 (历史上有 libWtDataStorage 段错误前科)。
"""
import json
import os

# 策略周期 → 存储目录/文件后缀
_PERIOD_DIR = {"d1": "day", "m5": "min5", "m1": "min1"}
_PERIOD_SUFFIX = {"d1": "d", "m5": "m5", "m1": "m1"}
_SUB_FOR_INTRABAR = {"m5": "min5", "m1": "min1"}


class DataIssue:
    def __init__(self, code: str, level: str, msg: str):
        self.code = code
        self.level = level      # error | warn
        self.msg = msg

    def __repr__(self):
        return f"[{self.level.upper():5s}] {self.code}: {self.msg}"


def _split_code(code: str):
    parts = code.split(".")
    if len(parts) < 2:
        return None, None
    return parts[0], parts[1]


def _csv_candidates(code: str, suffix: str, storage_dir: str):
    return [
        os.path.join(storage_dir, "csv",
                     f"{code}_{suffix}.csv"),
        os.path.join(storage_dir, f"{code}_{suffix}.csv"),
    ]


def _dsb_path(code: str, period: str, storage_dir: str):
    exchg, product = _split_code(code)
    if not exchg:
        return None
    pdir = _PERIOD_DIR.get(period, period)
    return os.path.join(storage_dir, "his", pdir, exchg,
                        f"{exchg}.{product}_HOT.dsb")


def _read_csv_health(path: str):
    """轻量 CSV 健康统计; 失败返回 None"""
    try:
        dates = []
        with open(path, encoding='utf-8') as f:
            f.readline()
            for line in f:
                s = line.strip()
                if not s:
                    continue
                d = s.split(",")[0].strip()
                digits = d.replace("/", "").replace("-", "")
                if digits.isdigit() and len(digits) >= 8:
                    dates.append(int(digits[:8]))
        if not dates:
            return None
        gaps = sum(1 for a, b in zip(dates, dates[1:])
                   if b < a)   # 仅检倒序 (跨品种节假日缺口不误报)
        return {"rows": len(dates), "first": min(dates),
                "last": max(dates), "inversions": gaps}
    except OSError:
        return None


def _read_dsb_health(path: str):
    """经 WtDtHelper 读 DSB 概要; 任何异常返回 None (降级不崩溃)"""
    try:
        from wtpy.wrapper.WtDtHelper import WtDataHelper
        helper = WtDataHelper()
        bars = helper.read_dsb_bars(path)
        if bars is None or len(bars) == 0:
            return None
        nd = getattr(bars, "ndarray", None)
        if nd is None:
            return {"rows": len(bars), "first": None, "last": None,
                    "inversions": 0}
        d = nd["date"]
        inversions = int(sum(1 for a, b in zip(d, d[1:]) if b < a))
        return {"rows": len(bars), "first": int(d[0]),
                "last": int(d[-1]), "inversions": inversions}
    except Exception:
        return None


def check_bars(code: str, period: str, storage_dir: str) -> list:
    """单品种单周期 K 线存在性/健康检查"""
    issues = []
    dsb = _dsb_path(code, period, storage_dir)
    health = None
    src = None
    dsb_exists = bool(dsb) and os.path.exists(dsb)
    if not dsb_exists:
        for cand in _csv_candidates(code, _PERIOD_SUFFIX.get(period, ""),
                                    storage_dir):
            if os.path.exists(cand):
                health = _read_csv_health(cand)
                src = cand
                break
    else:
        src = dsb
        health = _read_dsb_health(dsb)
        if health is None:
            # 解析失败不误报缺失 (段错误前科 → 降级为存在性通过)
            return [DataIssue(
                code, "warn",
                f"{period}: DSB 存在但无法解析 ({dsb}), 按存在性放行; "
                f"若回测异常请用 scripts/convert_csv_to_dsb.py 重建")]

    if health is None:
        hint = dsb if dsb else "storage"
        issues.append(DataIssue(
            code, "error",
            f"缺少 {period} K线 (找过 {hint} 与 storage/csv); "
            f"可用 wtpy.apps.WtHotPicker+datahelper 拉取或 "
            f"scripts/convert_csv_to_dsb.py 转换"))
        return issues

    where = os.path.basename(src)
    if health["inversions"] > 0:
        issues.append(DataIssue(
            code, "warn",
            f"{where}: {health['rows']} 根, 日期乱序 "
            f"{health['inversions']} 处"))
    rng = ""
    if health["first"]:
        rng = f" {health['first']}~{health['last']}"
    issues.append(DataIssue(
        code, "info",
        f"{period}: OK {where}{rng} rows={health['rows']}"))
    return issues


def check_hots(code: str, common_dir: str, live: bool = False):
    """hots.json 键检查; 返回 (是否可用, hots路径, 规则数)

    规则数: -1=键缺失/文件不可读, >=0=键存在 (0=空规则)。
    ⚠️ live=True (实盘) 时规则数必须 > 0: 空规则在实盘数据链路会
    导致 cacheIntegratedBars 返回空容器 → libWtDataStorage 段错误
    (2026-09-01 远端实测, segfault at 0x70)。回测直读 *_HOT.dsb 不受影响。
    """
    exchg, product = _split_code(code)
    hot_path = os.path.join(common_dir, "hots.json")
    if not os.path.exists(hot_path) or not exchg:
        return False, hot_path, -1
    try:
        with open(hot_path, encoding='utf-8') as f:
            hots = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False, hot_path, -1
    rules = hots.get(exchg, {}).get(product)
    if not isinstance(rules, list):
        return False, hot_path, -1
    n = len(rules)
    # 实盘口径: 空规则视为缺失 (致死级)
    return (n > 0 if live else True), hot_path, n


def fix_hots(code: str, common_dir: str):
    """向 hots.json 补空键 (空列表=不换月); 返回是否写入"""
    exists, hot_path, _n = check_hots(code, common_dir)
    if exists:
        return False
    exchg, product = _split_code(code)
    try:
        with open(hot_path, encoding='utf-8') as f:
            hots = json.load(f)
        hots.setdefault(exchg, {})[product] = []
        with open(hot_path, "w", encoding='utf-8') as f:
            json.dump(hots, f, indent=2, ensure_ascii=False)
        return True
    except OSError:
        return False


def required_depth(base_cfg, period: str) -> int:
    """策略暖机所需最小K线根数 (与 StraTurtle 订阅口径一致)"""
    if period in _SUB_FOR_INTRABAR:      # 盘中主K (m5/m1)
        return max(60, int(getattr(base_cfg, "intrabar_cnt", 300)))
    return max(60, int(getattr(base_cfg, "bar_cnt", 120)))


def check_depth(base_cfg, code: str, period: str,
                storage_dir: str) -> list:
    """历史深度检查: 可读行数 < 暖机需求 → error; 无法读取 → 跳过"""
    need = required_depth(base_cfg, period)
    dsb = _dsb_path(code, period, storage_dir)
    health = None
    if dsb and os.path.exists(dsb):
        health = _read_dsb_health(dsb)
    else:
        for cand in _csv_candidates(code, _PERIOD_SUFFIX.get(period, ""),
                                    storage_dir):
            if os.path.exists(cand):
                health = _read_csv_health(cand)
                break
    if health is None:
        # 存在性由 check_bars 负责, 这里只做可读时的深度判定
        return []
    rows = health.get("rows") or 0
    if rows < need:
        return [DataIssue(
            code, "error",
            f"{period} 历史深度不足: {rows} 根 < 暖机需求 {need} "
            f"(bar_cnt/intrabar_cnt); 补数后再启动, 否则指标 NaN 空转")]
    return [DataIssue(
        code, "info", f"{period}: 深度 OK ({rows} >= {need})")]


def check_config(cfg, common_dir: str, storage_dir: str,
                 live: bool = False) -> list:
    """整配置预检: hots 键 + 各品种信号周期/盘中周期 K 线 + 深度

    live=True (runLive/deploy run.py): hots 空规则升级为 error —
    实盘数据链路上空规则会导致 cacheIntegratedBars 空容器 segfault
    (2026-09-01 129.211.5.54 实测); 回测直读 *_HOT.dsb 不受影响。
    """
    issues = []
    for inst in cfg.instruments:
        ok, hot_path, n_rules = check_hots(inst.code, common_dir,
                                           live=live)
        if not ok:
            if n_rules == 0 and live:
                issues.append(DataIssue(
                    inst.code, "error",
                    f"hots.json 规则为空 ({inst.code}): 实盘会段错误! "
                    f"须写入当前主力映射 — scripts/fetch_data_tqsdk.py "
                    f"--fix-hots 或人工填 {hot_path}"))
            else:
                issues.append(DataIssue(
                    inst.code, "error",
                    f"hots.json 缺键 ({hot_path}); 运行 "
                    f"scripts/prepare_data.py --fix-hots 可自动补空键"))
        issues.extend(check_bars(inst.code, inst.period, storage_dir))
        issues.extend(check_depth(cfg.base, inst.code, inst.period,
                                  storage_dir))
        if inst.intrabar_stop:
            sub = getattr(cfg.base, "intrabar_period", "m5")
            issues.extend(check_bars(inst.code, sub, storage_dir))
            issues.extend(check_depth(cfg.base, inst.code, sub,
                                      storage_dir))
    return issues
