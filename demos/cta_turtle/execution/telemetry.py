"""可观测层 (A1-2, 2026-10-22): 从 StraTurtle 迁出的告警/日志/journal。

Telemetry 持有策略回指 (self.s), 方法体与迁出前逐字等价
(self.X → self.h.X 机械重写; 状态 dict 仍存于策略实例,
测试可直接观测)。黄金回测端到端回归覆盖。
"""
import os
import time
from execution.paths import GROUP_DIR as _GROUP_DIR



class Telemetry:
    """告警推送+本地落盘 / 否决日志去重 / 资金旁路 / K线旁路 journal"""

    # 显式依赖声明 (A1 尾项): 模块全部读写依赖白名单; 访问未声明名
    # 立即抛 AttributeError (隐式耦合 loud-failure, 替代静默降级)
    DEPS = (
        "_VETO_LOG_COOLDOWN",
        "_bar_last_date",
        "_base",
        "_extract_oi",
        "_fund_live_ts",
        "_get_fees",
        "_inst",
        "_j_d1_date",
        "_j_m5_key",
        "_journal",
        "_nd_field",
        "_notify",
        "_use_journal",
        "_veto_hits",
        "_veto_last",
        "name",
    )

    def __init__(self, host):
        self.h = host   # StrategyHost 显式依赖面 (A1 尾项, 见 host.py)     # StraTurtle 回指 (过渡期; 逐步收敛为显式依赖)

    def alert(self, key: str, text: str):
        """关键事件推送 + **本地落盘** (未配置 webhook 也不能静默)。

        2026-09-17 事故教训: 交易通道登录失败后所有告警只走 notifier,
        而生产 notify.enabled=false → 裸奔持仓两天无人知晓; 故此处无条件
        追加一行到 <group>/Logs/alerts.log, 作为最后一道留痕。
        """
        try:
            if self.h._notify is not None:
                self.h._notify.send(f"{self.h.name()}:{key}", text)
        except Exception:
            pass
        try:
            d = os.path.join(_GROUP_DIR, "Logs")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "alerts.log"), "a",
                      encoding="utf-8") as f:
                f.write("%s %s %s | %s\n" % (time.strftime("%F %T"),
                                             self.h.name(), key, text))
        except OSError:
            pass
    def veto_log(self, context, key: str, msg: str):
        """否决日志去重: 同 key 在冷却期内只记首次 (P2)。

        按 key 独立冷却 (原全局单槽在不同否决原因交替出现时不收敛);
        被抑制期间累计次数, 下次记录时带 [xN] 便于判断持续程度;
        首见同步告警 (风控否决=信号被拦截, 属生产须知事件)。
        """
        now = time.time()
        self.h._veto_hits[key] = self.h._veto_hits.get(key, 0) + 1
        last = self.h._veto_last.get(key)
        if last is not None and now - last < self.h._VETO_LOG_COOLDOWN:
            return
        n = self.h._veto_hits.pop(key, 1)
        self.h._veto_last[key] = now
        extra = f" [x{n} since last log]" if n > 1 else ""
        context.stra_log_text(f"{self.h.name()} {msg}{extra}", level=2)
        self.alert(f"veto_{key}", msg)
    def dump_fund_live(self, context):
        """日内资金旁路落盘 (P2-11): 框架 fund.csv 只在 session_end 日切
        时写入, 盘中权益不可见 (2026-09-18 sc 止损当日费用次日才见)。
        这里每 ≥60s 追加一行到 generated/outputs/<name>/fund_live.csv,
        纯旁路: 写失败只记日志, 绝不影响交易。"""
        now = time.time()
        if now - self.h._fund_live_ts < 60.0:
            return
        try:
            import datetime as _dt
            outdir = os.path.join(_GROUP_DIR, "generated", "outputs",
                                  self.h.name())
            os.makedirs(outdir, exist_ok=True)
            path = os.path.join(outdir, "fund_live.csv")
            new = not os.path.exists(path)
            pnl = context.stra_get_fund_data(0)
            fee = self.h._get_fees(context)
            pos = context.stra_get_position(self.h._inst.code)
            px = context.stra_get_price(self.h._inst.code)
            with open(path, "a", encoding="utf-8") as f:
                if new:
                    f.write("date,time,net_pnl,fee,position,price\n")
                f.write(_dt.datetime.now().strftime("%Y%m%d,%H%M%S")
                        + f",{pnl if isinstance(pnl, (int, float)) else ''}"
                        f",{fee if fee is not None else ''}"
                        f",{pos},"
                        f"{px if px is not None and px == px else ''}\n")
            self.h._fund_live_ts = now
        except Exception as e:
            context.stra_log_text(
                f"{self.h.name()} fund_live dump failed: {e}", level=2)
    def journal_writer(self):
        if self.h._journal is None:
            from core.BarJournal import BarJournal
            self.h._journal = BarJournal(self.h._base.journal_dir)
        return self.h._journal
    def bar_row(self, bars, i: int = -1) -> dict:
        """提取第 i 根 K 线为 journal 行; OHLC 不可靠时返回 None"""
        d = self.h._nd_field(bars, "date")
        if d is None:
            return None
        vals = {}
        for name in ("opens", "highs", "lows", "closes"):
            arr = getattr(bars, name, None)
            try:
                vals[name] = float(arr[i]) if arr is not None else None
            except (TypeError, ValueError, IndexError):
                vals[name] = None
        if any(vals[n] is None for n in ("opens", "highs", "lows",
                                         "closes")):
            return None
        vol_arr = getattr(bars, "volumes", None)
        try:
            vol = float(vol_arr[i]) if vol_arr is not None else 0.0
        except (TypeError, ValueError, IndexError):
            vol = 0.0
        turnover = 0.0
        nd = getattr(bars, "ndarray", None)
        names = getattr(getattr(nd, "dtype", None), "names", None) or ()
        if "turnover" in names:
            try:
                turnover = float(nd["turnover"][i])
            except (TypeError, ValueError, IndexError):
                turnover = 0.0
        t = self.h._nd_field(bars, "time")
        oi_arr = self.h._extract_oi(bars)
        try:
            oi = float(oi_arr[i]) if oi_arr is not None else 0.0
            dt = int(d[i])
            tm = int(t[i]) if t is not None else 0
        except (TypeError, ValueError, IndexError):
            return None
        return {"date": dt, "time": tm,
                "open": vals["opens"], "high": vals["highs"],
                "low": vals["lows"], "close": vals["closes"],
                "volume": vol, "turnover": turnover,
                "open_interest": oi}
    def journal_signal_bar(self, bars):
        """信号周期新完成 bar → journal (首call建基线不写历史)"""
        if not self.h._use_journal or bars is None:
            return
        date = self.h._bar_last_date(bars)
        if date is None:
            return
        if self.h._j_d1_date is None:
            self.h._j_d1_date = date
            return
        if date == self.h._j_d1_date:
            return
        row = self.bar_row(bars)
        if row:
            self.journal_writer().append(self.h._inst.code,
                                          self.h._inst.period, row)
        self.h._j_d1_date = date
    def journal_intrabar_bars(self, mbars, mkey):
        """盘中主K新增段 → journal"""
        if not self.h._use_journal or mbars is None or mkey is None:
            return
        if self.h._j_m5_key is None:
            self.h._j_m5_key = float(mkey[-1])
            return
        writer = None
        for i in range(len(mkey)):
            if mkey[i] <= self.h._j_m5_key:
                continue
            row = self.bar_row(mbars, i)
            if row:
                if writer is None:
                    writer = self.journal_writer()
                sub_period = getattr(self.h._base, "intrabar_period", "m5")
                writer.append(self.h._inst.code, sub_period, row)
        self.h._j_m5_key = float(mkey[-1])
