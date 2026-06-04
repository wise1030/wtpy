"""
ArbCore — 套利核心流水线

v10架构: 从SubStrategy重命名, 去除多sub/PositionRouter概念。
单策略实例只包含一个ArbCore(单个品种组合), 多品种组合用多策略实例。

自包含对象, 封装单个品种组合的所有组件和流水线逻辑:
    DataPipe → FactorEngine → SignalGen → RiskMgr → ArbExecHelper

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
from ArbCoreLifecycle import ArbCoreLifecycle
from schemas import ArbCoreConfig, LegConfig, RiskConfig, SignalConfig, RegimeConfig, ExecConfig
from dataclasses import dataclass


@dataclass
class ArbDecision:
    """单个品种组合的计算决策结果"""
    bar_data: object = None
    signal: object = None
    factor_signal: object = None
    current_direction: int = 0


class ArbCore:
    """
    套利核心流水线: 封装组件创建 + 流水线执行

    流水线: DataPipe → FactorEngine → SignalGen → RiskMgr → ArbExecHelper
    """

    def __init__(self, core_config: ArbCoreConfig,
                 risk_cfg: RiskConfig, signal_cfg: SignalConfig,
                 adaptive_cfg: RegimeConfig, factor_config_path: str,
                 exec_cfg: ExecConfig = None,
                 is_backtest: bool = False):
        self.name = core_config.name
        self._config = core_config
        self._risk_cfg = risk_cfg
        self._signal_cfg = signal_cfg
        self._regime_cfg = adaptive_cfg
        self._exec_cfg = exec_cfg or ExecConfig()
        self._is_backtest = is_backtest
        self._last_coint_p = 1.0  # 缓存上一bar的coint_p
        self._current_profile = None  # 当前生效的param_profile名
        self._daily_spread_history = []  # 实盘增量: 日线价差序列
        self._last_profile_recheck_date = 0  # 上次重算profile的日期

        # === 创建组件 ===
        self.fe = FactorEngine(factor_config_path, signal_cfg=signal_cfg,
                               regime_cfg=adaptive_cfg)
        self.sc = SpreadCalculator(core_config.legs)

        # 创建RegimeDetector
        from regime_detector import create_regime_detector
        regime_detector = create_regime_detector(adaptive_cfg)

        self.sg = SignalGen.from_config(
            signal_mode=core_config.signal_mode,
            entry_threshold=signal_cfg.entry_threshold,
            exit_threshold=signal_cfg.exit_threshold,
            adaptive_cfg=adaptive_cfg,
            signal_cfg=signal_cfg,
            regime_detector=regime_detector,
            factor_engine=self.fe,
        )
        self.eh = ArbExecHelper.from_config(
            core_config.legs, exec_cfg=self._exec_cfg, is_backtest=is_backtest)
        self.dp = DataPipe(core_config, self.sc, factor_engine=self.fe,
                           exec_cfg=self._exec_cfg)
        self.rm = RiskMgr(risk_cfg)
        self.sm = StateManager(self.name)
        self._diag = DiagLogger(log_dir=f'./logs/{self.name}', to_console=False)

        # P4M-1: WT 生命周期回调适配器 (组合关系, 不改变对外 API)
        self._lifecycle = ArbCoreLifecycle(self)
        # R2-A: 启动期 EMERGENCY 状态占位 (由 lifecycle.on_init 填充)
        self._startup_emergency = None

    @property
    def legs(self) -> list:
        """返回LegConfig列表"""
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
        """WT 回调入口 (facade) — 见 ArbCoreLifecycle.on_init。

        - is_main_holder=True: 首腿用 isMain=True (CtaMocker 单 _main_key 限制)
        - is_main_holder=False: 所有 K 线都 isMain=False
        """
        self._lifecycle.on_init(context, is_main_holder=is_main_holder)

    def process(self, context, cur_date: int):
        """主入口: sync → compute → execute → persist"""
        self.sync_state(context)
        self._recheck_profile(context, cur_date)
        decision = self.compute_decision(context, cur_date)
        if decision is None:
            return
        self.execute_decision(context, decision)
        self.persist_state(context)

    def sync_state(self, context):
        """同步执行状态机与当前仓位"""
        get_position = context.stra_get_position
        # 仓位同步: 检测实际仓位与状态机不一致的情况
        actual_has_pos = any(get_position(leg.code) != 0 for leg in self.legs)
        if not actual_has_pos and self.eh.direction != 0:
            self.eh.reset()
            self.rm.on_position_cleared()
            self.sg.reset_trailing()
        elif actual_has_pos and self.eh.direction == 0:
            self.eh.reset()
            context.stra_log_text(
                f"{self.name} position sync: has_pos but direction=0, resetting state machine",
                level=3)

        # 状态机推进(统一在process中执行)
        self.eh.on_tick_check(context, self._config)

        # P0-4: opening/closing超时保护
        self.eh.check_state_timeout(context, self._config)

    def _recheck_profile(self, context, cur_date: int):
        """实盘模式: 每隔N天用累积日线价差重算 auto_profile.

        仅在非回测模式且有 auto_profile 配置时生效。
        回测模式在 __init__ 时一次性确定, 不动态调整。
        """
        if self._is_backtest:
            return
        if not self._current_profile and not hasattr(self, '_auto_profile_cfg'):
            return

        # P0-2修复: 使用 dp.last_spread (原 hasattr(dp,'spread') 永远为False)
        if self.dp.last_spread is not None and len(self.dp.last_spread) > 0:
            latest_spread = float(self.dp.last_spread[-1])
            # 每日只追加一次(以cur_date变化为判据)
            if cur_date != self._last_profile_recheck_date:
                self._daily_spread_history.append(latest_spread)
                # 保留最近250天
                if len(self._daily_spread_history) > 250:
                    self._daily_spread_history = self._daily_spread_history[-250:]

        # 每10个交易日重算一次
        if len(self._daily_spread_history) < 60:
            return

        # P0-2修复: 原条件 `cur_date - self._last_profile_recheck_date > 0` 永远为True
        # (因为上面刚在同bar追加了数据但还没更新_last_profile_recheck_date)
        # 正确逻辑: 只在新交易日且非今日已重算时执行
        if cur_date == self._last_profile_recheck_date:
            return  # 今日已重算，跳过

        # 更新重算日期(在重算前标记，防止同日重复)
        self._last_profile_recheck_date = cur_date

        from auto_profile import auto_select_from_spread
        profile, hurst, info = auto_select_from_spread(self._daily_spread_history)

        if profile != self._current_profile:
            old_profile = self._current_profile
            self._current_profile = profile
            context.stra_log_text(
                f'[auto_profile] {self.name}: profile changed '
                f'{old_profile} → {profile} ({info})')
            # 动态调整信号参数(仅调整entry_threshold)
            if profile == 'trending_contract':
                self.sg.entry_threshold = min(self.sg.entry_threshold * 1.2, 0.8)
            elif profile == 'mr_contract':
                self.sg.entry_threshold = max(self.sg.entry_threshold * 0.8, 0.3)

    def compute_decision(self, context, cur_date: int):
        """计算本bar的决策对象"""
        # === DataPipe ===
        bar_data = self.dp.fetch(context, cur_date)
        if bar_data is None:
            return None

        # === RegimeDetect ===
        regime_dict = self.sg.detect_regime(
            bar_data.spread, bar_data.aux_bars,
            coint_p=self._last_coint_p)
        regime = regime_dict.get('regime', 'range')
        self.fe.apply_regime_weights(regime)

        # === FactorEngine ===
        factor_signal = self.fe.calculate(
            bar_data.spread, prices=bar_data.prices, volumes=bar_data.volumes,
            aux_bars=bar_data.aux_bars, cur_date=cur_date,
            norm_coeffs=self.sc.get_norm_coeffs())

        self._last_coint_p = factor_signal.coint_p

        # 因子异常诊断: 记录失败因子
        if factor_signal.failed_factors:
            for fname in factor_signal.failed_factors:
                self._diag.factor(
                    factor_name=fname, event='calc_failed',
                    detail=f'{self.name}: coint_p={factor_signal.coint_p:.4f}')

        # 协整退化诊断: coint_p超出安全阈值且有持仓
        coint_force_p = self._risk_cfg.coint_force_exit_p
        if factor_signal.coint_p >= coint_force_p and self.eh.direction != 0:
            self._diag.factor(
                factor_name='coint_residual', event='degraded',
                detail=f'{self.name}: coint_p={factor_signal.coint_p:.4f} >= force_exit_p={coint_force_p}')

        # === SignalGen ===
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
            current_hour=context.stra_get_time() // 100)

        # 更新指标
        self._update_indicators(context, bar_data, factor_signal, signal)

        return ArbDecision(
            bar_data=bar_data,
            signal=signal,
            factor_signal=factor_signal,
            current_direction=current_direction,
        )

    def execute_decision(self, context, decision: ArbDecision):
        """执行下单决策"""
        self._execute_decision(
            context, decision.bar_data, decision.signal,
            decision.factor_signal, decision.current_direction)

    def persist_state(self, context):
        """持久化当前状态"""
        now = context.stra_get_date() * 10000 + context.stra_get_time()
        self.sm.save_all(context, self.eh.direction, self.rm, now)
        # P0-9: 持久化执行状态
        self.sm.save_exec_state(context, self.eh)

    def _update_indicators(self, context, bar_data, factor_signal, signal):
        """更新图表指标"""
        idx_name = f"Spread_{self.name}"
        context.set_index_value(idx_name, "spread", float(bar_data.spread[-1]))
        context.set_index_value(idx_name, "upper", self.sc.upper)
        context.set_index_value(idx_name, "lower", self.sc.lower)
        context.set_index_value(idx_name, "signal", signal.entry_signal)
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

        冷却期策略:
        - 信号平仓(should_exit): 触发冷却期(cooldown_bars)
        - 反转开仓(should_reverse): 同样触发冷却期
        - 风控平仓(risk_result): 触发冷却期，阻止任何新开仓
        """
        # cooldown递减统一放开头
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
            self.eh.close_all(context, self._config, tag="RC")
            context.stra_log_text(f"{self.name} risk triggered: {risk_result}")
            context.add_chart_mark(float(bar_data.spread[-1]), "risk_exit", self.name)
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
                self._diag.trade(
                    self.name, action='open', direction=signal.direction,
                    signal=signal.entry_signal,
                    factors=factor_signal.factor_scores)
        elif signal.should_exit or signal.should_reverse:
            min_hold = self._risk_cfg.min_hold_bars
            if self.rm.hold_bars < min_hold and not signal.pnl_trailing_exit:
                pass  # 持仓时间不足，忽略信号平仓
            else:
                action = 'reverse' if signal.should_reverse else 'exit'
                if signal.should_reverse and getattr(self._risk_cfg, 'reverse_cooldown_exempt', False):
                    pass
                else:
                    self.rm.on_signal_close()
                context.stra_log_text(
                    f"{self.name} signal close, realized_pnl={self.rm.realized_pnl:.2f}")
                self.eh.execute(context, self._config, signal)
                context.add_chart_mark(float(bar_data.spread[-1]), "exit", self.name)
                self._diag.trade(
                    self.name, action=action, direction=signal.direction,
                    signal=signal.entry_signal,
                    pnl=self.rm.realized_pnl)

    def on_session_begin(self, context):
        """WT 回调入口 (facade) — 见 ArbCoreLifecycle.on_session_begin。"""
        self._lifecycle.on_session_begin(context)

    def on_session_end(self, context):
        """WT 回调入口 (facade) — 见 ArbCoreLifecycle.on_session_end。"""
        self._lifecycle.on_session_end(context)
