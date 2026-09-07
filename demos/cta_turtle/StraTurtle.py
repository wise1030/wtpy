"""StraTurtle — 海龟趋势跟踪策略 (wtpy CTA 框架)

架构: 薄壳模式, 信号逻辑全部在 helpers/TurtleEngine (纯逻辑可单测)。
本层职责:
- 生命周期: on_init 订阅K线/注册图表/恢复状态, on_calculate 主流程
- 指标计算: ATR(N) + Donchian 通道 (helpers/indicators)
- 执行: 方向闸 → VaR 手数 → stra_set_position 目标仓位
- 换月适配: avgpx 漂移 re-anchor 引擎层价格 (.HOT 自动移仓)
- 持久化: user_save_data 引擎快照, 重启恢复
- 图表: 通道线/止损线叠加主图, ATR/持仓副图, 进出场 mark

框架对齐:
- 交易 .HOT 主力连续合约, C++ 核心自动换月
- 合约乘数取 stra_get_comminfo().volscale (非配置)
- set_position 目标语义, live 由 executers.yaml 执行单元落单
"""
import json
import os
import time

import numpy as np
from wtpy import BaseCtaStrategy, CtaContext

from schemas import InstrumentConfig, TurtleBaseConfig, RiskConfig
from filters import build_chain, FilterContext
from helpers.indicators import wilder_atr, donchian
from helpers.TurtleEngine import (TurtleEngine, DEC_ENTER, DEC_ADD,
                                  DEC_EXIT, DEC_SKIP, DEC_ARM)
from helpers.PositionSizer import PositionSizer
from helpers.RiskManager import RiskManager


