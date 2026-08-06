"""indicators 等价性测试: 向量化 vs 朴素参考实现"""
import numpy as np

from conftest import *  # noqa: F401,F403 — sys.path 注入
from helpers.indicators import wilder_atr, donchian, true_range


def _ref_atr(highs, lows, closes, period):
    """朴素循环参考: 种子 mean(TR[:period]), 递推 (prev*(n-1)+TR)/n"""
    n = len(highs)
    out = np.full(n, np.nan)
    if n < period:
        return out
    tr = [highs[0] - lows[0]]
    for i in range(1, n):
        tr.append(max(highs[i] - lows[i],
                      abs(highs[i] - closes[i - 1]),
                      abs(lows[i] - closes[i - 1])))
    prev = sum(tr[:period]) / period
    out[period - 1] = prev
    for i in range(period, n):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def _ref_donchian(highs, lows, period):
    n = len(highs)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    for i in range(period, n):
        upper[i] = max(highs[i - period:i])
        lower[i] = min(lows[i - period:i])
    return upper, lower


def _make_series(n=200, seed=42):
    rng = np.random.RandomState(seed)
    close = 100 + np.cumsum(rng.randn(n) * 2)
    high = close + np.abs(rng.randn(n))
    low = close - np.abs(rng.randn(n))
    return high, low, close


def test_true_range():
    h = np.array([10., 12., 11.])
    l = np.array([8., 9., 10.])
    c = np.array([9., 11., 10.5])
    tr = true_range(h, l, c)
    assert tr[0] == 2.0                     # H-L
    assert tr[1] == max(3, 3, 0)            # max(H-L,|H-C0|,|L-C0|)
    assert abs(tr[2] - max(1, 0, 1)) < 1e-12


def test_wilder_atr_matches_reference():
    h, l, c = _make_series()
    got = wilder_atr(h, l, c, 20)
    ref = _ref_atr(h, l, c, 20)
    assert np.allclose(got[19:], ref[19:], atol=1e-10)
    assert np.all(np.isnan(got[:19]))


def test_wilder_atr_seed_is_simple_mean():
    """经典海龟: 首个 N = 前 period 个 TR 简单平均"""
    h, l, c = _make_series()
    got = wilder_atr(h, l, c, 20)
    tr = true_range(h, l, c)
    assert abs(got[19] - tr[:20].mean()) < 1e-10


def test_wilder_atr_short_series():
    got = wilder_atr(np.arange(5.), np.arange(5.), np.arange(5.), 20)
    assert np.all(np.isnan(got))


def test_donchian_matches_reference():
    h, l, c = _make_series()
    gu, gl = donchian(h, l, 20)
    ru, rl = _ref_donchian(h, l, 20)
    assert np.allclose(gu[20:], ru[20:], atol=1e-12)
    assert np.allclose(gl[20:], rl[20:], atol=1e-12)
    assert np.all(np.isnan(gu[:20]))


def test_donchian_excludes_current_bar():
    """通道值不得含当前 bar (防未来函数)"""
    h = np.array([1., 2., 3., 100., 5., 6.])
    l = np.array([0., 1., 2., 3., 4., 5.])
    u, lo = donchian(h, l, 3)
    # i=3: 前3根 max=3, 不含自身的 100
    assert u[3] == 3.0
    # i=4: 前3根 max(2,3,100)=100
    assert u[4] == 100.0
