"""SignalPipeline — 信号生成管道(v4)

组合多个Step，顺序执行。

v4: regime检测由外部(ArbCore)前置完成，通过regime_dict传入。
RegimeDispatchStep不再内部调用RegimeDetector，直接使用传入的regime。
"""
import numpy as np
from .context import SignalContext
from .regime_dispatch import RegimeDispatchStep
from .regime_weight import RegimeWeightStep
from .persist_filter import PersistFilterStep
from .entry_judge import EntryJudgeStep
from .exit_judge import ExitJudgeStep


class SignalPipeline:
    """信号生成管道(v4) — 组合多个Step，顺序执行"""

    def __init__(self, steps: list):
        self._steps = steps

    def generate(self, factor_signal: dict, aux_bars: dict,
                 current_direction: int = 0,
                 spread_trend_filter: int = 0,
                 spread: np.ndarray = None,
                 coint_p: float = 1.0,
                 regime_dict: dict = None,
                 unrealized_pnl: float = 0.0,
                 current_hour: int = 0) -> dict:
        """生成信号 — regime_dict由外部传入(避免重复检测)"""
        ctx = SignalContext(
            factor_signal=factor_signal,
            aux_bars=aux_bars,
            current_direction=current_direction,
            spread_trend_filter=spread_trend_filter,
            spread=spread,
            coint_p=coint_p,
            unrealized_pnl=unrealized_pnl,
            current_hour=current_hour,
        )
        # 将外部regime结果注入context
        if regime_dict is not None:
            ctx.regime = regime_dict.get('regime', 'range')
            ctx.regime_confidence = regime_dict.get('confidence', 0.0)
            ctx.regime_direction = regime_dict.get('direction', 0)
        for step in self._steps:
            ctx = step.process(ctx)
        return ctx.to_signal_result()

    # 兼容旧接口
    def generate_with_regime(self, factor_signal: dict, aux_bars: dict,
                             current_direction: int = 0,
                             spread_trend_filter: int = 0,
                             spread: np.ndarray = None,
                             coint_p: float = 1.0,
                             regime_dict: dict = None,
                             unrealized_pnl: float = 0.0,
                             current_hour: int = 0) -> dict:
        """同generate() — 保持接口一致性"""
        return self.generate(
            factor_signal, aux_bars, current_direction,
            spread_trend_filter=spread_trend_filter,
            spread=spread, coint_p=coint_p,
            regime_dict=regime_dict,
            unrealized_pnl=unrealized_pnl,
            current_hour=current_hour)

    @property
    def regime(self) -> str:
        for step in self._steps:
            if isinstance(step, RegimeDispatchStep):
                return step.regime
        return 'unknown'

    @property
    def regime_direction(self) -> int:
        for step in self._steps:
            if isinstance(step, RegimeDispatchStep):
                return step.regime_direction
        return 0

    def reset_trailing(self):
        for step in self._steps:
            if isinstance(step, (EntryJudgeStep, ExitJudgeStep)):
                if hasattr(step, 'reset_trailing'):
                    step.reset_trailing()
            if isinstance(step, PersistFilterStep):
                step.reset_history()

    @staticmethod
    def from_config(signal_mode, entry_threshold, exit_threshold,
                    adaptive_cfg, signal_cfg):
        """从配置创建Pipeline

        REG-1修复: signal_cfg可能是SignalConfig dataclass或dict，统一处理
        A1: 移除未使用的 regime_detector/factor_engine 死参数。
        """
        # REG-1修复: 统一dataclass和dict访问
        def _cfg_get(cfg, key, default):
            if hasattr(cfg, key):
                return getattr(cfg, key, default)
            elif isinstance(cfg, dict):
                return cfg.get(key, default)
            return default

        persist_step = PersistFilterStep(
            signal_persist_bars=_cfg_get(signal_cfg, 'signal_persist_bars', 1),
            entry_threshold=entry_threshold,
        )
        steps = [
            RegimeDispatchStep(
                signal_mode=signal_mode,
            ),
            RegimeWeightStep(
                coint_decay=_cfg_get(signal_cfg, 'coint_decay', 0.7),
                momentum_decay=_cfg_get(signal_cfg, 'momentum_decay', 0.7),
            ),
            persist_step,
            EntryJudgeStep(
                entry_threshold=entry_threshold,
                short_entry_multiplier=_cfg_get(signal_cfg, 'short_entry_multiplier', 1.0),
                trend_counter_decay_factor=_cfg_get(signal_cfg, 'trend_counter_decay_factor', 0.5),
                coint_block_entry_p=_cfg_get(signal_cfg, 'coint_block_entry_p', 0.05),
                # v7.2: 时段过滤
                blocked_hours=_cfg_get(signal_cfg, 'blocked_hours', '14'),
            ),
            ExitJudgeStep(
                entry_threshold=entry_threshold,
                exit_threshold=exit_threshold,
                trailing_exit_bars=_cfg_get(signal_cfg, 'trailing_exit_bars', 6),
                trailing_decay_ratio=_cfg_get(signal_cfg, 'trailing_decay_ratio', 0.3),
                profit_protect_peak=_cfg_get(signal_cfg, 'profit_protect_peak', 0.7),
                profit_protect_floor=_cfg_get(signal_cfg, 'profit_protect_floor', 0.35),
                trend_trailing_ratio=_cfg_get(signal_cfg, 'trend_trailing_ratio', 0.5),
                trend_trailing_bars=_cfg_get(signal_cfg, 'trend_trailing_bars', 3),
                range_exit_threshold=_cfg_get(signal_cfg, 'range_exit_threshold', 0.4),
                short_entry_multiplier=_cfg_get(signal_cfg, 'short_entry_multiplier', 1.0),
                # v7: 浮盈追踪止盈参数
                pnl_trailing_threshold=_cfg_get(signal_cfg, 'pnl_trailing_threshold', 500.0),
                pnl_trailing_tight=_cfg_get(signal_cfg, 'pnl_trailing_tight', 0.2),
                pnl_trailing_medium=_cfg_get(signal_cfg, 'pnl_trailing_medium', 0.3),
                pnl_trailing_wide=_cfg_get(signal_cfg, 'pnl_trailing_wide', 0.4),
            ),
        ]
        return SignalPipeline(steps)