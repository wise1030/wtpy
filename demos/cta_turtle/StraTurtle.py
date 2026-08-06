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

import numpy as np
from wtpy import BaseCtaStrategy, CtaContext

from schemas import InstrumentConfig, TurtleBaseConfig
from helpers.indicators import wilder_atr, donchian
from helpers.TurtleEngine import (TurtleEngine, DEC_ENTER, DEC_ADD,
                                  DEC_EXIT, DEC_SKIP)
from helpers.PositionSizer import PositionSizer


class StraTurtle(BaseCtaStrategy):

    def __init__(self, name: str, inst_cfg: InstrumentConfig,
                 base_cfg: TurtleBaseConfig):
        BaseCtaStrategy.__init__(self, name)
        self._inst = inst_cfg
        self._base = base_cfg
        self._engine = TurtleEngine(
            system=inst_cfg.system,
            stop_atr_mult=base_cfg.stop_atr_mult,
            pyramid_layers=base_cfg.pyramid_layers,
            pyramid_step_atr=base_cfg.pyramid_step_atr)
        self._sizer = PositionSizer(base_cfg.unit_var)
        self._volscale = None       # on_init 时从框架取
        self._pricetick = None
        self._last_veto_key = None  # 方向闸/手数否决日志去重

    # ------------------------------------------------------------------
    # 工厂
    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls, name: str, code: str, config_path: str):
        from helpers.ConfigLoader import ConfigLoader
        cfg = ConfigLoader.load(config_path)
        for inst in cfg.instruments:
            if inst.code == code:
                return cls(name, inst, cfg.base)
        raise ValueError(f"{code} 不在 {config_path} 的 instruments 中")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def on_init(self, context: CtaContext):
        code = self._inst.code
        p = self._base

        context.stra_prepare_bars(code, self._inst.period, p.bar_cnt,
                                  isMain=True)

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

        # === 状态恢复 ===
        raw = context.user_load_data(f"{self.name()}_engine", "", str)
        if raw:
            try:
                self._engine.restore(json.loads(raw))
                context.stra_log_text(
                    f"{self.name()} restored: dir={self._engine.direction}, "
                    f"layers={len(self._engine.layers)}")
            except (json.JSONDecodeError, TypeError) as e:
                context.stra_log_text(
                    f"{self.name()} state restore failed: {e}", level=2)

        # 持仓状态对账: 框架有持仓但引擎空仓 → 按单层重建
        pos = context.stra_get_position(code)
        if pos != 0 and self._engine.direction == 0:
            avgpx = context.stra_get_position_avgpx(code)
            self._engine.direction = 1 if pos > 0 else -1
            self._engine.layers = [[abs(int(pos)), avgpx]]
            context.stra_log_text(
                f"{self.name()} adopt orphan position {pos}@{avgpx}, "
                f"rebuilt as single layer", level=2)

        context.stra_log_text(
            f"StraTurtle inited: {code} {self._inst.period} "
            f"{self._inst.system} units={self._inst.units} "
            f"dir_gate={self._inst.direction}")

    def on_calculate(self, context: CtaContext):
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

        atr = wilder_atr(highs, lows, closes, p.atr_period)
        n = float(atr[-1])
        if not np.isfinite(n):
            return

        if self._inst.system == "S1":
            e_up, e_lo = donchian(highs, lows, p.s1_entry)
            x_up, x_lo = donchian(highs, lows, p.s1_exit)
        else:
            e_up, e_lo = donchian(highs, lows, p.s2_entry)
            x_up, x_lo = donchian(highs, lows, p.s2_exit)
        fs_up, fs_lo = donchian(highs, lows, p.s2_entry)

        channels = {
            'entry_upper': float(e_up[-1]), 'entry_lower': float(e_lo[-1]),
            'exit_upper': float(x_up[-1]), 'exit_lower': float(x_lo[-1]),
            'fs_upper': float(fs_up[-1]), 'fs_lower': float(fs_lo[-1]),
            'prev_entry_upper': float(e_up[-2]),
            'prev_entry_lower': float(e_lo[-2]),
            'prev_fs_upper': float(fs_up[-2]),
            'prev_fs_lower': float(fs_lo[-2]),
        }

        # === HOT 换月 re-anchor: 持仓不变时 avgpx 漂移 = 换月价差 ===
        self._reanchor_on_roll(context)

        decisions = self._engine.on_bar(
            float(highs[-1]), float(lows[-1]), float(closes[-1]),
            float(closes[-2]), n, channels)

        for dec in decisions:
            self._execute(context, dec, n)

        # === 图表更新 ===
        pos = context.stra_get_position(code)
        context.set_index_value(self._idx, "atr_n", n)
        context.set_index_value(self._idx, "position", float(pos))
        context.set_index_value(self._ch_idx, "entry_upper",
                                channels['entry_upper'])
        context.set_index_value(self._ch_idx, "entry_lower",
                                channels['entry_lower'])
        if self._engine.direction != 0 and self._engine.layers:
            context.set_index_value(self._ch_idx, "stop",
                                    self._engine._stop_price(n))

        # === 持久化 ===
        context.user_save_data(
            f"{self.name()}_engine", json.dumps(self._engine.snapshot()))

    def on_session_end(self, context: CtaContext, curTDate: int):
        e = self._engine
        if e.direction != 0:
            context.stra_log_text(
                f"{self.name()} session end: dir={e.direction}, "
                f"layers={len(e.layers)}, pos="
                f"{context.stra_get_position(self._inst.code)}")

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
    def _execute(self, context: CtaContext, dec: dict, n: float):
        code = self._inst.code
        kind = dec["decision"]

        if kind == DEC_SKIP:
            context.stra_log_text(
                f"{self.name()} S1 filter skip {dec['direction']}@"
                f"{dec['price']:.2f} (last winner), fail-safe armed")
            return

        if kind == DEC_EXIT:
            old_pos = context.stra_get_position(code)
            context.stra_set_position(code, 0, "Exit")
            self._engine.confirm_exit(dec["price"])
            mark = "sell" if old_pos > 0 else "buy"
            context.add_chart_mark(dec["price"], "exit", self.name())
            context.stra_log_text(
                f"{self.name()} exit {old_pos}@{dec['price']:.2f} "
                f"reason={dec['reason']}")
            return

        if kind == DEC_ENTER:
            d = dec["direction"]
            # 方向闸: 否决不提交引擎, 不影响 S1 过滤器
            if not self._inst.allows_direction(d):
                self._veto_log(context, f"dir{d}",
                               f"veto enter {d}@{dec['price']:.2f}: "
                               f"direction gate={self._inst.direction}")
                return
            lots = self._sizer.layer_lots(n, self._volscale,
                                          self._inst.units)
            if lots <= 0:
                self._veto_log(context, f"lot{d}",
                               f"veto enter {d}@{dec['price']:.2f}: "
                               f"lots=0 (N={n:.2f}×volscale={self._volscale} "
                               f"> budget)")
                return
            self._last_veto_key = None
            context.stra_set_position(code, d * lots, "Enter")
            fill = self._implied_fill(context, prev_pos=0, prev_avg=0.0,
                                      fallback=dec["price"])
            self._engine.confirm_enter(d, lots, fill)
            context.add_chart_mark(dec["price"],
                                   "buy" if d > 0 else "sell", self.name())
            fs = " [FAIL-SAFE]" if dec.get("failsafe") else ""
            context.stra_log_text(
                f"{self.name()} enter {'LONG' if d > 0 else 'SHORT'} "
                f"{lots} lots @~{fill:.2f}, N={n:.2f}, VaR/layer="
                f"{n * self._volscale * lots:.0f}{fs}")
            return

        if kind == DEC_ADD:
            d = self._engine.direction
            lots = self._sizer.layer_lots(n, self._volscale,
                                          self._inst.units)
            if lots <= 0:
                self._veto_log(context, f"add{len(self._engine.layers)}",
                               f"veto add @{dec['price']:.2f}: lots=0")
                return
            self._last_veto_key = None
            old_pos = context.stra_get_position(code)
            old_avg = context.stra_get_position_avgpx(code)
            context.stra_set_position(code, old_pos + d * lots, "Add")
            fill = self._implied_fill(context, prev_pos=old_pos,
                                      prev_avg=old_avg,
                                      fallback=dec["price"])
            self._engine.confirm_add(lots, fill)
            context.add_chart_mark(dec["price"],
                                   "buy" if d > 0 else "sell", self.name())
            context.stra_log_text(
                f"{self.name()} add layer{len(self._engine.layers)} "
                f"{lots} lots @~{fill:.2f}, N={n:.2f}")
            return

    def _veto_log(self, context: CtaContext, key: str, msg: str):
        """否决日志去重: 同类否决连续触发只记首次"""
        if self._last_veto_key == key:
            return
        self._last_veto_key = key
        context.stra_log_text(f"{self.name()} {msg}", level=2)

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

    def _reanchor_on_roll(self, context: CtaContext):
        """HOT 换月检测: 持仓不变但框架 avgpx 漂移 → 各层价格平移。

        框架 avgpx 带方向符号 (空头为负), 与层价格 (恒正) 比较时取绝对值。
        """
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
                f"{self.name()} hot roll detected: avgpx shift "
                f"{shift:+.2f}, layer prices re-anchored")
