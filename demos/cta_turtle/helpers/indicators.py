"""海龟指标: Wilder ATR + Donchian 通道 (numpy/lfilter 向量化)

ATR 尺度说明(与 cta_spread_arb 和尺度递推区分):
海龟 N 使用均值尺度递推 atr[i] = (atr[i-1]*(n-1) + TR[i]) / n,
即 alpha=1/n 的 EMA 形式 → lfilter(b=[1/n], a=[1, -(1-1/n)]),
种子 atr[n-1] = mean(TR[:n]) (经典海龟首个 N 用简单平均)。
"""
import numpy as np
from scipy.signal import lfilter


def true_range(highs: np.ndarray, lows: np.ndarray,
               closes: np.ndarray) -> np.ndarray:
    """TR[i] = max(H-L, |H-C[i-1]|, |L-C[i-1]|); TR[0] = H[0]-L[0]"""
    highs = np.asarray(highs, dtype=float)
    lows = np.asarray(lows, dtype=float)
    closes = np.asarray(closes, dtype=float)
    hl = highs - lows
    hc = np.abs(highs[1:] - closes[:-1])
    lc = np.abs(lows[1:] - closes[:-1])
    tr = np.empty(len(highs))
    tr[0] = hl[0]
    tr[1:] = np.maximum(hl[1:], np.maximum(hc, lc))
    return tr


def wilder_atr(highs: np.ndarray, lows: np.ndarray,
               closes: np.ndarray, period: int = 20) -> np.ndarray:
    """Wilder 平滑 ATR (海龟 N)。前 period-1 个值为 NaN。

    种子: atr[period-1] = mean(TR[:period])
    递推: atr[i] = (atr[i-1]*(period-1) + TR[i]) / period
    """
    highs = np.asarray(highs, dtype=float)
    lows = np.asarray(lows, dtype=float)
    closes = np.asarray(closes, dtype=float)
    n = len(highs)
    out = np.full(n, np.nan)
    if n < period:
        return out
    tr = true_range(highs, lows, closes)
    seed = float(np.mean(tr[:period]))
    alpha = 1.0 / period
    zi = [seed * (1.0 - alpha)]  # lfilter 状态: y[i-1]*(1-alpha)
    tail, _ = lfilter([alpha], [1.0, -(1.0 - alpha)], tr[period:], zi=zi)
    out[period - 1] = seed
    out[period:] = tail
    return out


def donchian(highs: np.ndarray, lows: np.ndarray,
             period: int) -> tuple:
    """Donchian 通道 (不含当前 bar, 用前 period 根)。

    upper[i] = max(highs[i-period:i]), lower[i] = min(lows[i-period:i])
    前 period 个值为 NaN。
    """
    highs = np.asarray(highs, dtype=float)
    lows = np.asarray(lows, dtype=float)
    n = len(highs)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    if n <= period:
        return upper, lower
    hwin = np.lib.stride_tricks.sliding_window_view(highs, period)
    lwin = np.lib.stride_tricks.sliding_window_view(lows, period)
    upper[period:] = hwin.max(axis=1)[:-1]
    lower[period:] = lwin.min(axis=1)[:-1]
    return upper, lower
