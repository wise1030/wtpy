"""auto_profile — 基于历史价差走势自动选择 param_profile

v8.1: 让 SpreadArbStrategy 在 __init__ 时根据合约的历史价差结构
(Hurst/ADF/趋势斜率) 自动决定该 sub 应用哪个 param_profile,
无需用户手动为每个合约月份在 YAML 里指定。

判定原则:
- Hurst > trend_threshold (默认 0.55) → trending_contract
- Hurst < mr_threshold   (默认 0.45) → mr_contract
- 否则                              → default (None, 用全局 baseline)

数据源: storage/csv/EXCHG.PRODUCT.CONTRACT_d.csv (日线), CSV 列包含 close。
若数据不足或文件缺失, 回退到默认 profile 并打印 warning。
"""
import os
import csv
import math
from typing import List, Optional, Tuple


def _read_close(csv_path: str) -> List[float]:
    """读 d1 CSV 的 close 列。文件不存在或字段缺失返回空列表。"""
    if not os.path.exists(csv_path):
        return []
    closes = []
    try:
        with open(csv_path, encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                c = row.get('close') or row.get('Close')
                if c:
                    try:
                        closes.append(float(c))
                    except ValueError:
                        continue
    except Exception:
        return []
    return closes


def calc_hurst(series: List[float], max_lag: int = 50) -> float:
    """R/S 简化版 Hurst 指数 (与 regime_detector._calc_hurst 同一公式).

    series 长度 < 30 返回 0.5 (中性).
    """
    n = len(series)
    if n < 30:
        return 0.5
    max_lag = min(max_lag, n // 4)
    if max_lag < 5:
        return 0.5
    tau = []
    for lag in range(2, max_lag):
        diffs = [series[i + lag] - series[i] for i in range(n - lag)]
        if not diffs:
            continue
        mean = sum(diffs) / len(diffs)
        var = sum((d - mean) ** 2 for d in diffs) / len(diffs)
        std = math.sqrt(var)
        if std > 0:
            tau.append(std)
    if len(tau) < 3:
        return 0.5
    try:
        xs = [math.log(i) for i in range(2, 2 + len(tau))]
        ys = [math.log(t) for t in tau]
        # 一阶多项式拟合
        mx = sum(xs) / len(xs)
        my = sum(ys) / len(ys)
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        den = sum((x - mx) ** 2 for x in xs)
        if den == 0:
            return 0.5
        h = num / den
        return max(0.3, min(0.8, h))
    except Exception:
        return 0.5


def compute_spread(closes_a: List[float], closes_b: List[float],
                   ratio_a: float = 1.0, ratio_b: float = -1.0) -> List[float]:
    """按 leg 比例合成价差序列, 取共同长度的尾段对齐."""
    n = min(len(closes_a), len(closes_b))
    if n == 0:
        return []
    a = closes_a[-n:]
    b = closes_b[-n:]
    return [ratio_a * a[i] + ratio_b * b[i] for i in range(n)]


def calc_trend_r2(series: List[float]) -> Tuple[float, float]:
    """对 series 做一元线性回归, 返回 (R², slope_normalized).

    R² 衡量序列贴线性趋势的程度 (0=完全无趋势, 1=完美直线).
    slope_normalized = slope * len / mean(|series|), 跨品种可比 (无量纲).
    Hurst 在单边强趋势但日内大幅震荡的价差上会偏低 (低估趋势),
    R² 可以补足这一缺陷.
    """
    n = len(series)
    if n < 10:
        return 0.0, 0.0
    xs = list(range(n))
    mx = (n - 1) / 2.0
    my = sum(series) / n
    num = sum((xs[i] - mx) * (series[i] - my) for i in range(n))
    den_x = sum((x - mx) ** 2 for x in xs)
    den_y = sum((y - my) ** 2 for y in series)
    if den_x == 0 or den_y == 0:
        return 0.0, 0.0
    slope = num / den_x
    # R² = (Cov)² / (Var_x * Var_y)
    r2 = (num * num) / (den_x * den_y)
    scale = abs(my) if abs(my) > 1e-6 else 1.0
    slope_norm = slope * n / scale
    return float(max(0.0, min(1.0, r2))), float(slope_norm)


def select_profile_by_hurst(spread: List[float],
                            trend_threshold: float = 0.55,
                            mr_threshold: float = 0.45,
                            lookback: int = 120,
                            trend_r2_threshold: float = 0.55) -> Tuple[Optional[str], float]:
    """根据价差 Hurst + 线性趋势 R² 联合返回 (profile_name, hurst_value).

    判定优先级:
      1. 线性趋势 R² > trend_r2_threshold → trending_contract (强单边趋势, Hurst 可能假性偏低)
      2. Hurst > trend_threshold          → trending_contract
      3. Hurst < mr_threshold             → mr_contract
      4. 否则                             → None (baseline)

    profile_name 取值:
      - 'trending_contract'
      - 'mr_contract'
      - None
    """
    if not spread:
        return None, 0.5
    ts = spread[-lookback:] if len(spread) > lookback else spread
    h = calc_hurst(ts)
    r2, _slope = calc_trend_r2(ts)
    # R² 优先 — 强趋势绕过 Hurst 判定
    if r2 > trend_r2_threshold:
        return 'trending_contract', h
    if h > trend_threshold:
        return 'trending_contract', h
    if h < mr_threshold:
        return 'mr_contract', h
    return None, h


def auto_select_for_legs(legs: list, storage_csv_dir: str,
                         trend_threshold: float = 0.55,
                         mr_threshold: float = 0.45,
                         lookback: int = 120,
                         trend_r2_threshold: float = 0.55) -> Tuple[Optional[str], float, str]:
    """根据 sub 的 legs 配置, 读取 d1 CSV 数据计算价差 Hurst+R², 返回 profile.

    Args:
        legs: ArbCoreConfig.legs (LegConfig list, 至少 2 条)
        storage_csv_dir: storage/csv/ 路径
        trend/mr_threshold: Hurst 阈值
        lookback: 回看根数
        trend_r2_threshold: 强趋势 R² 阈值 (绕过 Hurst)

    Returns:
        (profile_name, hurst, info_msg)
    """
    if len(legs) < 2:
        return None, 0.5, 'legs不足2条, 跳过auto_profile'
    leg_a, leg_b = legs[0], legs[1]
    csv_a = os.path.join(storage_csv_dir, f'{leg_a.code}_d.csv')
    csv_b = os.path.join(storage_csv_dir, f'{leg_b.code}_d.csv')

    closes_a = _read_close(csv_a)
    closes_b = _read_close(csv_b)
    if not closes_a or not closes_b:
        return None, 0.5, f'缺失日线数据: {csv_a if not closes_a else csv_b}'

    spread = compute_spread(closes_a, closes_b,
                            ratio_a=leg_a.ratio, ratio_b=leg_b.ratio)
    ts = spread[-lookback:] if len(spread) > lookback else spread
    h = calc_hurst(ts)
    r2, slope = calc_trend_r2(ts)
    profile, _ = select_profile_by_hurst(spread, trend_threshold, mr_threshold,
                                         lookback, trend_r2_threshold)
    info = (f'spread_len={len(spread)}, hurst={h:.3f}, r2={r2:.3f}, slope_norm={slope:.3f}, '
            f'thr=[mr<{mr_threshold},trend_h>{trend_threshold},trend_r2>{trend_r2_threshold}]')
    return profile, h, info
