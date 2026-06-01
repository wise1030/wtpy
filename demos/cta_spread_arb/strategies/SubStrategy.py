"""
SubStrategy — 子策略容器

v8架构: 使用schemas.py结构化配置(SubStrategyConfig/LegConfig)。
自包含对象, 封装单个品种组合的所有组件和流水线逻辑。
新增品种只需在YAML中加配置, 无需修改Python代码。

优化:
- 接收结构化配置对象(RiskConfig/SignalConfig/RegimeConfig)，不再isinstance(dict)兼容
- DiagLogger接入核心流水线(开仓/平仓/风控/因子异常)
- on_tick_check统一在process()中调用，on_tick回调不再推进状态机
"""
from FactorEngine import FactorEngine
from SpreadCalculator import SpreadCalculator
from ArbExecHelper import ArbExecHelper
from DataPipe import DataPipe
from RiskMgr import RiskMgr
from SignalGen import SignalGen
from StateManager import StateManager
from DiagLogger import DiagLogger
from schemas import SubStrategyConfig, LegConfig, RiskConfig, SignalConfig, RegimeConfig, ExecConfig


class SubStrategy:
    """
    子策略容器: 封装组件创建 + 流水线执行
    
    流水线: DataPipe → FactorEngine → SignalGen → RiskMgr → ArbExecHelper
    """

    def __init__(self, sub_config: SubStrategyConfig,
                 risk_cfg: RiskConfig, signal_cfg: SignalConfig,
                 adaptive_cfg: RegimeConfig, factor_config_path: str,
                 exec_cfg: ExecConfig = None,
                 is_backtest: bool = False):
        self.name = sub_config.name
        self._config = sub_config
        self._risk_cfg = risk_cfg      # 直接接收RiskConfig对象
        self._signal_cfg = signal_cfg   # 直接接收SignalConfig对象
        self._regime_cfg = adaptive_cfg # 直接接收RegimeConfig对象
        self._exec_cfg = exec_cfg or ExecConfig()  # P1-2: 执行参数配置
        self._is_backtest = is_backtest
        self._last_coint_p = 1.0  # P2-7: 缓存上一bar的coint_p

        # === 创建组件 ===
        # P0-1修复: 传入regime_cfg让FactorEngine从YAML配置构建权重映射
        self.fe = FactorEngine(factor_config_path, signal_cfg=signal_cfg,
                               regime_cfg=adaptive_cfg)
        self.sc = SpreadCalculator(sub_config.legs)

        # 创建RegimeDetector(需要regime配置)
        from regime_detector import create_regime_detector
        # P2-3修复: create_regime_detector现在直接接受RegimeConfig对象，无需手动转dict
        regime_detector = create_regime_detector(adaptive_cfg)

        # SignalConfig属性访问(不再dict.get)
        self.sg = SignalGen.from_config(
            signal_mode=sub_config.signal_mode,
            entry_threshold=signal_cfg.entry_threshold,
            exit_threshold=signal_cfg.exit_threshold,
            adaptive_cfg=adaptive_cfg,
            signal_cfg=signal_cfg,
            regime_detector=regime_detector,
            factor_engine=self.fe,
        )
        self.eh = ArbExecHelper.from_config(
            sub_config.legs, exec_cfg=self._exec_cfg, is_backtest=is_backtest)
        self.dp = DataPipe(sub_config, self.sc, factor_engine=self.fe,
                           exec_cfg=self._exec_cfg)  # P1-2: 传入ExecConfig
        self.rm = RiskMgr(risk_cfg)  # 直接传入RiskConfig对象
        self.sm = StateManager(self.name)
        # ARCH-6修复: 创建DiagLogger实例
        self._diag = DiagLogger(log_dir=f'./logs/{self.name}', to_console=False)

    @property
    def legs(self) -> list:
        """返回LegConfig列表(结构化)"""
        return self._config.legs

    @property
    def main_period(self) -> str:
        return self._config.main_period

    @property
    def main_bar_cnt(self) -> int:
        return self._config.main_bar_cnt

    @property
    def aux_periods(self) -> list:
        return self._config.aux_periods

    def on_init(self, context, is_main_holder: bool = True):
        """注册K线/指标/恢复状态

        v8.1: 多子策略并行支持
        - is_main_holder=True: 首腿用 isMain=True (CtaMocker 单 _main_key 限制)
        - is_main_holder=False: 所有 K 线都 isMain=False, 由 main_holder 驱动节拍
        """
        main_code = self.legs[0].code
        context.stra_prepare_bars(
            main_code, self.main_period, self.main_bar_cnt, isMain=is_main_holder)

        for leg in self.legs[1:]:
            context.stra_prepare_bars(
                leg.code, self.main_period, self.main_bar_cnt, isMain=False)

        for aux in self.aux_periods:
            for leg in self.legs:
                context.stra_prepare_bars(
                    leg.code, aux['period'], aux['bar_cnt'], isMain=False)

        for leg in self.legs:
            context.stra_sub_ticks(leg.code)

        # 注册指标
        idx_name = f"Spread_{self.name}"
        context.register_index(idx_name, idxType=1)
        context.register_index_line(idx_name, "spread", lineType=0)
        context.register_index_line(idx_name, "upper", lineType=0)
        context.register_index_line(idx_name, "lower", lineType=0)
        context.register_index_line(idx_name, "signal", lineType=1)
        context.add_index_baseline(idx_name, "signal", 0.0)
        # v4: regime诊断输出
        context.register_index_line(idx_name, "regime_val", lineType=1)  # 0=range,1=mr,2=trend
        context.register_index_line(idx_name, "regime_dir", lineType=1)  # -1/0/1
        context.register_index_line(idx_name, "regime_conf", lineType=0)  # 0~1

        fidx_name = f"Factors_{self.name}"
        context.register_index(fidx_name, idxType=1)
        for fname in self.fe.get_enabled_factor_names():
            context.register_index_line(fidx_name, fname, lineType=0)

        context.set_chart_kline(main_code, self.main_period)

        # 恢复持久化状态
        direction, last_signal_time = self.sm.load_all(context, self.rm)
        if direction != 0:
            self.eh.restore_state(direction)
            context.stra_log_text(
                f"{self.name} restored direction={direction}, state=opened")

    def process(self, context, cur_date: int):
        """执行完整流水线(v4): DataPipe → RegimeDetect → FactorEngine → Signal → Risk → Exec"""
        # 仓位同步: 检测实际仓位与状态机不一致的情况
        actual_has_pos = any(
            context.stra_get_position(leg.code) != 0 for leg in self.legs)
        if not actual_has_pos and self.eh.direction != 0:
            # 实际仓位归零但状态机未重置(如外部强平/程序重启)
            self.eh.reset()
            self.rm.on_position_cleared()
            self.sg.reset_trailing()  # v7: 重置浮盈追踪状态
        elif actual_has_pos and self.eh.direction == 0:
            # P0-2修复: 有仓位但状态机方向为0(如紧急平仓指令发出但某腿未成交)
            # 重置状态机让下一bar信号重新决策，避免仓位叠加
            self.eh.reset()
            context.stra_log_text(
                f"{self.name} position sync: has_pos but direction=0, resetting state machine",
                level=3)

        # 状态机推进(统一在process中执行，on_tick不再推进)
        self.eh.on_tick_check(context, self._config)

        # === DataPipe ===
        bar_data = self.dp.fetch(context, cur_date)
        if bar_data is None:
            return

        # === RegimeDetect(v4前置) ===
        regime_dict = self.sg.detect_regime(
            bar_data.spread, bar_data.aux_bars,
            coint_p=self._last_coint_p)  # P2-7: 用缓存值
        regime = regime_dict.get('regime', 'range')
        self.fe.apply_regime_weights(regime)

        # === FactorEngine(使用当前bar的regime权重) ===
        factor_signal = self.fe.calculate(
            bar_data.spread, prices=bar_data.prices, volumes=bar_data.volumes,
            aux_bars=bar_data.aux_bars, cur_date=cur_date,
            norm_coeffs=self.sc.get_norm_coeffs())

        # P2-7修复: 缓存当前bar的coint_p供下一bar的regime检测使用
        # P2-1修复: factor_signal现在是FactorOutput对象，用属性访问
        self._last_coint_p = factor_signal.coint_p

        # === SignalGen(使用已检测的regime，不重复检测) ===
        current_direction = self.eh.direction
        actual_direction = self.eh.infer_direction(context)
        if actual_direction != 0 and current_direction == 0:
            current_direction = actual_direction

        self.rm.update_hold_bars(current_direction)

        # 计算当前未实现盈亏(供ExitJudgeStep使用)
        unrealized_pnl = 0.0
        if current_direction != 0:
            for leg in self._config.legs:
                unrealized_pnl += context.stra_get_position_profit(leg.code)

        signal = self.sg.generate_with_regime(
            factor_signal, bar_data.aux_bars, current_direction,
            spread_trend_filter=bar_data.spread_trend_filter,
            spread=bar_data.spread,
            coint_p=factor_signal.coint_p,
            regime_dict=regime_dict,
            unrealized_pnl=unrealized_pnl,
            current_hour=context.stra_get_time() // 100)  # v7.2: 传入当前小时

        # 更新指标
        self._update_indicators(context, bar_data, factor_signal, signal)

        # === RiskMgr + 交易决策 ===
        self._execute_decision(context, bar_data, signal, factor_signal, current_direction)

        # 持久化
        now = context.stra_get_date() * 10000 + context.stra_get_time()
        self.sm.save_all(context, self.eh.direction, self.rm, now)

    def _update_indicators(self, context, bar_data, factor_signal, signal):
        """更新图表指标"""
        idx_name = f"Spread_{self.name}"
        context.set_index_value(idx_name, "spread", float(bar_data.spread[-1]))
        context.set_index_value(idx_name, "upper", self.sc.upper)
        context.set_index_value(idx_name, "lower", self.sc.lower)
        context.set_index_value(idx_name, "signal", signal.entry_signal)
        # v4: regime诊断输出
        regime_map = {'range': 0, 'mr': 1, 'trend': 2}
        context.set_index_value(idx_name, "regime_val",
                                float(regime_map.get(signal.regime, 0)))
        context.set_index_value(idx_name, "regime_dir",
                                float(signal.regime_direction))
        context.set_index_value(idx_name, "regime_conf",
                                float(signal.regime_confidence))

        fidx_name = f"Factors_{self.name}"
        for fname, fval in factor_signal.factor_scores.items():
            context.set_index_value(fidx_name, fname, float(fval))

        now = context.stra_get_date() * 10000 + context.stra_get_time()
        positions = {leg.code: context.stra_get_position(leg.code) for leg in self.legs}
        pnl = {leg.code: context.stra_get_position_profit(leg.code) for leg in self.legs}

        context.write_indicator(tag=self.name, time=now, data={
            "spread": float(bar_data.spread[-1]),
            "signal": signal.entry_signal,
            "direction": signal.direction,
            "regime": signal.regime,
            "factors": factor_signal.factor_scores,
            "total_score": factor_signal.total_score,
            "positions": positions,
            "pnl": pnl,
            "leg_status": self.eh.get_status(),
        })

    def _execute_decision(self, context, bar_data, signal, factor_signal, current_direction):
        """风控检查 + 交易决策 + DiagLogger记录

        冷却期策略(P2-4文档化):
        - 信号平仓(should_exit): 触发冷却期(cooldown_bars)，阻止立即重新开仓
        - 反转开仓(should_reverse): 同样触发冷却期，反转不豁免。
          原因: 反转本质是"平旧仓+开新仓"，两次交易应独立评估。
          反转后新仓位需要经过完整的EntryJudge→PersistFilter→cooldown→coint门控，
          避免在市场快速波动中频繁反转导致过度交易。
          如需反转豁免冷却期，在strategy_config.yaml中设置
          risk_control.reverse_cooldown_exempt: true(当前未实现此选项)。
        - 风控平仓(risk_result): 触发冷却期，阻止任何新开仓。
        """
        # ARCH-4修复: cooldown递减统一放开头
        self.rm.tick_cooldown()

        # 缓存未实现盈亏
        if current_direction != 0:
            cur_unrealized = sum(
                context.stra_get_position_profit(leg.code) for leg in self.legs)
            self.rm.cache_unrealized(cur_unrealized)

        # 风控检查
        risk_result = None
        if current_direction != 0 and not self.rm.closing:
            risk_result = self.rm.check(
                context, self._config, bar_data.spread, self.sc,
                coint_p=factor_signal.coint_p,
                current_direction=current_direction)

        if risk_result is not None:
            self.rm.on_risk_close()
            context.stra_log_text(
                f"{self.name} risk close, realized_pnl += {self.rm.realized_pnl:.2f}")
            self.eh.close_all(context, self._config, tag="RiskClose")
            context.stra_log_text(f"{self.name} risk triggered: {risk_result}")
            context.add_chart_mark(float(bar_data.spread[-1]), "risk_exit", self.name)
            # P1-4: DiagLogger记录风控触发
            self._diag.risk(self.name, rule='risk_close', detail=risk_result)
        elif signal.should_trade:
            if self.rm.is_in_cooldown():
                pass  # 冷却期中，不开仓
            else:
                self.eh.execute(context, self._config, signal)
                spread_val = float(bar_data.spread[-1])
                if signal.entry_signal > 0:
                    context.add_chart_mark(spread_val, "buy", self.name)
                else:
                    context.add_chart_mark(spread_val, "sell", self.name)
                # P1-4: DiagLogger记录开仓
                self._diag.trade(
                    self.name, action='open', direction=signal.direction,
                    signal=signal.entry_signal,
                    factors=factor_signal.factor_scores)
        elif signal.should_exit or signal.should_reverse:
            min_hold = self._risk_cfg.min_hold_bars
            # v7: pnl_trailing_exit豁免min_hold_bars — 浮盈追踪止盈是利润保护，
            # 不需要等待最短持仓时间(信号噪声过滤不适用于利润保护场景)
            if self.rm.hold_bars < min_hold and not signal.pnl_trailing_exit:
                pass  # 持仓时间不足，忽略信号平仓
            else:
                action = 'reverse' if signal.should_reverse else 'exit'
                # P2-4修复: 反转时检查reverse_cooldown_exempt选项
                if signal.should_reverse and getattr(self._risk_cfg, 'reverse_cooldown_exempt', False):
                    # 反转豁免冷却期: 不调用on_signal_close()设置cooldown
                    pass
                else:
                    self.rm.on_signal_close()
                context.stra_log_text(
                    f"{self.name} signal close, realized_pnl={self.rm.realized_pnl:.2f}")
                self.eh.execute(context, self._config, signal)
                context.add_chart_mark(float(bar_data.spread[-1]), "exit", self.name)
                # P1-4: DiagLogger记录平仓
                self._diag.trade(
                    self.name, action=action, direction=signal.direction,
                    signal=signal.entry_signal,
                    pnl=self.rm.realized_pnl)

    def on_session_begin(self, context):
        """交易日开始: 重置因子refit"""
        self.fe.reset_refit()

    def on_session_end(self, context):
        """交易日结束: 输出绩效 + flush诊断日志"""
        unrealized = sum(context.stra_get_position_profit(leg.code) for leg in self.legs)
        total_pnl = self.rm.realized_pnl + unrealized
        context.stra_log_text(
            f"{self.name} session end, realized={self.rm.realized_pnl:.2f}, "
            f"unrealized={unrealized:.2f}, total={total_pnl:.2f}")
        # ARCH-6修复: 回测结束时flush诊断日志缓冲区
        self._diag.flush_all()