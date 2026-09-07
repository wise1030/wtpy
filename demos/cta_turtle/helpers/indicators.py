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


def wilder_adx(highs: np.ndarray, lows: np.ndarray,
               closes: np.ndarray, period: int = 14) -> tuple:
    """Wilder DMI/ADX。返回 (+DI, -DI, ADX) 三条序列。

    平滑口径与海龟 N 一致 (alpha=1/period 的 EMA):
    - DI 平滑种子 = 前 period 个分量的简单和 (Wilder 原版口径, DI 比值中和可约去)
    - ADX 为 DX 的同型平滑, 种子 = 首个 period 个 DX 的均值
    前 period-1 个 DI、前 2*period-2 个 ADX 为 NaN。
    """
    highs = np.asarray(highs, dtype=float)
    lows = np.asarray(lows, dtype=float)
    closes = np.asarray(closes, dtype=float)
    n = len(highs)
    di_p_out = np.full(n, np.nan)
    di_m_out = np.full(n, np.nan)
    adx_out = np.full(n, np.nan)
    if n < period + 1:
        return di_p_out, di_m_out, adx_out

    up = highs[1:] - highs[:-1]
    dn = lows[:-1] - lows[1:]
    # DM 自 bar1 起有定义: 前导补零对齐 TR 索引 (bar0 计 0, 与经典口径一致)
    plus_dm = np.concatenate(([0.0], np.where((up > dn) & (up > 0), up, 0.0)))
    minus_dm = np.concatenate(([0.0], np.where((dn > up) & (dn > 0), dn, 0.0)))
    tr = true_range(highs, lows, closes)

    alpha = 1.0 / period

    def _smooth_sum(x: np.ndarray) -> np.ndarray:
        """种子=前 period 项之和, 之后 S[i] = S[i-1] + (x[i]-S[i-1])/period"""
        out_full = np.full(n, np.nan)
        seed = float(np.sum(x[:period]))
        tail, _ = lfilter([alpha], [1.0, -(1.0 - alpha)], x[period:],
                          zi=[seed * (1.0 - alpha)])
        out_full[period - 1] = seed
        out_full[period:] = tail
        return out_full

    tr_s = _smooth_sum(tr)
    pdm_s = _smooth_sum(plus_dm)
    mdm_s = _smooth_sum(minus_dm)

    with np.errstate(divide='ignore', invalid='ignore'):
        di_p = np.where(tr_s > 0, 100.0 * pdm_s / tr_s, np.nan)
        di_m = np.where(tr_s > 0, 100.0 * mdm_s / tr_s, np.nan)
        denom = di_p + di_m
        dx = np.where(denom > 0, 100.0 * np.abs(di_p - di_m) / denom, np.nan)
    di_p_out[period - 1:] = di_p[period - 1:]
    di_m_out[period - 1:] = di_m[period - 1:]

    # ADX: 从首个有效 DX 起做 period 均值种子 + 同型递推
    valid = ~np.isnan(dx)
    idx = np.flatnonzero(valid)
    if len(idx) < period:
        return di_p_out, di_m_out, adx_out
    start = idx[0]
    dx_valid = dx[start:]
    if np.isnan(dx_valid).any():
        return di_p_out, di_m_out, adx_out   # 序列中段缺失不支持
    seed = float(np.mean(dx_valid[:period]))
    adx_out[start + period - 1] = seed
    if len(dx_valid) > period:
        tail, _ = lfilter([alpha], [1.0, -(1.0 - alpha)], dx_valid[period:],
                          zi=[seed * (1.0 - alpha)])
        adx_out[start + period:] = tail
    return di_p_out, di_m_out, adx_out


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
