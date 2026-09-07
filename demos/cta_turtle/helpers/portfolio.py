"""组合层权益合成与贡献分解 (P1-4)

口径: 回测产出 funds.csv 的 dynbalance 为相对初始资金的累计盈亏
(首日为 0), 故组合权益 = capital × (1 + Σ wᵢ · dynbalanceᵢ(t)/capitalᵢ)。
各品种曲线按交易日并集对齐, 缺失日期前值填充 (ffill)。

纯函数实现, 供 run_matrix 调用与单测。
"""
import csv
import os

import numpy as np

TRADING_DAYS = 240


def read_funds_curve(path: str) -> dict:
    """读取 funds.csv → {date(int): dynbalance(float)}; 失败返回 {}"""
    if not os.path.exists(path):
        return {}
    out = {}
    try:
        with open(path, encoding='utf-8') as f:
            for r in csv.DictReader(f):
                try:
                    out[int(r["date"])] = float(r["dynbalance"])
                except (KeyError, ValueError, TypeError):
                    continue
    except OSError:
        return {}
    return out


def merge_curves(curves: dict, weights: dict = None):
    """多品种盈亏曲线对齐合成。

    Args:
        curves: {code: {date: pnl}}
        weights: {code: 权重}; 缺省等权 (和归一化)

    Returns:
        (dates: list[int], port_pnl: np.ndarray,
         aligned: {code: np.ndarray})   # aligned 已含权重
    """
    codes = [c for c in curves if curves[c]]
    if not codes:
        return [], np.array([]), {}
    if not weights:
        w = 1.0 / len(codes)
        weights = {c: w for c in codes}
    else:
        tot = sum(weights.get(c, 0.0) for c in codes)
        weights = {c: (weights.get(c, 0.0) / tot if tot > 0 else 0.0)
                   for c in codes}

    dates = sorted({d for c in codes for d in curves[c]})
    idx = {d: i for i, d in enumerate(dates)}
    aligned = {}
    for c in codes:
        cmap = curves[c]
        arr = np.zeros(len(dates))
        first_i = min(idx[d] for d in cmap)
        last = 0.0
        for i, d in enumerate(dates):
            if d in cmap:
                last = cmap[d]
            # 上市前填 0; 之后缺口前值填充 (ffill)
            arr[i] = (last if i >= first_i else 0.0)
        aligned[c] = arr * weights[c]
    port = np.sum([aligned[c] for c in codes], axis=0)
    return dates, port, aligned


def portfolio_metrics(dates: list, port_pnl: np.ndarray,
                      capital: float, rf: float = 0.02,
                      days: int = TRADING_DAYS) -> dict:
    """由组合盈亏序列计算年化/回撤/夏普/卡玛"""
    if len(port_pnl) == 0 or capital <= 0:
        return {}
    equity = capital + port_pnl
    rets = np.diff(equity) / equity[:-1]
    total_ret = port_pnl[-1] / capital
    years = max(len(port_pnl) / days, 1e-9)
    annual = (1.0 + total_ret) ** (1.0 / years) - 1.0 \
        if float(equity.min()) > 0 else -1.0
    peak = np.maximum.accumulate(equity)
    dd = (peak - equity) / peak
    max_dd = float(dd.max()) if len(dd) else 0.0
    std_daily = float(rets.std()) if len(rets) > 1 else 0.0
    ann_vol = std_daily * (days ** 0.5)
    sharpe = (float(rets.mean()) * days - rf) / ann_vol \
        if ann_vol > 0 else 0.0
    calmar = annual / max_dd if max_dd > 0 else 0.0
    return {"total_return": round(float(total_ret), 6),
            "annual_return": round(float(annual), 6),
            "max_drawdown": round(max_dd, 6),
            "sharpe_ratio": round(sharpe, 4),
            "calmar_ratio": round(float(calmar), 4),
            "days": len(port_pnl)}


def contribution(aligned: dict, port_pnl: np.ndarray) -> dict:
    """各品种对组合总盈亏的贡献占比 (含权重后的口径)"""
    tot = float(port_pnl[-1]) if len(port_pnl) else 0.0
    if abs(tot) < 1e-12:
        return {c: 0.0 for c in aligned}
    return {c: round(float(a[-1]) / tot, 4) for c, a in aligned.items()}
