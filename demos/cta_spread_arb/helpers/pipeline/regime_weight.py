"""RegimeWeightStep — Step 2: Regime驱动的权重调整 + 趋势/MR确认

替代原CointGateStep + MomentumConfirmStep:
- trend regime: 检查趋势因子是否确认(非MR确认)
- mr regime: 检查协整是否显著(原CointGate逻辑)
- range regime: 额外衰减信号
"""
import numpy as np
from .context import SignalContext


class RegimeWeightStep:

    def __init__(self, coint_decay: float = 0.7,
                 momentum_decay: float = 0.7):
        self._coint_decay = coint_decay
        self._momentum_decay = momentum_decay

    def process(self, ctx: SignalContext) -> SignalContext:
        if ctx.regime == 'trend':
            self._trend_confirm(ctx)
        elif ctx.regime == 'mr':
            self._mr_confirm(ctx)
        else:
            self._range_confirm(ctx)
        return ctx

    def _trend_confirm(self, ctx: SignalContext):
        """趋势regime确认: 检查趋势因子是否支持信号方向"""
        # 用direction_type动态判断momentum类因子，不再硬编码因子名
        momentum_scores = []
        for name, score in ctx.factor_scores.items():
            dt = ctx.factor_direction_types.get(name, 'neutral')
            # P1-2文档化: 阈值0.1是"动量否决"而非"动量确认"
            # rank_normalize后因子值在[-1,1]范围，0.1是噪声过滤门槛
            # abs(score)>0.1的动量因子才参与确认判断，低于0.1视为噪声忽略
            # mr_confirmed默认True(不否决)，只有动量与信号反向时才设False(否决)
            if dt == 'momentum' and abs(score) > 0.1:
                momentum_scores.append(score)

        if momentum_scores:
            avg_momentum = np.mean(momentum_scores)
            signal_sign = np.sign(ctx.entry_signal)
            momentum_sign = np.sign(avg_momentum)
            # 趋势因子与信号同向 → 确认
            if signal_sign != 0 and momentum_sign != 0 and signal_sign == momentum_sign:
                ctx.trend_confirmed = True
                ctx.coint_decay_val = 1.0  # 趋势regime不依赖协整
            else:
                ctx.trend_confirmed = False
                ctx.coint_decay_val = 0.7  # 趋势因子不确认时轻度衰减
        else:
            ctx.trend_confirmed = True
            ctx.coint_decay_val = 1.0

        ctx.mr_confirmed = True  # 趋势regime不检查MR

    def _mr_confirm(self, ctx: SignalContext):
        """MR regime确认: 检查协整显著性和动量确认"""
        # 协整门控(原CointGateStep逻辑)
        if ctx.coint_p >= 0.10:
            ctx.coint_decay_val = self._coint_decay ** 2
        elif ctx.coint_p > 0.05:
            ctx.coint_decay_val = self._coint_decay
        else:
            ctx.coint_decay_val = 1.0

        # 动量确认(用direction_type判断，不再硬编码因子名)
        momentum_score = 0.0
        momentum_count = 0
        for name, score in ctx.factor_scores.items():
            dt = ctx.factor_direction_types.get(name, 'neutral')
            if dt == 'momentum' and abs(score) > 0.1:
                momentum_score += score
                momentum_count += 1
        if momentum_count > 0:
            momentum_score /= momentum_count
        if abs(momentum_score) > 0.1:
            signal_sign = np.sign(ctx.entry_signal)
            momentum_sign = np.sign(momentum_score)
            if signal_sign != 0 and momentum_sign != 0 and signal_sign != momentum_sign:
                ctx.mr_confirmed = False
            else:
                ctx.mr_confirmed = True
        else:
            ctx.mr_confirmed = True

        ctx.trend_confirmed = True  # MR regime不检查趋势

    def _range_confirm(self, ctx: SignalContext):
        """range regime: v4不再额外衰减"""
        ctx.coint_decay_val = 1.0  # v4: 不额外衰减，避免三重衰减
        ctx.trend_confirmed = True
        ctx.mr_confirmed = True