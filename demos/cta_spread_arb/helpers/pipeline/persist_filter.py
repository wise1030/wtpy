"""PersistFilterStep — Step 3: 信号持续性过滤

信号方向需连续N bar一致。
persist_bars=1时不过滤(默认)，=2时需连续2bar同向，以此类推。
强信号(>1.5x阈值)跳过持续性检查。
"""
import numpy as np
from .context import SignalContext


class PersistFilterStep:

    def __init__(self, signal_persist_bars: int = 1,
                 entry_threshold: float = 0.5):
        self._signal_persist_bars = signal_persist_bars
        self._entry_threshold = entry_threshold
        from collections import deque
        self._signal_history = deque(maxlen=max(signal_persist_bars, 1) + 1)

    def process(self, ctx: SignalContext) -> SignalContext:
        strong_signal = abs(ctx.entry_signal) > self._entry_threshold * 1.5
        ctx.strong_signal = strong_signal

        self._signal_history.append(np.sign(ctx.entry_signal))

        signal_persistent = True
        if not strong_signal and self._signal_persist_bars > 1:
            if len(self._signal_history) < self._signal_persist_bars:
                signal_persistent = False
            else:
                recent = list(self._signal_history)[-self._signal_persist_bars:]
                # 连续N bar方向一致且非零
                if len(set(recent)) != 1 or recent[0] == 0:
                    signal_persistent = False

        ctx.signal_persistent = signal_persistent
        return ctx

    def reset_history(self):
        self._signal_history.clear()