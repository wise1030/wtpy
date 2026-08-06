"""RegimeDispatchStep — Step 1: Regime信号分派(v4)

v4: 不再内部检测regime。regime由外部(ArbCore)前置检测并注入SignalContext。
P0-2修复: 不再做trend_aligned_boost/trend_counter_decay/range_signal_decay调整。
信号调整统一由FactorEngine.apply_regime_weights()处理，避免双重调整。
本Step只负责:
- 固定模式下覆盖regime
- 将total_score传递为entry_signal(不做额外乘法)
"""
from .context import SignalContext


class RegimeDispatchStep:

    def __init__(self, signal_mode: str = 'adaptive'):
        self._signal_mode = signal_mode
        # C-4(2026-07): 删除 _last_regime/_last_regime_direction 镜像状态 —
        # regime 单一来源归 SignalGen._last_regime(detect_regime 更新),
        # 原三处缓存(Detector/SignalGen/DispatchStep)易漂移

    def process(self, ctx: SignalContext) -> SignalContext:
        total_score = ctx.total_score

        # v4: regime已由外部注入SignalContext，不再内部检测
        # 只在固定模式下覆盖regime
        if self._signal_mode == 'trend':
            ctx.regime = 'trend'
        elif self._signal_mode == 'mean_revert':
            ctx.regime = 'mr'
        elif self._signal_mode not in ('adaptive',):
            ctx.regime = 'range'

        # P0-2修复: 直接传递total_score，不再做boost/decay
        # 原问题: FactorEngine.apply_regime_weights()已调整因子权重(1.5x/0.5x/0.3x)，
        #   RegimeDispatchStep又做trend_aligned_boost(1.3x)，导致trend regime
        #   顺势信号被双重放大1.5*1.3=1.95倍，偏离配置意图。
        # 修复: 信号调整统一由FactorEngine处理，本Step只做regime分派。
        ctx.entry_signal = total_score

        return ctx