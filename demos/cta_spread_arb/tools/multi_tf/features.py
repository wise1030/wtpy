"""
multi_tf.features — 多周期×多窗口特征提取(离线分析)

特征组(3 类,目标 < 200 行):
1. Regime: 趋势/震荡识别 (ADF, Hurst, ACF lag1, 价差 z-score 滞留时间)
2. Coint:  协整稳定性 (OLS β/c 滚动, 残差半衰期, β 时间序列方差)
3. Signal: 信号质量 (z 阈值穿越次数, 阈值后 N bars 平均回归幅度, 胜率)
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from typing import Dict, Tuple, Optional


# ============================================================
# Group 1: Regime
# ============================================================

def adf_pvalue(series: np.ndarray) -> float:
    """ADF 检验 p-value (越小越平稳)"""
    try:
        from statsmodels.tsa.stattools import adfuller
        s = pd.Series(series).dropna().values
        if len(s) < 20:
            return np.nan
        return float(adfuller(s, autolag='AIC')[1])
    except Exception:
        return np.nan


def hurst_exponent(series: np.ndarray, max_lag: int = 20) -> float:
    """
    Hurst 指数 (0.5=随机游走, <0.5=均值回归, >0.5=趋势)
    简化 R/S 法
    """
    s = pd.Series(series).dropna().values
    if len(s) < max_lag * 2:
        return np.nan
    try:
        lags = range(2, min(max_lag, len(s) // 2))
        tau = [np.sqrt(np.std(np.subtract(s[lag:], s[:-lag]))) for lag in lags]
        if any(t <= 0 for t in tau):
            return np.nan
        poly = np.polyfit(np.log(list(lags)), np.log(tau), 1)
        return float(poly[0] * 2.0)
    except Exception:
        return np.nan


def acf_lag1(series: np.ndarray) -> float:
    """一阶自相关 (高=趋势,低/负=均值回归)"""
    s = pd.Series(series).dropna()
    if len(s) < 10:
        return np.nan
    return float(s.autocorr(lag=1))


def regime_features(spread: np.ndarray) -> Dict[str, float]:
    return {
        'adf_p':   adf_pvalue(spread),
        'hurst':   hurst_exponent(spread),
        'acf1':    acf_lag1(spread),
        'std':     float(np.nanstd(spread)),
        'range':   float(np.nanmax(spread) - np.nanmin(spread)) if len(spread) else np.nan,
    }


# ============================================================
# Group 2: Coint
# ============================================================

def ols_beta_c(y: np.ndarray, x: np.ndarray) -> Tuple[float, float, np.ndarray]:
    """OLS y = beta*x + c, 返回 (beta, c, residual)"""
    mask = ~(np.isnan(y) | np.isnan(x))
    y, x = y[mask], x[mask]
    if len(y) < 10:
        return np.nan, np.nan, np.array([])
    X = np.column_stack([x, np.ones_like(x)])
    try:
        params, *_ = np.linalg.lstsq(X, y, rcond=None)
        beta, c = params[0], params[1]
        resid = y - (beta * x + c)
        return float(beta), float(c), resid
    except Exception:
        return np.nan, np.nan, np.array([])


def half_life(resid: np.ndarray) -> float:
    """残差均值回归半衰期 (OU 估计, bars)"""
    s = pd.Series(resid).dropna()
    if len(s) < 30:
        return np.nan
    lag = s.shift(1).dropna()
    delta = (s.diff()).dropna()
    n = min(len(lag), len(delta))
    if n < 30:
        return np.nan
    lag = lag.iloc[-n:].values
    delta = delta.iloc[-n:].values
    try:
        X = np.column_stack([lag, np.ones_like(lag)])
        params, *_ = np.linalg.lstsq(X, delta, rcond=None)
        phi = params[0]
        if phi >= 0:
            return np.inf  # 发散
        return float(-np.log(2) / np.log(1 + phi)) if phi > -1 else np.nan
    except Exception:
        return np.nan


def beta_stability(y: np.ndarray, x: np.ndarray, win: int = 60) -> float:
    """滚动 beta 的标准差 / 均值 (CV, 越小越稳定)"""
    if len(y) < win * 2:
        return np.nan
    betas = []
    for i in range(win, len(y)):
        b, _, _ = ols_beta_c(y[i-win:i], x[i-win:i])
        if not np.isnan(b):
            betas.append(b)
    if len(betas) < 5:
        return np.nan
    arr = np.array(betas)
    mu = np.mean(arr)
    return float(np.std(arr) / abs(mu)) if abs(mu) > 1e-9 else np.nan


def coint_features(y: np.ndarray, x: np.ndarray) -> Dict[str, float]:
    beta, c, resid = ols_beta_c(y, x)
    return {
        'beta':         beta,
        'c':            c,
        'half_life':    half_life(resid),
        'beta_cv':      beta_stability(y, x, win=min(60, len(y)//3)),
        'resid_adf_p':  adf_pvalue(resid),
    }


# ============================================================
# Group 3: Signal
# ============================================================

def signal_features(spread: np.ndarray,
                    entry_z: float = 1.5,
                    exit_z: float = 0.3,
                    lookback: int = 60,
                    holding_max: int = 30) -> Dict[str, float]:
    """
    模拟简单 z-score 信号:
      - z > entry_z: 做空价差 / z < -entry_z: 做多
      - |z| < exit_z 或 持仓 >= holding_max: 平仓
    返回信号统计(不算 fee)
    """
    s = pd.Series(spread).dropna()
    if len(s) < lookback * 2:
        return {'n_signals': 0, 'win_rate': np.nan, 'avg_pnl_z': np.nan, 'avg_hold': np.nan}

    z = (s - s.rolling(lookback).mean()) / s.rolling(lookback).std()
    z = z.fillna(0).values
    spread_arr = s.values

    pos = 0
    entry_idx = -1
    entry_spread = 0
    pnls, holds = [], []
    for i in range(len(z)):
        if pos == 0:
            if z[i] > entry_z:
                pos, entry_idx, entry_spread = -1, i, spread_arr[i]
            elif z[i] < -entry_z:
                pos, entry_idx, entry_spread = 1, i, spread_arr[i]
        else:
            hold = i - entry_idx
            if abs(z[i]) < exit_z or hold >= holding_max:
                pnl = pos * (spread_arr[i] - entry_spread)
                # 归一化到 std 单位,跨 TF/品种可比
                norm = pnl / s.rolling(lookback).std().iloc[i] if s.rolling(lookback).std().iloc[i] > 0 else 0
                pnls.append(norm)
                holds.append(hold)
                pos = 0

    if not pnls:
        return {'n_signals': 0, 'win_rate': np.nan, 'avg_pnl_z': np.nan, 'avg_hold': np.nan}
    pnls = np.array(pnls)
    return {
        'n_signals':  len(pnls),
        'win_rate':   float((pnls > 0).mean()),
        'avg_pnl_z':  float(pnls.mean()),
        'avg_hold':   float(np.mean(holds)),
    }


# ============================================================
# Aggregator
# ============================================================

def extract_all(spread: np.ndarray,
                p1: Optional[np.ndarray] = None,
                p2: Optional[np.ndarray] = None) -> Dict[str, float]:
    """提取全部特征. p1/p2 用于协整组(可选)"""
    out = {}
    out.update({f'reg_{k}': v for k, v in regime_features(spread).items()})
    if p1 is not None and p2 is not None:
        out.update({f'coi_{k}': v for k, v in coint_features(p1, p2).items()})
    out.update({f'sig_{k}': v for k, v in signal_features(spread).items()})
    return out
