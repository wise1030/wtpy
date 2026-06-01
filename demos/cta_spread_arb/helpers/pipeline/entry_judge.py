"""EntryJudgeStep — Step 4: 入场判断

信号衰减 + 入场阈值 + should_trade

方向决策统一: direction = sign(final_signal)
- final_signal > entry_threshold → 开正向套利
- final_signal < -entry_threshold → 开反向套利

趋势regime下逆势信号: 不硬阻挡，而是衰减信号强度
(乘以trend_counter_decay_factor)，让阈值自然过滤。
"""
import numpy as np
from .context import SignalContext


class EntryJudgeStep:

    def __init__(self, entry_threshold: float = 0.5,
                 short_entry_multiplier: float = 1.0,
                 trend_counter_decay_factor: float = 0.5,
                 coint_block_entry_p: float = 0.05,
                 # v7.2: 时段过滤 — 屏蔽低质量开仓时段
                 blocked_hours: str = '14'):
        self._entry_threshold = entry_threshold
        self._short_entry_multiplier = min(short_entry_multiplier, 1.0)
        self._trend_counter_decay_factor = trend_counter_decay_factor
        self._coint_block_entry_p = coint_block_entry_p
        # v7.2: 解析屏蔽时段
        self._blocked_hours = set()
        if blocked_hours:
            for h in blocked_hours.split(','):
                h = h.strip()
                if h.isdigit():
                    self._blocked_hours.add(int(h))

    def process(self, ctx: SignalContext) -> SignalContext:
        # 1. 应用衰减(v4: 只在确认失败时衰减)
        final_signal = ctx.entry_signal
        if ctx.regime == 'trend' and not ctx.trend_confirmed:
            final_signal = final_signal * ctx.coint_decay_val
        elif ctx.regime == 'mr' and not ctx.mr_confirmed:
            final_signal = final_signal * 0.7  # MR动量不确认时衰减

        # 2. 趋势regime下逆势信号衰减(替代硬阻挡)
        trend_blocked = False
        if ctx.regime == 'trend' and ctx.regime_direction != 0:
            signal_dir = np.sign(final_signal)
            if signal_dir != 0 and signal_dir != ctx.regime_direction:
                final_signal = final_signal * self._trend_counter_decay_factor
                trend_blocked = True

        ctx.final_signal = final_signal
        ctx.trend_blocked = trend_blocked

        # 3. 入场阈值(做空需更强信号)
        effective_threshold = self._entry_threshold
        if np.sign(final_signal) < 0:
            effective_threshold *= self._short_entry_multiplier

        # 4. 协整门控: coint_p>=阈值且空仓时阻止开仓(MR/range regime专用)
        # P1-1修复: 阈值从参数读取，不再硬编码0.05
        coint_blocked = False
        if ctx.coint_p >= self._coint_block_entry_p and ctx.current_direction == 0 and ctx.regime != 'trend':
            coint_blocked = True

        # 5. 价差趋势过滤(MR regime下信号方向与价差趋势同向时阻止)
        trend_filter_blocked = False
        if ctx.spread_trend_filter != 0 and ctx.regime == 'mr':
            signal_dir = np.sign(final_signal)
            if signal_dir != 0 and signal_dir == ctx.spread_trend_filter:
                trend_filter_blocked = True

        # 6. 入场判断
        ctx.should_trade = (abs(final_signal) > effective_threshold
                           and ctx.current_direction == 0
                           and ctx.signal_persistent
                           and not coint_blocked
                           and not trend_filter_blocked)

        # v7.2: 时段过滤 — 屏蔽低质量开仓时段
        if ctx.should_trade and self._blocked_hours and hasattr(ctx, 'current_hour'):
            if ctx.current_hour in self._blocked_hours:
                ctx.should_trade = False

        return ctx