class StraTurtle(BaseCtaStrategy):

    def __init__(self, name: str, inst_cfg: InstrumentConfig,
                 base_cfg: TurtleBaseConfig,
                 risk_cfg: RiskConfig = None,
                 risk_mgr: RiskManager = None,
                 notify=None,
                 bt_no_simtick: bool = False):
        BaseCtaStrategy.__init__(self, name)
        self._inst = inst_cfg
        self._base = base_cfg
        self._notify = notify    # helpers/notifier.WebhookNotifier 或 None
        # bt 日线 trigger 退化: 无 m5 数据时不订 tick/m5 副K (模拟tick
        # 只能由非日K bar 生成, simTicks 跳过 KP_DAY), 触发判定仍走
        # on_bar 的 check_bar, 撮合退化为 bar 收盘口径 (成交价偏保守,
        # 与 rb 有 m5 时的 tick 撮合基线不可直接对拍)
        self._bt_no_simtick = bt_no_simtick

        # === 子系统装配 ===
        # S1+S2: 经典海龟双系统并行, 各分得 floor/ceil 半量 units,
        # 共享过滤链与组合风控账本 (同 code 合并记账), 独立引擎状态;
        # 框架持仓为两子系统净和, 批量执行后统一 set_position。
        # units=1 时 S2 分 0 手 → 其入场按 lots=0 否决 (空转)。
        self._subs = []

        def _mk_sub(sysname: str, units: int):
            return {"name": sysname, "units": max(0, int(units)),
                    "engine": TurtleEngine(
                        system=sysname,
                        stop_atr_mult=base_cfg.stop_atr_mult,
                        pyramid_layers=base_cfg.pyramid_layers,
                        pyramid_step_atr=base_cfg.pyramid_step_atr,
                        enable_s1_filter=base_cfg.enable_s1_filter,
                        entry_mode=base_cfg.entry_mode)}

        if inst_cfg.system == "S1+S2":
            u = int(inst_cfg.units)
            self._subs.append(_mk_sub("S1", u - u // 2))
            self._subs.append(_mk_sub("S2", u // 2))
        else:
            self._subs.append(_mk_sub(inst_cfg.system, inst_cfg.units))
        # 兼容别名: 单系统即唯一引擎; 双系统指向 S1 子系统
        self._engine = self._subs[0]["engine"]

        self._sizer = PositionSizer(base_cfg.unit_var)
        # 组合风控: runLive 多品种共享同一实例; 单品种回测私有实例等价
        self._risk = risk_mgr or RiskManager(risk_cfg or RiskConfig())
        # 信号过滤链 (隐式方向闸 + instruments[].filters)
        self._chain = build_chain(inst_cfg)
        self._volscale = None       # on_init 时从框架取
        self._pricetick = None
        self._last_veto_key = None  # 方向闸/手数否决日志去重
        self._recon_miss = 0        # 运行时对账连续失配计数
        self._last_n = float("nan")  # 对账重建记账用最近 N
        # 盘中去重状态 (P1: None=待首个 calculate 建基线, 不回放历史)
        self._d1_date = None        # 已消费的信号周期末根 bar 日期
        self._m5_key = None         # 已扫描的盘中止损主K末根键值
        self._catchup_date = None   # 自愈评估已检查的交易日 (每日至多一次)
        self._in_schedule = False   # on_calculate 期间置位 (图表API仅此时可用)
        self._fill_pending = None   # 成交均价待回写 {"engine": 子引擎}
        # K线旁路日志 (数据完整性: 崩溃窗口兜底, helpers/BarJournal)
        self._journal = None        # 惰性创建 BarJournal
        self._j_d1_date = None      # 已落日志的信号周期日期
        self._j_m5_key = None       # 已落日志的盘中键值

    @property
    def _use_journal(self) -> bool:
        return bool(self._base.journal_dir)

    def _journal_writer(self):
        if self._journal is None:
            from helpers.BarJournal import BarJournal
            self._journal = BarJournal(self._base.journal_dir)
        return self._journal

    def _bar_row(self, bars, i: int = -1) -> dict:
        """提取第 i 根 K 线为 journal 行; OHLC 不可靠时返回 None"""
        d = self._nd_field(bars, "date")
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
        t = self._nd_field(bars, "time")
        oi_arr = self._extract_oi(bars)
        try:
            oi = float(oi_arr[i]) if oi_arr is not None else 0.0
            dt = int(d[i])
            tm = int(t[i]) if t is not None else 0
        except (TypeError, ValueError, IndexError):
            return None
        return {"date": dt, "time": tm,
                "open": vals["opens"], "high": vals["highs"],
                "low": vals["lows"], "close": vals["closes"],
                "volume": vol, "open_interest": oi}

    def _journal_signal_bar(self, bars):
        """信号周期新完成 bar → journal (首call建基线不写历史)"""
        if not self._use_journal or bars is None:
            return
        date = self._bar_last_date(bars)
        if date is None:
            return
        if self._j_d1_date is None:
            self._j_d1_date = date
            return
        if date == self._j_d1_date:
            return
        row = self._bar_row(bars)
        if row:
            self._journal_writer().append(self._inst.code,
                                          self._inst.period, row)
        self._j_d1_date = date

    def _journal_intrabar_bars(self, mbars, mkey):
        """盘中主K新增段 → journal"""
        if not self._use_journal or mbars is None or mkey is None:
            return
        if self._j_m5_key is None:
            self._j_m5_key = float(mkey[-1])
            return
        writer = None
        for i in range(len(mkey)):
            if mkey[i] <= self._j_m5_key:
                continue
            row = self._bar_row(mbars, i)
            if row:
                if writer is None:
                    writer = self._journal_writer()
                sub_period = getattr(self._base, "intrabar_period", "m5")
                writer.append(self._inst.code, sub_period, row)
        self._j_m5_key = float(mkey[-1])

    # ------------------------------------------------------------------
    # 工厂
    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls, name: str, code: str, config_path: str,
                    risk_mgr: RiskManager = None, notify=None,
                    bt_no_simtick: bool = False):
        from helpers.ConfigLoader import ConfigLoader
        cfg = ConfigLoader.load(config_path)
        for inst in cfg.instruments:
            if inst.code == code:
                return cls(name, inst, cfg.base, risk_cfg=cfg.risk,
                           risk_mgr=risk_mgr, notify=notify,
                           bt_no_simtick=bt_no_simtick)
        raise ValueError(f"{code} 不在 {config_path} 的 instruments 中")

    # ------------------------------------------------------------------
    # 事件推送
    # ------------------------------------------------------------------
    def _safe_mark(self, context, price: float, icon: str, tag: str):
        """图表标记安全封装: 仅 schedule 时段可用 (CtaStraBaseCtx.
        cpp:1978 非 schedule 打 error 并丢弃), on_tick 路径静默跳过"""
        if not self._in_schedule:
            return
        context.add_chart_mark(price, icon, tag)

    def _alert(self, key: str, text: str):
        """关键事件 webhook 推送 (未配置/失败静默)"""
        try:
            if self._notify is not None:
                self._notify.send(f"{self.name()}:{key}", text)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 子系统访问
    # ------------------------------------------------------------------
    @property
    def _is_dual(self) -> bool:
        return len(self._subs) > 1

    def _sub(self, name: str):
        for s in self._subs:
            if s["name"] == name:
                return s
        return None

    def _combined_target(self) -> int:
        """两子系统的净目标仓位 (框架 set_position 的最终标的)"""
        t = 0
        for s in self._subs:
            e = s["engine"]
            if e.direction != 0 and e.layers:
                sign = 1 if e.direction > 0 else -1
                t += sign * sum(l for l, _ in e.layers)
        return t

    def _first_active_sub(self):
        for s in self._subs:
            if s["engine"].direction != 0:
                return s
        return self._subs[0]

    def _sub_lots(self, sub) -> int:
        e = sub["engine"]
        if e.direction == 0:
            return 0
        return sum(l for l, _ in e.layers)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def on_init(self, context: CtaContext):
        code = self._inst.code
        p = self._base

        # 盘中止损模式: 主K切换为 intrabar_period, 信号周期(d1/h1)作副K
        if self._inst.intrabar_stop:
            self._main_period = p.intrabar_period
            context.stra_prepare_bars(code, self._main_period,
                                      p.intrabar_cnt, isMain=True)
        else:
            self._main_period = self._inst.period
            context.stra_prepare_bars(code, self._main_period,
                                      p.bar_cnt, isMain=True)

        # trigger 模式: 订阅 tick 驱动盘中触发; 日线主K时另备 m5 副K
        # (实盘日线不闭合 F3 → session_end 用 m5 聚合合成当日日K评估)
        # bt_no_simtick: bt 且无 m5 数据 → 不订 tick/副K, 触发判定走
        # on_bar check_bar, 撮合退化为 bar 收盘口径
        if p.entry_mode == "trigger" and not self._bt_no_simtick:
            context.stra_sub_ticks(code)
            if not self._inst.intrabar_stop:
                context.stra_prepare_bars(code, p.intrabar_period,
                                          p.intrabar_cnt, isMain=False)
        elif p.entry_mode == "trigger":
            context.stra_log_text(
                f"{self.name()} bt_no_simtick: trigger 判定走 "
                f"check_bar, 撮合=bar收盘口径 (无 m5 模拟tick)")

        comm = context.stra_get_comminfo(code)
        self._volscale = float(comm.volscale)
        self._pricetick = float(comm.pricetick)

        # === 图表注册 ===
        context.set_chart_kline(code, self._inst.period)
        idx = f"Turtle_{self.name()}"
        self._idx = idx
        context.register_index(idx, idxType=1)      # 副图: ATR + 持仓
        context.register_index_line(idx, "atr_n", lineType=0)
        context.register_index_line(idx, "position", lineType=1)
        context.add_index_baseline(idx, "position", 0.0)
        ch_idx = f"TurtleCh_{self.name()}"
        self._ch_idx = ch_idx
        context.register_index(ch_idx, idxType=0)   # 主图叠加: 通道 + 止损
        context.register_index_line(ch_idx, "entry_upper", lineType=0)
        context.register_index_line(ch_idx, "entry_lower", lineType=0)
        context.register_index_line(ch_idx, "stop", lineType=0)

        # === 状态恢复 (v4: +trigger武装; v3: 双子系统+去重键; v2/v1 兼容) ===
        raw = context.user_load_data(f"{self.name()}_engine", "", str)
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("v") in (3, 4):
                    for nm, snap in (data.get("subs") or {}).items():
                        sub = self._sub(nm)
                        if sub:
                            sub["engine"].restore(snap)
                    self._d1_date = data.get("d1_date")
                    self._m5_key = data.get("m5_key")
                elif isinstance(data, dict) and data.get("v") == 2:
                    self._subs[0]["engine"].restore(data["engine"])
                    self._d1_date = data.get("d1_date")
                    self._m5_key = data.get("m5_key")
                else:
                    self._subs[0]["engine"].restore(data)
                if p.entry_mode == "trigger":
                    # 旧版快照无武装状态 → 按已有 N/通道重建 (幂等)
                    for s in self._subs:
                        if s["engine"].armed.get("long") is None and \
                                s["engine"].armed.get("stop") is None:
                            s["engine"].rearm()
                    # 孤儿 pending 清除: pending=触发决策已发出待成交
                    # 确认; 进程重启后其下落不可知 (若决策在策略层丢失
                    # 则从未下单 —— 2026-09-07 实测: 9/4 旧代码
                    # _last_n=NaN 静默丢弃 add 决策, pending 挂起后经
                    # 快照持久化/恢复, 永久吞掉后续全部触发, sc 盘中
                    # 709 穿越 add 位 688.32 无响应)。清除后武装位保留,
                    # 价格仍在位上方时下一 tick 立即补票触发; 若决策
                    # 实际已成交而 confirm 前崩溃, 下方持仓对账与运行时
                    # reconcile 按券商实际持仓兜底。
                    for s in self._subs:
                        dropped = s["engine"].clear_pending()
                        if dropped is not None:
                            context.stra_log_text(
                                f"{self.name()}[{s['name']}] drop orphan "
                                f"pending {dropped} on restart "
                                f"(re-trigger if price still beyond level)",
                                level=2)
                            self._alert("orphan_pending",
                                        f"drop orphan pending {dropped} "
                                        f"on restart")
                    # _last_n 同步: 引擎 _n 已随快照恢复, 策略层缓存
                    # 不同步则重启后~下次日评估间所有触发决策被
                    # isfinite(_last_n) 闸静默丢弃 (2026-09-04 夜盘实测:
                    # sc 穿越 add 位无响应)
                    for s in self._subs:
                        en = s["engine"]._n
                        if en is not None and np.isfinite(en):
                            self._last_n = en
                            break
                context.stra_log_text(
                    f"{self.name()} restored: dir={self._engine.direction}, "
                    f"layers={len(self._engine.layers)}")
            except (json.JSONDecodeError, TypeError) as e:
                context.stra_log_text(
                    f"{self.name()} state restore failed: {e}", level=2)

        # 持仓状态对账: 框架有持仓但引擎空仓 → 按单层重建到首子系统
        pos = context.stra_get_position(code)
        if pos != 0 and self._combined_target() == 0:
            avgpx = context.stra_get_position_avgpx(code)
            self._subs[0]["engine"].direction = 1 if pos > 0 else -1
            self._subs[0]["engine"].layers = [[abs(int(pos)), avgpx]]
            context.stra_log_text(
                f"{self.name()} adopt orphan position {pos}@{avgpx}, "
                f"rebuilt as single layer", level=2)

        # 组合风控账本恢复: RiskManager._book 进程内存态不持久化,
        # 重启后为空 → 持仓期间组合总敞口/单位数约束被低估
        # (2026-09-04 审计发现)。按恢复/收养的引擎持仓重建记账。
        tot_lots = 0
        wsum = 0.0
        dirn = 0
        n0 = None
        for s in self._subs:
            e = s["engine"]
            if e.direction == 0 or not e.layers:
                continue
            dirn = e.direction
            for l, pr in e.layers:
                tot_lots += l
                wsum += l * pr
            if n0 is None and e._n is not None and np.isfinite(e._n):
                n0 = e._n
        if tot_lots > 0 and not self._risk.has_position(code):
            self._risk.on_rebuilt(
                code, dirn, tot_lots, wsum / tot_lots,
                n0 or 1.0, self._volscale or 1.0,
                p.stop_atr_mult, self._inst.group)
            context.stra_log_text(
                f"{self.name()} risk book restored: dir={dirn} "
                f"lots={tot_lots} avg={wsum / tot_lots:.2f}")

        context.stra_log_text(
            f"StraTurtle inited: {code} {self._inst.period} "
            f"{self._inst.system} units={self._inst.units} "
            f"dir_gate={self._inst.direction}")

    def on_calculate(self, context: CtaContext):
        self._in_schedule = True
        try:
            if self._inst.intrabar_stop:
                self._on_calc_intrabar(context)
            else:
                self._on_calc_daily(context)
        finally:
            self._in_schedule = False

    def _on_calc_daily(self, context: CtaContext):
        code = self._inst.code
        p = self._base
        bars = context.stra_get_bars(code, self._inst.period, p.bar_cnt,
                                     isMain=True)
        need = max(p.s2_entry, p.s1_entry, p.s2_exit, p.atr_period) + 2
        if bars is None or len(bars) < need:
            return

        closes = bars.closes
        highs = bars.highs
        lows = bars.lows

        # trigger 模式 (回测日线粒度): 先用当根 OHLC 做触碰仿真
        # (按昨日武装位), 再进入日线评估 (武装次日)。
        # 触碰仿真按 bar 日期去重: 重启重放同一根信号 bar 时不得二次
        # 消费 (否则恢复的 add 武装位会对同根 bar 重复触发, 原测试靠
        # _last_n=NaN 意外挡住的真问题, 2026-09-04 修复 _last_n 同步后暴露)
        if p.entry_mode == "trigger":
            d_cur = self._bar_last_date(bars)
            if d_cur is not None and self._d1_date is not None \
                    and int(d_cur) > int(self._d1_date):
                # 新交易日: 上一交易日挂起的 pending 必然已死 (正常流程
                # pending 在同一 tick/bar 内被 confirm/reject 消费,
                # 跨日存活 = 决策丢失), 清除后当根触碰仿真立即可用
                self._drop_stale_pending(context, f"bar_eval {d_cur}")
            if d_cur is None or self._d1_date is None \
                    or int(d_cur) > int(self._d1_date):
                opens = getattr(bars, "opens", None)
                o = float(opens[-1]) if opens is not None \
                    else float(closes[-1])
                self._run_trigger_bar(context, o, float(highs[-1]),
                                      float(lows[-1]), float(closes[-1]))

        atr, n, chan = self._calc_indicators(highs, lows, closes)
        if n is None:
            return
        self._last_n = n

        # === HOT 换月 re-anchor: 持仓不变时 avgpx 漂移 = 换月价差 ===
        self._reanchor_on_roll(context)

        # === 过滤器上下文 (只读快照, 供过滤链判定) ===
        fctx = FilterContext(
            code=code, price=float(closes[-1]), n=n, n_series=atr,
            highs=highs, lows=lows, closes=closes,
            volumes=getattr(bars, "volumes", None),
            open_interests=self._extract_oi(bars))

        if self._is_dual:
            dec_map = {}
            for sub in self._subs:
                dec_map[sub["name"]] = sub["engine"].on_bar(
                    float(highs[-1]), float(lows[-1]), float(closes[-1]),
                    float(closes[-2]), n, chan[sub["name"]])
            self._execute_dual(context, dec_map, n, fctx)
        else:
            decisions = self._engine.on_bar(
                float(highs[-1]), float(lows[-1]), float(closes[-1]),
                float(closes[-2]), n, chan[self._inst.system])
            for dec in decisions:
                self._execute(context, dec, n, fctx)

        # K线旁路日志: 新完成信号bar幂等落盘 (崩溃窗口兜底)
        self._journal_signal_bar(bars)

        # trigger 模式: 记录已评估日期, 防 on_session_end 合成路径重评估
        if p.entry_mode == "trigger":
            d = self._bar_last_date(bars)
            if d is not None:
                self._d1_date = d

        self._post_calculate(context, closes, n, chan)

    def _calc_indicators(self, highs, lows, closes):
        """ATR + 各子系统 Donchian 通道。

        Returns:
            (atr序列, N, {子系统名: channels dict}); N 无效返回 (None,..)
        """
        p = self._base
        atr = wilder_atr(highs, lows, closes, p.atr_period)
        n = float(atr[-1])
        if not np.isfinite(n):
            return None, None, None

        need_s1 = self._inst.system != "S2"
        need_s2 = self._inst.system != "S1"
        e_up = e_lo = x_up = x_lo = None
        s2_up = s2_lo = None
        if need_s1:
            e_up, e_lo = donchian(highs, lows, p.s1_entry)
            x_up, x_lo = donchian(highs, lows, p.s1_exit)
        if need_s2:
            s2_up, s2_lo = donchian(highs, lows, p.s2_entry)

        def _mk(eu, el, xu, xl):
            fu, fl = (s2_up, s2_lo) if need_s2 else \
                donchian(highs, lows, p.s2_entry)
            return {
                'entry_upper': float(eu[-1]), 'entry_lower': float(el[-1]),
                'exit_upper': float(xu[-1]), 'exit_lower': float(xl[-1]),
                'fs_upper': float(fu[-1]), 'fs_lower': float(fl[-1]),
                'prev_entry_upper': float(eu[-2]),
                'prev_entry_lower': float(el[-2]),
                'prev_fs_upper': float(fu[-2]),
                'prev_fs_lower': float(fl[-2]),
            }

        chan = {}
        if self._is_dual:
            chan["S1"] = _mk(e_up, e_lo, x_up, x_lo)
            chan["S2"] = _mk(s2_up, s2_lo,
                             donchian(highs, lows, p.s2_exit)[0],
                             donchian(highs, lows, p.s2_exit)[1])
        elif self._inst.system == "S1":
            chan["S1"] = _mk(e_up, e_lo, x_up, x_lo)
        else:
            ex_u, ex_l = donchian(highs, lows, p.s2_exit)
            chan["S2"] = _mk(s2_up, s2_lo, ex_u, ex_l)
        return atr, n, chan

    def _post_calculate(self, context: CtaContext, closes, n: float,
                        chan: dict, mark: bool = True):
        """对账 → 图表 → 持久化 (daily/intrabar 共用收尾)

        图表段仅 schedule 时段可用 (C++ set_index_value 非 schedule 打
        error 丢弃, CtaStraBaseCtx:2036) — mark 再叠 _in_schedule 闸,
        on_tick/session_end 路径自动跳过。"""
        code = self._inst.code
        self._reconcile(context, closes)
        self._reanchor_actual_fill(context)
        pos = context.stra_get_position(code)
        first = self._first_active_sub()
        channels = chan[first["name"]]
        if mark and self._in_schedule:
            context.set_index_value(self._idx, "atr_n", n)
            context.set_index_value(self._idx, "position", float(pos))
            context.set_index_value(self._ch_idx, "entry_upper",
                                    channels['entry_upper'])
            context.set_index_value(self._ch_idx, "entry_lower",
                                    channels['entry_lower'])
            fe = first["engine"]
            if fe.direction != 0 and fe.layers:
                # 双系统图表取首活跃子系统止损 (净仓位图元为合并口径)
                context.set_index_value(self._ch_idx, "stop",
                                        fe.stop_price(n))
        snap = {"v": 4,
                "subs": {s["name"]: s["engine"].snapshot()
                         for s in self._subs},
                "d1_date": self._d1_date, "m5_key": self._m5_key}
        context.user_save_data(f"{self.name()}_engine", json.dumps(snap))

    # ------------------------------------------------------------------
    # 盘中止损路径 (P1): 主K=intrabar_period 触发; 信号周期仅取完成日
    # ------------------------------------------------------------------
    def _on_calc_intrabar(self, context: CtaContext):
        code = self._inst.code
        p = self._base
        mbars = context.stra_get_bars(code, p.intrabar_period,
                                      p.intrabar_cnt, isMain=True)
        bars = context.stra_get_bars(code, self._inst.period, p.bar_cnt)
        need = max(p.s2_entry, p.s1_entry, p.s2_exit, p.atr_period) + 2
        if mbars is None or len(mbars) == 0 or bars is None \
                or len(bars) < need:
            return

        closes = bars.closes
        highs = bars.highs
        lows = bars.lows
        atr, n, chan = self._calc_indicators(highs, lows, closes)
        if n is None:
            return
        self._last_n = n

        mkey = self._bar_keys(mbars)
        ddate = self._bar_last_date(bars)

        # 基线/去重判定相互独立: 任一键值体系缺失只跳过对应能力
        can_scan = False
        if mkey is not None:
            if self._m5_key is None:
                self._m5_key = float(mkey[-1])    # 首次建基线不回放
            else:
                can_scan = True
        can_signal = False
        if ddate is not None:
            if self._d1_date is None:
                self._d1_date = ddate             # 首次建基线
            else:
                can_signal = True

        self._reanchor_on_roll(context)

        # 1) 盘中段: trigger 模式逐根做触碰仿真(含止损/出场/加仓/入场);
        #    close 模式扫描 2N 止损
        if can_scan:
            if p.entry_mode == "trigger":
                self._run_trigger_bars(context, mbars, mkey)
            else:
                self._scan_intrabar_stop(context, mbars, mkey)
            self._journal_intrabar_bars(mbars, mkey)
            self._m5_key = float(mkey[-1])

        # 2) 日线信号: 信号周期出现新完成 bar 才评估一次 (日期去重)
        if can_signal and ddate != self._d1_date:
            self._drop_stale_pending(context, f"bar_eval {ddate}")
            self._d1_date = ddate
            fctx = FilterContext(
                code=code, price=float(closes[-1]), n=n, n_series=atr,
                highs=highs, lows=lows, closes=closes,
                volumes=getattr(bars, "volumes", None),
                open_interests=self._extract_oi(bars))
            last_h, last_l, last_c = float(highs[-1]), float(lows[-1]), \
                float(closes[-1])
            prev_c = float(closes[-2])
            if self._is_dual:
                dec_map = {s["name"]: s["engine"].on_bar(
                    last_h, last_l, last_c, prev_c, n, chan[s["name"]])
                    for s in self._subs}
                self._execute_dual(context, dec_map, n, fctx)
            else:
                decisions = self._engine.on_bar(
                    last_h, last_l, last_c, prev_c, n,
                    chan[self._inst.system])
                for dec in decisions:
                    self._execute(context, dec, n, fctx)

        # K线旁路日志 (与决策去重独立, 幂等)
        self._journal_signal_bar(bars)

        self._post_calculate(context, closes, n, chan)

    def _scan_intrabar_stop(self, context: CtaContext, mbars, mkey):
        """扫描新增主K段是否触及各子系统 2N 止损 → 即时离场 (净仓调整)"""
        actives = [s for s in self._subs
                   if s["engine"].direction != 0 and s["engine"].layers]
        if not actives or mkey is None:
            return
        n = self._last_n
        if not np.isfinite(n):
            return
        prev_key = self._m5_key
        highs = np.asarray(mbars.highs, dtype=float)
        lows = np.asarray(mbars.lows, dtype=float)
        times = self._nd_field(mbars, "time")
        code = self._inst.code
        hits = []   # (sub, stop_px, bar_idx)
        for i in range(len(mkey)):
            if mkey[i] <= prev_key:
                continue
            for sub in actives:
                if any(h[0] is sub for h in hits):
                    continue
                e = sub["engine"]
                sp = e.stop_price(n)
                hit = (e.direction > 0 and lows[i] <= sp) or \
                      (e.direction < 0 and highs[i] >= sp)
                if hit:
                    hits.append((sub, sp, i))
            if len(hits) == len(actives):
                break
        if not hits:
            return
        for sub, sp, i in hits:
            tstr = f"{int(times[i])}" if times is not None else "?"
            old = self._sub_lots(sub) * (1 if sub["engine"].direction > 0
                                         else -1)
            sub["engine"].confirm_exit(sp)
            self._safe_mark(context, sp, "exit", self.name())
            context.stra_log_text(
                f"{self.name()}[{sub['name']}] intrabar exit "
                f"{old}@{sp:.2f} (stop touched @m5 {tstr})", level=1)
        new_t = self._combined_target()
        cur = int(round(context.stra_get_position(code)))
        if new_t != cur:
            context.stra_set_position(code, new_t, "ExitI")
        self._sync_risk_book(n)

    # ------------------------------------------------------------------
    # trigger 模式 (方案C): 盘中触发执行路由
    # 信号=价格穿越武装位 (tick/bar); 过滤器于武装时(日评估)前置裁决;
    # 风控于触发时复核当前权益; 否决 → engine.reject_pending 当日撤武装
    # ------------------------------------------------------------------
    def _run_trigger_bar(self, context: CtaContext, o: float, h: float,
                         l: float, c: float):
        """单根 bar 触碰仿真 (bt 日线/m5 路径)"""
        if not np.isfinite(self._last_n):
            return
        for sub in self._subs:
            decs = sub["engine"].check_bar(o, h, l, c)
            if os.environ.get("WT_TRIG_DEBUG") == "1":
                context.stra_log_text(
                    f"[trigdbg] {sub['name']} d={context.stra_get_date()} "
                    f"OHLC={o:.0f}/{h:.0f}/{l:.0f}/{c:.0f} "
                    f"armed={sub['engine'].armed} "
                    f"decs={[d.get('decision') for d in decs]}")
            self._exec_trigger_decs(context, sub, decs)

    def _run_trigger_bars(self, context: CtaContext, mbars, mkey):
        """新增主K段逐根触碰仿真 (trigger 模式的 m5 主K路径)"""
        prev_key = self._m5_key
        opens = getattr(mbars, "opens", None)
        highs = mbars.highs
        lows = mbars.lows
        closes = mbars.closes
        for i in range(len(mkey)):
            if mkey[i] <= prev_key:
                continue
            o = float(opens[i]) if opens is not None else float(closes[i])
            self._run_trigger_bar(context, o, float(highs[i]),
                                  float(lows[i]), float(closes[i]))

    def on_tick(self, context: CtaContext, stdCode: str, newTick):
        """实盘 tick 驱动触发 (trigger 模式)"""
        if self._base.entry_mode != "trigger" or stdCode != self._inst.code:
            return
        # 新交易日自愈评估: session_end 未触发(实盘收盘后无 tick,
        # ticker 兜底要求行情时间推进过收盘点, 白盘 15:00 后无行情
        # → 1515 永不触发) 或盘后启动时, 首个 tick 检测 his 末根
        # 未评估则立即评估武装 (每日至多检查一次)
        tdate = context.stra_get_tdate()
        if tdate and self._catchup_date != int(tdate):
            self._catchup_date = int(tdate)
            self._catchup_eval(context)
        px = context.stra_get_price(stdCode)
        if px is None or px != px or px <= 0:
            return
        self._reanchor_actual_fill(context)   # 分批窗口闭合即回写均价
        fired = False
        for sub in self._subs:
            decs = sub["engine"].on_price(float(px))
            if decs:
                fired = True
            self._exec_trigger_decs(context, sub, decs)
        if fired:
            self._persist(context)   # 触发事件即时落快照

    def _exec_trigger_decs(self, context: CtaContext, sub: dict,
                           decs: list):
        """触发决策执行: 入场/加仓已过武装时过滤, 触发时只过风控闸"""
        if not decs:
            return
        if not np.isfinite(self._last_n):
            # 重启后~下次日评估的窗口外不应发生 (快照已恢复 _n → _last_n
            # 在 on_init 同步); 仍兜底告警而非静默丢弃
            context.stra_log_text(
                f"{self.name()} trigger decisions dropped: "
                f"N unavailable (restart pre-eval window)", level=2)
            self._alert("trigger_drop", "trigger decisions dropped: "
                        "N unavailable (restart pre-eval window)")
            return
        for dec in decs:
            dec["prefiltered"] = True      # 武装时已裁决, 免重复过滤
        if self._is_dual:
            self._execute_dual(context, {sub["name"]: decs}, self._last_n)
        else:
            for dec in decs:
                self._execute(context, dec, self._last_n)

    def _on_arm(self, context: CtaContext, engine, fctx):
        """武装落地: 过滤链对入场方向逐一裁决, 否决方向当日解除武装"""
        for d, lv in engine.entry_levels().items():
            if lv is None:
                continue
            dec = {"price": lv, "direction": d}
            if not self._run_filter_chain(context, d, dec, fctx):
                engine.disarm_entry(d)

    def _persist(self, context: CtaContext):
        """轻量持久化 (触发事件后): 仅引擎快照+去重键"""
        snap = {"v": 4,
                "subs": {s["name"]: s["engine"].snapshot()
                         for s in self._subs},
                "d1_date": self._d1_date, "m5_key": self._m5_key}
        context.user_save_data(f"{self.name()}_engine", json.dumps(snap))

    @staticmethod
    def _nd_field(bars, field):
        """防御式读取 K 线结构化字段 (WtNpKline.ndarray / 测试桩属性)"""
        nd = getattr(bars, "ndarray", None)
        if nd is not None and field in (
                getattr(nd.dtype, "names", None) or ()):
            return nd[field]
        return getattr(bars, field, None)

    def _bar_keys(self, bars):
        """单调递增 bar 键值数组 (date*1e6 + time%1e6); 无法构造返回 None"""
        d = self._nd_field(bars, "date")
        t = self._nd_field(bars, "time")
        if d is None or t is None or len(d) != len(t):
            return None
        try:
            d64 = np.asarray(d, dtype=np.int64)
            t64 = np.asarray(t, dtype=np.int64)
        except (TypeError, ValueError):
            return None
        return d64 * 1000000 + (t64 % 1000000)

    def _bar_last_date(self, bars):
        d = self._nd_field(bars, "date")
        if d is None or len(d) == 0:
            return None
        try:
            return int(d[-1])
        except (TypeError, ValueError):
            return None

    def on_session_end(self, context: CtaContext, curTDate: int):
        if self._combined_target() != 0:
            subs = ",".join(
                f"{s['name']}:dir{s['engine'].direction}"
                f"x{self._sub_lots(s)}"
                for s in self._subs
                if s["engine"].direction != 0)
            msg = (f"session end: pos="
                   f"{context.stra_get_position(self._inst.code)} ({subs})")
            context.stra_log_text(f"{self.name()} {msg}")
            self._alert("session", msg)
        # trigger 模式: 实盘日线永不闭合(F3) → session end 用当日 m5
        # 聚合合成日K, 做收盘日线评估并武装次日触发位
        if self._base.entry_mode == "trigger":
            self._session_eval_daily(context, curTDate)

    def _drop_stale_pending(self, context: CtaContext, tag: str):
        """日评估前清除跨日 stale pending: pending=触发决策已发出待
        成交确认, 挂起超过一个交易日意味着决策已在策略层丢失 (如
        N 缺失窗口静默丢弃) 或订单状态不可知; 保留武装位, 清除后
        新一轮评估/触发照常进行 (与 on_init 孤儿 pending 清除同源)"""
        for s in self._subs:
            dropped = s["engine"].clear_pending()
            if dropped is not None:
                context.stra_log_text(
                    f"{self.name()}[{s['name']}] drop stale pending "
                    f"{dropped} @ {tag} (decision lost in-flight)",
                    level=2)
                self._alert("stale_pending",
                            f"drop stale pending {dropped} @ {tag}")

    def _session_eval_daily(self, context: CtaContext, curTDate: int):
        """trigger 模式收盘评估: m5 聚合当日日K → 指标 → on_bar 武装"""
        code = self._inst.code
        p = self._base
        if self._d1_date is not None and int(self._d1_date) == int(curTDate):
            return                               # bt 日线到达路径已评估
        self._drop_stale_pending(context, f"session_eval {curTDate}")
        bars = context.stra_get_bars(code, self._inst.period, p.bar_cnt)
        need = max(p.s2_entry, p.s1_entry, p.s2_exit, p.atr_period) + 2
        if bars is None or len(bars) < need - 1:
            return
        # 当日 m5 聚合 (his 无当日, 见 DATA_OPS F3)
        synth = None
        mbars = context.stra_get_bars(code, p.intrabar_period,
                                      p.intrabar_cnt)
        if mbars is not None and len(mbars) > 0:
            dates = self._nd_field(mbars, "date")
            if dates is not None:
                idx = [i for i in range(len(dates))
                       if int(dates[i]) == int(curTDate)]
                if idx:
                    opens = getattr(mbars, "opens", None)
                    synth = (
                        float(opens[idx[0]]) if opens is not None
                        else float(mbars.closes[idx[0]]),
                        float(np.max(np.asarray(mbars.highs)[idx])),
                        float(np.min(np.asarray(mbars.lows)[idx])),
                        float(mbars.closes[idx[-1]]))
        closes = np.asarray(bars.closes, dtype=float)
        highs = np.asarray(bars.highs, dtype=float)
        lows = np.asarray(bars.lows, dtype=float)
        prev_close = float(closes[-1])
        if synth is not None:
            o, h, l, c = synth
            highs = np.append(highs, h)
            lows = np.append(lows, l)
            closes = np.append(closes, c)
        else:
            # 无当日 m5 (异常兜底): 以 his 末根充当, 评估幂等
            o, h, l, c = float(highs[-1]), float(highs[-1]), \
                float(lows[-1]), prev_close
        atr, n, chan = self._calc_indicators(highs, lows, closes)
        if n is None:
            return
        self._last_n = n
        self._d1_date = int(curTDate)
        # avgpx 漂移对账 (实盘 d1 主K不闭合, on_calculate 路径永不执行,
        # 换月/记账偏差修正必须挂在日评估路径)
        self._reanchor_on_roll(context)
        fctx = FilterContext(
            code=code, price=float(closes[-1]), n=n, n_series=atr,
            highs=highs, lows=lows, closes=closes,
            volumes=None, open_interests=None)
        if self._is_dual:
            dec_map = {s["name"]: s["engine"].on_bar(
                h, l, c, prev_close, n, chan[s["name"]])
                for s in self._subs}
            self._execute_dual(context, dec_map, n, fctx)
        else:
            for dec in self._engine.on_bar(h, l, c, prev_close, n,
                                           chan[self._inst.system]):
                self._execute(context, dec, n, fctx)
        # 当日合成日K落 journal (崩溃窗口兜底)
        if synth is not None and self._use_journal:
            self._journal_writer().append(
                code, "d", {"date": int(curTDate), "time": 1500,
                            "open": o, "high": h, "low": l, "close": c})
        self._post_calculate(context, closes, n, chan)

    def _catchup_eval(self, context: CtaContext):
        """新交易日自愈评估 (仅实盘 trigger 日线): 用 his 数据
        (不含当日) 补做日线评估并武装。与 session_end 的 synth=None
        兜底分支同口径; bt 不启用 (bt 日K闭合, on_bar 路径完整)。"""
        if self._base.entry_mode != "trigger" or self._inst.intrabar_stop:
            return
        if getattr(context, "is_backtest", False):
            return
        code = self._inst.code
        p = self._base
        bars = context.stra_get_bars(code, self._inst.period, p.bar_cnt)
        need = max(p.s2_entry, p.s1_entry, p.s2_exit, p.atr_period) + 2
        if bars is None or len(bars) < need:
            return
        last_date = self._bar_last_date(bars)
        if last_date is None:
            return
        if self._d1_date is not None and int(self._d1_date) >= int(last_date):
            return                        # his 末根已评估过
        self._drop_stale_pending(context, f"catchup {last_date}")
        highs = np.asarray(bars.highs, dtype=float)
        lows = np.asarray(bars.lows, dtype=float)
        closes = np.asarray(bars.closes, dtype=float)
        atr, n, chan = self._calc_indicators(highs, lows, closes)
        if n is None:
            return
        self._last_n = n
        self._d1_date = int(last_date)
        # avgpx 漂移对账 (同 _session_eval_daily, 见该处注释)
        self._reanchor_on_roll(context)
        h, l, c = float(highs[-1]), float(lows[-1]), float(closes[-1])
        prev_c = float(closes[-2])
        fctx = FilterContext(
            code=code, price=c, n=n, n_series=atr,
            highs=highs, lows=lows, closes=closes,
            volumes=getattr(bars, "volumes", None),
            open_interests=self._extract_oi(bars))
        if self._is_dual:
            dec_map = {s["name"]: s["engine"].on_bar(h, l, c, prev_c, n,
                                                     chan[s["name"]])
                       for s in self._subs}
            self._execute_dual(context, dec_map, n, fctx)
        else:
            for dec in self._engine.on_bar(h, l, c, prev_c, n,
                                           chan[self._inst.system]):
                self._execute(context, dec, n, fctx)
        context.stra_log_text(
            f"{self.name()} catchup eval: his last={last_date} armed "
            f"(session_end 缺失/盘后启动自愈)")
        self._post_calculate(context, closes, n, chan, mark=False)

    def on_backtest_end(self, context: CtaContext):
        pos = context.stra_get_position(self._inst.code)
        if pos != 0:
            context.stra_log_text(
                f"{self.name()} backtest end open pos {pos}, force close",
                level=3)
            context.stra_set_position(self._inst.code, 0, "BEC")
        context.stra_log_text(f"{self.name()} backtest ended")

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    def _execute(self, context: CtaContext, dec: dict, n: float,
                 fctx: FilterContext = None):
        code = self._inst.code
        kind = dec["decision"]

        if kind == DEC_ARM:
            # trigger 模式: 武装时过滤链前置裁决入场方向
            self._on_arm(context, self._engine, fctx)
            a = self._engine.armed
            context.stra_log_text(
                f"{self.name()} armed L={a['long']} S={a['short']} "
                f"stop={a['stop']} add={a['add']} exit={a['exit']}")
            return

        if kind == DEC_SKIP:
            context.stra_log_text(
                f"{self.name()} S1 filter skip {dec['direction']}@"
                f"{dec['price']:.2f} (last winner), fail-safe armed")
            return

        if kind == DEC_EXIT:
            old_pos = context.stra_get_position(code)
            context.stra_set_position(code, 0, "Exit")
            self._engine.confirm_exit(dec["price"])
            self._risk.on_closed(code)
            self._safe_mark(context, dec["price"], "exit", self.name())
            context.stra_log_text(
                f"{self.name()} exit {old_pos}@{dec['price']:.2f} "
                f"reason={dec['reason']}")
            return

        if kind == DEC_ENTER:
            d = dec["direction"]
            # === 过滤链 (隐式方向闸 + 配置过滤器): 否决不提交引擎 ===
            # trigger 模式触发决策已在武装时裁决 (prefiltered), 跳过
            if not dec.get("prefiltered") and \
                    not self._run_filter_chain(context, d, dec, fctx):
                self._reject_trigger(self._engine)
                return
            lots = self._sizer.layer_lots(n, self._volscale,
                                          self._inst.units)
            if lots <= 0:
                self._veto_log(context, f"lot{d}",
                               f"veto enter {d}@{dec['price']:.2f}: "
                               f"lots=0 (N={n:.2f}×volscale={self._volscale} "
                               f"> budget)")
                self._reject_trigger(self._engine)
                return
            # === 组合风控闸门: 缩量优先, 归零否决 ===
            equity = self._risk.equity(context)
            final_lots, reasons = self._risk.cap_entry(
                code, lots=lots, n=n, volscale=self._volscale,
                price=dec["price"], stop_mult=self._base.stop_atr_mult,
                equity=equity, group=self._inst.group)
            if final_lots <= 0:
                self._veto_log(
                    context, f"risk{d}",
                    f"veto enter {d}@{dec['price']:.2f}: risk "
                    f"({'/'.join(reasons)}), equity={equity:.0f}")
                self._reject_trigger(self._engine)
                return
            if final_lots < lots:
                self._veto_log(
                    context, f"rscale{d}",
                    f"risk scaled {lots}->{final_lots} lots "
                    f"({'/'.join(reasons)}), equity={equity:.0f}")
            self._last_veto_key = None
            context.stra_set_position(code, d * final_lots, "Enter")
            fill = self._implied_fill(context, prev_pos=0, prev_avg=0.0,
                                      fallback=dec["price"])
            self._engine.confirm_enter(d, final_lots, fill)
            self._fill_pending = {"engine": self._engine,
                                     "ts": time.time()}
            self._risk.on_filled(code, d, final_lots, fill, n,
                                 self._volscale, self._base.stop_atr_mult,
                                 self._inst.group)
            self._safe_mark(context, dec["price"],
                                   "buy" if d > 0 else "sell", self.name())
            fs = " [FAIL-SAFE]" if dec.get("failsafe") else ""
            context.stra_log_text(
                f"{self.name()} enter {'LONG' if d > 0 else 'SHORT'} "
                f"{final_lots} lots @~{fill:.2f}, N={n:.2f}, VaR/layer="
                f"{n * self._volscale * final_lots:.0f}{fs}")
            return

        if kind == DEC_ADD:
            d = self._engine.direction
            lots = self._sizer.layer_lots(n, self._volscale,
                                          self._inst.units)
            if lots <= 0:
                self._veto_log(context, f"add{len(self._engine.layers)}",
                               f"veto add @{dec['price']:.2f}: lots=0")
                self._reject_trigger(self._engine)
                return
            equity = self._risk.equity(context)
            final_lots, reasons = self._risk.cap_entry(
                code, lots=lots, n=n, volscale=self._volscale,
                price=dec["price"], stop_mult=self._base.stop_atr_mult,
                equity=equity, group=self._inst.group)
            if final_lots <= 0:
                self._veto_log(
                    context, f"addrisk{len(self._engine.layers)}",
                    f"veto add @{dec['price']:.2f}: risk "
                    f"({'/'.join(reasons)})")
                self._reject_trigger(self._engine)
                return
            if final_lots < lots:
                self._veto_log(
                    context, f"addscale{len(self._engine.layers)}",
                    f"risk scaled add {lots}->{final_lots} "
                    f"({'/'.join(reasons)})")
            self._last_veto_key = None
            old_pos = context.stra_get_position(code)
            old_avg = context.stra_get_position_avgpx(code)
            context.stra_set_position(code, old_pos + d * final_lots, "Add")
            fill = self._implied_fill(context, prev_pos=old_pos,
                                      prev_avg=old_avg,
                                      fallback=dec["price"])
            self._engine.confirm_add(final_lots, fill)
            self._fill_pending = {"engine": self._engine,
                                     "ts": time.time()}
            self._risk.on_filled(code, d, final_lots, fill, n,
                                 self._volscale, self._base.stop_atr_mult,
                                 self._inst.group)
            self._safe_mark(context, dec["price"],
                                   "buy" if d > 0 else "sell", self.name())
            context.stra_log_text(
                f"{self.name()} add layer{len(self._engine.layers)} "
                f"{final_lots} lots @~{fill:.2f}, N={n:.2f}")
            return

    def _run_filter_chain(self, context: CtaContext, sig_dir: int,
                          dec: dict, fctx: FilterContext = None) -> bool:
        """信号过滤链: 任一过滤器否决即拒绝 (不触碰引擎状态)"""
        for flt in self._chain:
            try:
                if fctx is not None:
                    ok, why = flt.allow_entry(fctx.with_direction(sig_dir))
                else:   # 直测路径 (_execute 无 bar 上下文)
                    ok, why = flt.allow_entry(FilterContext(sig_dir=sig_dir))
            except Exception as e:   # 过滤器异常不阻断交易, 记录后放行
                context.stra_log_text(
                    f"{self.name()} filter {flt.name} error: {e}", level=2)
                continue
            if not ok:
                self._veto_log(
                    context, f"f_{flt.name}_{sig_dir}",
                    f"veto enter {'LONG' if sig_dir > 0 else 'SHORT'}@"
                    f"{dec['price']:.2f}: filter={flt.name} ({why})")
                return False
        return True

    @staticmethod
    def _extract_oi(bars):
        """防御式提取持仓量序列 (WtNpKline 经 .ndarray 暴露原始字段;
        无该字段/非框架 K 线对象时返回 None, 过滤器自行降级)"""
        nd = getattr(bars, "ndarray", None)
        if nd is None:
            return None
        names = getattr(nd.dtype, "names", None) or ()
        if "open_interest" in names:
            return nd["open_interest"]
        return None

    # ------------------------------------------------------------------
    # 运行时对账 (P0-3)
    # ------------------------------------------------------------------
    def _reconcile(self, context: CtaContext, closes):
        """引擎层与实际持仓持续失配检测。

        实盘部分成交/拒单/手工干预会造成 stra_get_position 与
        引擎 layers 脱节 (on_init 只收养一次)。连续 recon_patience
        根 bar 失配则告警并按实际持仓重建为单层, 风控账本同步覆盖。
        单根失配(订单在途)不动作。
        """
        code = self._inst.code
        pos = context.stra_get_position(code)
        eng_pos = self._combined_target()
        try:
            pos_i = int(round(pos))
        except (TypeError, ValueError):
            return
        if pos_i == eng_pos:
            self._recon_miss = 0
            return

        self._recon_miss += 1
        if self._recon_miss < int(self._base.recon_patience):
            return

        n = self._last_n if np.isfinite(self._last_n) else 0.0
        if pos_i == 0:
            # 外部全平: 全部活跃子系统确认出场, 风控清账
            px = float(closes[-1])
            held = [s for s in self._subs
                    if s["engine"].direction != 0 and s["engine"].layers]
            avg_entry = 0.0
            tot = 0
            for s in held:
                L = sum(l for l, _ in s["engine"].layers)
                avg_entry += sum(l * pr for l, pr
                                 in s["engine"].layers)
                tot += L
            avg_entry = avg_entry / max(1, tot)
            context.stra_log_text(
                f"{self.name()} RECONCILE: engine held "
                f"({','.join(s['name'] for s in held) or '-'}) but broker "
                f"flat, flatten @~{px:.2f} (entry~{avg_entry:.2f})", level=3)
            self._alert("reconcile", f"broker flat, engine flattened "
                                     f"@~{px:.2f}")
            for s in held:
                s["engine"].confirm_exit(px)
            self._risk.on_closed(code)
        else:
            d = 1 if pos_i > 0 else -1
            avgpx = abs(float(context.stra_get_position_avgpx(code) or 0))
            if avgpx <= 0:
                avgpx = float(closes[-1])
            context.stra_log_text(
                f"{self.name()} RECONCILE: mismatch persisted "
                f"(engine net={eng_pos}; broker {pos_i}), rebuilt into "
                f"{self._subs[0]['name']} as single layer "
                f"[{abs(pos_i)}@{avgpx:.2f}]", level=3)
            self._alert("reconcile", f"mismatch persisted, rebuilt "
                                     f"{abs(pos_i)}@{avgpx:.2f}")
            first = self._subs[0]["engine"]
            first.direction = d
            first.layers = [[abs(pos_i), avgpx]]
            for s in self._subs[1:]:
                s["engine"].direction = 0
                s["engine"].layers = []
            self._risk.on_rebuilt(code, d, abs(pos_i), avgpx,
                                  n if n > 0 else 1.0, self._volscale or 1.0,
                                  self._base.stop_atr_mult, self._inst.group)
        self._recon_miss = 0

    # ------------------------------------------------------------------
    # S1+S2 双系统批量执行 (净仓语义: 框架持仓 = Σ子系统目标)
    # ------------------------------------------------------------------
    def _execute_dual(self, context: CtaContext, dec_map: dict, n: float,
                      fctx: FilterContext = None):
        """逐子系统消费决策 (只改引擎态), 批次结束统一落一次净仓单。

        成交价口径: 引擎按决策价(bar close)确认 — 回测撮合价即当根
        收盘, 实盘经执行单元的偏差由运行时对账兜底。
        """
        code = self._inst.code
        for sub in self._subs:
            for dec in dec_map.get(sub["name"], []):
                kind = dec["decision"]
                e = sub["engine"]
                tag = f"[{sub['name']}]"
                if kind == DEC_ARM:
                    self._on_arm(context, e, fctx)
                    continue
                if kind == DEC_SKIP:
                    context.stra_log_text(
                        f"{self.name()}{tag} S1 filter skip "
                        f"{dec['direction']}@{dec['price']:.2f} "
                        f"(last winner), fail-safe armed")
                    continue
                if kind == DEC_EXIT:
                    old = self._sub_lots(sub) * \
                        (1 if e.direction > 0 else -1)
                    e.confirm_exit(dec["price"])
                    self._safe_mark(context, dec["price"], "exit",
                                           self.name())
                    context.stra_log_text(
                        f"{self.name()}{tag} exit {old}"
                        f"@{dec['price']:.2f} reason={dec['reason']}")
                    continue
                if kind == DEC_ENTER:
                    d = dec["direction"]
                    lots = self._apply_enter_gates(context, sub, d,
                                                   dec, n, fctx)
                    if lots <= 0:
                        self._reject_trigger(e)
                        continue
                    e.confirm_enter(d, lots, dec["price"])
                    self._fill_pending = {"engine": e, "ts": time.time()}
                    self._safe_mark(context,
                                    dec["price"],
                                    "buy" if d > 0 else "sell",
                                    self.name())
                    fs = " [FAIL-SAFE]" if dec.get("failsafe") else ""
                    context.stra_log_text(
                        f"{self.name()}{tag} enter "
                        f"{'LONG' if d > 0 else 'SHORT'} {lots} "
                        f"@~{dec['price']:.2f}, N={n:.2f}{fs}")
                    continue
                if kind == DEC_ADD:
                    lots = self._apply_add_gates(context, sub, dec, n)
                    if lots <= 0:
                        self._reject_trigger(e)
                        continue
                    e.confirm_add(lots, dec["price"])
                    self._fill_pending = {"engine": e, "ts": time.time()}
                    dmark = "buy" if e.direction > 0 else "sell"
                    self._safe_mark(context, dec["price"], dmark,
                                           self.name())
                    context.stra_log_text(
                        f"{self.name()}{tag} add "
                        f"layer{len(e.layers)} {lots} "
                        f"@~{dec['price']:.2f}, N={n:.2f}")

        new_t = self._combined_target()
        cur = int(round(context.stra_get_position(code)))
        if new_t != cur:
            context.stra_set_position(code, new_t, "Dual")
        self._sync_risk_book(n)

    def _apply_enter_gates(self, context: CtaContext, sub: dict, d: int,
                           dec: dict, n: float,
                           fctx: FilterContext = None) -> int:
        """入场三道闸: 过滤链 → VaR 手数 → 组合风控。返回最终手数(0=否决)"""
        code = self._inst.code
        tag = f"[{sub['name']}]"
        if not dec.get("prefiltered") and \
                not self._run_filter_chain(context, d, dec, fctx):
            return 0
        lots = self._sizer.layer_lots(n, self._volscale, sub["units"])
        if lots <= 0:
            self._veto_log(context, f"lot{d}",
                           f"{tag} veto enter {d}@{dec['price']:.2f}: "
                           f"lots=0 (units={sub['units']}, "
                           f"N={n:.2f}×volscale={self._volscale})")
            return 0
        equity = self._risk.equity(context)
        final_lots, reasons = self._risk.cap_entry(
            code, lots=lots, n=n, volscale=self._volscale,
            price=dec["price"], stop_mult=self._base.stop_atr_mult,
            equity=equity, group=self._inst.group,
            share=self._sub_share(sub))
        if final_lots <= 0:
            self._veto_log(
                context, f"risk{d}",
                f"{tag} veto enter {d}@{dec['price']:.2f}: risk "
                f"({'/'.join(reasons)}), equity={equity:.0f}")
            return 0
        if final_lots < lots:
            self._veto_log(
                context, f"rscale{d}",
                f"{tag} risk scaled {lots}->{final_lots} lots "
                f"({'/'.join(reasons)}), equity={equity:.0f}")
        return final_lots

    def _apply_add_gates(self, context: CtaContext, sub: dict,
                         dec: dict, n: float) -> int:
        e = sub["engine"]
        tag = f"[{sub['name']}]"
        lots = self._sizer.layer_lots(n, self._volscale, sub["units"])
        if lots <= 0:
            self._veto_log(context, f"add{len(e.layers)}",
                           f"{tag} veto add @{dec['price']:.2f}: lots=0")
            return 0
        equity = self._risk.equity(context)
        final_lots, reasons = self._risk.cap_entry(
            self._inst.code, lots=lots, n=n, volscale=self._volscale,
            price=dec["price"], stop_mult=self._base.stop_atr_mult,
            equity=equity, group=self._inst.group,
            share=self._sub_share(sub))
        if final_lots <= 0:
            self._veto_log(
                context, f"addrisk{len(e.layers)}",
                f"{tag} veto add @{dec['price']:.2f}: risk "
                f"({'/'.join(reasons)})")
            return 0
        return final_lots

    def _sub_share(self, sub: dict) -> float:
        """子系统品种内预算份额 (双系统按 units 占比; 单系统=1.0)"""
        if not self._is_dual or int(self._inst.units) <= 0:
            return 1.0
        return max(0.0, min(1.0, sub["units"] / float(self._inst.units)))

    def _sync_risk_book(self, n: float):
        """按两子系统合并仓位覆盖式同步风控账本"""
        code = self._inst.code
        tot_l = 0
        wsum = 0.0
        dirn = 0
        for s in self._subs:
            e = s["engine"]
            if e.direction == 0 or not e.layers:
                continue
            L = sum(l for l, _ in e.layers)
            wsum += sum(l * pr for l, pr in e.layers)
            tot_l += L
            dirn = e.direction   # 双系统同向持仓 (反向并存时以首活跃为准)
        if dirn == 0 or tot_l == 0:
            self._risk.on_closed(code)
            return
        price = wsum / tot_l
        self._risk.sync_position(
            code, dirn, tot_l, price,
            n if n and n > 0 else 1.0, self._volscale or 1.0,
            self._base.stop_atr_mult, self._inst.group)

    def _reject_trigger(self, engine):
        """trigger 模式: 策略层否决已触发决策 → 引擎解除当日武装"""
        if engine.entry_mode == "trigger":
            engine.reject_pending()

    def _veto_log(self, context: CtaContext, key: str, msg: str):
        """否决日志去重: 同类否决连续触发只记首次; 首见同步告警
        (风控否决=信号被拦截, 属生产须知事件, P0-2)"""
        if self._last_veto_key == key:
            return
        self._last_veto_key = key
        context.stra_log_text(f"{self.name()} {msg}", level=2)
        self._alert(f"veto_{key}", msg)

    def _implied_fill(self, context: CtaContext, prev_pos: float,
                      prev_avg: float, fallback: float) -> float:
        """从 avgpx 增量反推成交价 (回测=当根收盘, 实盘=成交均价)。

        符号约定(复核确认): 框架 avgpx 带持仓方向符号 (空头为负,
        avgpx×pos=成本>0), 故用有符号量做增量计算后取绝对值。
        """
        code = self._inst.code
        new_pos = context.stra_get_position(code)
        new_avg = context.stra_get_position_avgpx(code)
        if new_pos == prev_pos or new_pos == 0 or new_avg == 0:
            return fallback
        fill = (new_avg * new_pos - prev_avg * prev_pos) / (new_pos - prev_pos)
        fill = abs(fill)
        return fill if fill > 0 else fallback

    def _reanchor_actual_fill(self, context: CtaContext):
        """成交均价回写: 用框架 avgpx 反推最后确认层的实际成交价。

        confirm 时分批窗口未闭合 → 记账价 fallback 触发价; 待持仓量
        达到目标(分批完成)后, 从 avgpx 增量反推最后一层实际价修正,
        stop/add/exit 武装位随引擎 rearm 自动跟随 (偏差>0.5tick 才动)。
        与 _reanchor_on_roll 协同: 本方法先精确修最后一层, 换月检测
        的 our_avg vs fw_avg 差随之收敛, 不误报 hot roll。
        仅单方向持仓时数学可解 (双系统反向净额化无法归因单层, 跳过)。
        """
        fp = self._fill_pending
        if fp is None:
            return
        e = fp["engine"]
        if e.direction == 0 or not e.layers:
            self._fill_pending = None
            return
        code = self._inst.code
        pos = context.stra_get_position(code)
        if int(round(pos)) != self._combined_target() or pos == 0:
            # 分批窗口超时 (>10min 未达目标仓位) → 告警一次
            # (执行单元放弃追单/涨跌停无法成交的可观测代理, P0-2)
            if not fp.get("alerted") and \
                    time.time() - fp.get("ts", 0) > 600:
                fp["alerted"] = True
                context.stra_log_text(
                    f"{self.name()} fill timeout: target="
                    f"{self._combined_target()} vs broker={pos}, "
                    f"10min unfilled", level=3)
                self._alert("fill_timeout",
                            f"target={self._combined_target()} "
                            f"vs broker={pos}, 10min unfilled")
            return                # 分批成交中/未成交, 下次再试
        fw_avg = context.stra_get_position_avgpx(code)
        if fw_avg == 0:
            return
        dirs = {s["engine"].direction for s in self._subs
                if s["engine"].direction != 0}
        if len(dirs) > 1:
            self._fill_pending = None   # 双系统反向, 净仓成本无法归因
            return
        sign = 1 if e.direction > 0 else -1
        last_lots, recorded = e.layers[-1]
        rest = 0.0                # 除最后确认层外全部层的带符号成本
        for s in self._subs:
            se = s["engine"]
            if se.direction == 0:
                continue
            ss = 1 if se.direction > 0 else -1
            layers = se.layers[:-1] if se is e else se.layers
            for l, p in layers:
                rest += ss * l * p
        actual = (fw_avg * pos - rest) / (sign * last_lots)
        self._fill_pending = None
        if actual <= 0:
            return                # 反推异常(持仓干预等), 放弃修正
        if self._pricetick and abs(actual - recorded) > 0.5 * self._pricetick:
            e.reanchor_last_price(actual)
            context.stra_log_text(
                f"{self.name()} fill re-anchor layer{len(e.layers)} "
                f"{recorded:.2f} -> {actual:.2f} (avgpx), "
                f"stop/add levels shifted")

    def _reanchor_on_roll(self, context: CtaContext):
        """avgpx 漂移对账: 持仓量不变但框架 avgpx 偏离引擎记账 → 各层平移。

        覆盖两类漂移: ①HOT 换月价差; ②重启丢失 _fill_pending 后残留的
        记账价偏差 (fill 回写的崩溃窗口兜底)。框架 avgpx 带方向符号
        (空头为负), 与层价格 (恒正) 比较时取绝对值。
        实盘 d1 主K不闭合 → on_calculate 路径不执行, 故同时挂在
        _session_eval_daily/_catchup_eval 两条日评估路径。"""
        e = self._engine
        if e.direction == 0 or not e.layers:
            return
        code = self._inst.code
        pos = context.stra_get_position(code)
        tot_lots = sum(l for l, _ in e.layers)
        if pos == 0 or abs(int(pos)) != tot_lots:
            return   # 持仓量不符(部分成交/手工干预)时不做 re-anchor
        our_avg = sum(l * pr for l, pr in e.layers) / tot_lots
        fw_avg = abs(context.stra_get_position_avgpx(code))
        shift = fw_avg - our_avg
        if self._pricetick and abs(shift) > 0.5 * self._pricetick:
            e.shift_prices(shift)
            context.stra_log_text(
                f"{self.name()} avgpx drift re-anchor: shift "
                f"{shift:+.2f} (hot roll or stale fill), "
                f"layer prices re-anchored")
