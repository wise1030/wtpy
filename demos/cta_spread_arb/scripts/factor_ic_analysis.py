#!/usr/bin/env python3
"""
factor_ic_analysis.py — 因子 IC (Information Coefficient) 分析

M3 目标: 对所有关停因子计算 IC, 与已启用因子比较,
决定哪些因子值得重启。

IC 定义: 因子值与未来 N 期 spread 收益率的 Spearman rank 相关系数。
- |IC| > 0.05: 有预测力
- 与已启用因子相关系数 > 0.8: 共线性高, 不值得重启

用法:
    python scripts/factor_ic_analysis.py
    python scripts/factor_ic_analysis.py --forward 20
    python scripts/factor_ic_analysis.py --experiment default
"""
import os
import sys
import argparse
import numpy as np
import pandas as pd
from scipy import stats

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_PROJECT_ROOT,
           os.path.join(_PROJECT_ROOT, 'strategies'),
           os.path.join(_PROJECT_ROOT, 'helpers'),
           os.path.join(_PROJECT_ROOT, 'factors')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import yaml
from FactorEngine import _auto_discover_factors


def load_csv_data(storage_dir, legs, period='m5'):
    """从 CSV 加载 OHLCV 数据"""
    dfs = {}
    for leg in legs:
        code = leg['code']
        fn = f"{code}_{period}.csv"
        fp = os.path.join(storage_dir, fn)
        if os.path.exists(fp):
            df = pd.read_csv(fp, encoding='utf-8-sig')
            dfs[code] = df
        else:
            print(f"  [IC] WARNING: {fp} not found")
    return dfs


def build_spread_series(close_a, close_b, ratio):
    """构建 spread = close_a - ratio * close_b"""
    min_len = min(len(close_a), len(close_b))
    return close_a[:min_len] - ratio * close_b[:min_len]


def compute_factor_series(factor, spread, lookback=200,
                          close_a=None, close_b=None,
                          volume_a=None, volume_b=None,
                          open_a=None, open_b=None,
                          high_a=None, high_b=None,
                          low_a=None, low_b=None,
                          turnover_a=None, turnover_b=None,
                          oi_a=None, oi_b=None):
    """逐 bar 调用 factor.calculate(), 返回因子值序列。

    因子接口: factor.calculate(spread_window, **kwargs) → float
    CointResidualFactor 特殊: 需要 factor.refit() 周期调用
    """
    values = []
    n = len(spread)
    is_coint = hasattr(factor, 'refit')
    refit_interval = 200  # CointResidualFactor 每 200 bar refit

    for i in range(n):
        start = max(0, i - lookback)
        spread_window = spread[start:i + 1]

        if len(spread_window) < 5:
            values.append(0.0)
            continue

        # CointResidualFactor: 定期 refit
        if is_coint and i > 0 and i % refit_interval == 0:
            try:
                factor.refit(spread_window)
            except Exception:
                pass

        try:
            # 构建 kwargs — 部分因子需要额外数据
            kwargs = {}
            if close_a is not None:
                kwargs['close_a'] = close_a[start:i + 1]
            if close_b is not None:
                kwargs['close_b'] = close_b[start:i + 1]
            if volume_a is not None:
                kwargs['volume_a'] = volume_a[start:i + 1]
            if volume_b is not None:
                kwargs['volume_b'] = volume_b[start:i + 1]
            if open_a is not None:
                kwargs['open_a'] = open_a[start:i + 1]
            if open_b is not None:
                kwargs['open_b'] = open_b[start:i + 1]
            if high_a is not None:
                kwargs['high_a'] = high_a[start:i + 1]
            if high_b is not None:
                kwargs['high_b'] = high_b[start:i + 1]
            if low_a is not None:
                kwargs['low_a'] = low_a[start:i + 1]
            if low_b is not None:
                kwargs['low_b'] = low_b[start:i + 1]
            if turnover_a is not None:
                kwargs['turnover_a'] = turnover_a[start:i + 1]
            if turnover_b is not None:
                kwargs['turnover_b'] = turnover_b[start:i + 1]
            if oi_a is not None:
                kwargs['oi_a'] = oi_a[start:i + 1]
            if oi_b is not None:
                kwargs['oi_b'] = oi_b[start:i + 1]

            v = factor.calculate(spread_window, **kwargs)
            values.append(float(v) if np.isfinite(v) else 0.0)
        except Exception:
            values.append(0.0)

    return np.array(values)


def compute_ic(factor_values, forward_returns, method='spearman'):
    """计算 IC (rank 相关系数)"""
    valid = np.isfinite(factor_values) & np.isfinite(forward_returns)
    # 排除 0 值 (未初始化)
    nonzero = factor_values != 0.0
    valid = valid & nonzero
    if valid.sum() < 30:
        return np.nan, np.nan
    fv = factor_values[valid]
    fr = forward_returns[valid]
    if method == 'spearman':
        ic, pval = stats.spearmanr(fv, fr)
    else:
        ic, pval = stats.pearsonr(fv, fr)
    return ic, pval


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Factor IC Analysis')
    parser.add_argument('--experiment', type=str, default='rb_hc_2601')
    parser.add_argument('--config-root', type=str, default=None)
    parser.add_argument('--forward', type=int, default=10,
                        help='前视 bar 数 (默认10)')
    parser.add_argument('--storage', type=str, default=None)
    args = parser.parse_args()

    config_dir = (os.path.abspath(args.config_root)
                  if args.config_root
                  else os.path.join(_PROJECT_ROOT, 'config'))
    storage_dir = args.storage or os.path.join(_PROJECT_ROOT, 'storage', 'csv')

    # 加载实验配置
    exp_path = os.path.join(config_dir, 'experiments', f'{args.experiment}.yaml')
    with open(exp_path, 'r', encoding='utf-8') as f:
        exp_cfg = yaml.safe_load(f) or {}

    # 加载因子配置
    factors_path = os.path.join(config_dir, 'base', 'factors.yaml')
    with open(factors_path, 'r', encoding='utf-8') as f:
        factors_cfg = yaml.safe_load(f) or {}

    # 加载合约配置
    contracts_path = os.path.join(config_dir, 'contracts', 'rb_hc.yaml')
    with open(contracts_path, 'r', encoding='utf-8') as f:
        contract_cfg = yaml.safe_load(f) or {}

    # 渲染 legs
    legs = contract_cfg.get('legs_template', contract_cfg.get('legs', []))
    ratio = contract_cfg.get('ratio', 1.0)
    contract_month = exp_cfg.get('instances', [{}])[0].get('contract_month', '2601')

    rendered_legs = []
    for leg in legs:
        tpl = leg.get('stdCode_template', leg.get('stdCode', ''))
        std_code = tpl.replace('{month}', contract_month)
        rendered_legs.append({
            'code': std_code,
            'exchange': leg.get('exchange', ''),
            'product': leg.get('symbol', ''),
            'contract': contract_month,
            'ratio': leg.get('ratio', 1),
        })
    legs = rendered_legs

    print(f"[IC] experiment={args.experiment}")
    print(f"[IC] legs={[l.get('code','?') for l in legs]}")
    print(f"[IC] ratio={ratio}")
    print(f"[IC] forward={args.forward} bars")

    # 加载 CSV 数据
    period = 'm5'
    csv_data = load_csv_data(storage_dir, legs, period)
    if len(csv_data) < 2:
        print(f"[IC] ERROR: 只找到 {len(csv_data)} 个品种的数据")
        sys.exit(1)

    leg_a, leg_b = legs[0], legs[1]
    code_a, code_b = leg_a['code'], leg_b['code']

    def get_series(code, col):
        df = csv_data.get(code)
        if df is None:
            return None
        return df[col].values.astype(float) if col in df.columns else None

    close_a = get_series(code_a, 'close')
    close_b = get_series(code_b, 'close')
    volume_a = get_series(code_a, 'volume')
    volume_b = get_series(code_b, 'volume')
    open_a = get_series(code_a, 'open')
    open_b = get_series(code_b, 'open')
    high_a = get_series(code_a, 'high')
    high_b = get_series(code_b, 'high')
    low_a = get_series(code_a, 'low')
    low_b = get_series(code_b, 'low')
    turnover_a = get_series(code_a, 'turnover')
    turnover_b = get_series(code_b, 'turnover')
    oi_a = get_series(code_a, 'open_interest')
    oi_b = get_series(code_b, 'open_interest')

    # 构建 spread
    spread = build_spread_series(close_a, close_b, ratio)
    n = len(spread)
    print(f"[IC] spread length={n}")

    # 未来收益率
    forward_returns = np.full(n, np.nan)
    forward_returns[:n - args.forward] = (
        spread[args.forward:] - spread[:n - args.forward]
    )

    # 发现所有因子类
    factor_map = _auto_discover_factors()
    print(f"[IC] discovered {len(factor_map)} factor classes")

    # 对每个因子计算 IC
    results = []
    factor_value_map = {}

    # 判定哪些因子已启用
    enabled_names = set()
    for f_cfg in factors_cfg.get('factors', []):
        if isinstance(f_cfg, dict) and f_cfg.get('enabled', False):
            enabled_names.add(f_cfg.get('name', ''))

    for cls_name, cls in sorted(factor_map.items()):
        try:
            factor = cls(name=cls_name, params={})
        except Exception as e:
            print(f"  [IC] {cls_name}: 创建失败 ({e})")
            continue

        # 逐 bar 计算
        try:
            values = compute_factor_series(
                factor, spread,
                close_a=close_a, close_b=close_b,
                volume_a=volume_a, volume_b=volume_b,
                open_a=open_a, open_b=open_b,
                high_a=high_a, high_b=high_b,
                low_a=low_a, low_b=low_b,
                turnover_a=turnover_a, turnover_b=turnover_b,
                oi_a=oi_a, oi_b=oi_b,
            )
        except Exception as e:
            print(f"  [IC] {cls_name}: 计算失败 ({e})")
            continue

        # 计算 IC
        ic, pval = compute_ic(values, forward_returns)
        nonzero_pct = (values != 0.0).sum() / n * 100

        results.append({
            'name': cls_name,
            'ic': ic,
            'pval': pval,
            'nonzero_pct': nonzero_pct,
            'enabled': cls_name in enabled_names,
        })
        factor_value_map[cls_name] = values

        status = "ON" if cls_name in enabled_names else "OFF"
        sig = "✱" if abs(ic) > 0.05 and pval < 0.05 else ""
        ic_str = f"{ic:+.4f}" if not np.isnan(ic) else "   NaN"
        p_str = f"{pval:.4f}" if not np.isnan(pval) else "  NaN"
        print(f"  {cls_name:>30s} [{status:>3s}] IC={ic_str}  p={p_str}  nonzero={nonzero_pct:.0f}%  {sig}")

    # === 因子间相关性矩阵 ===
    print(f"\n{'='*70}")
    print("因子间相关性 (Spearman) — 标记 > 0.8 共线性")
    print(f"{'='*70}")

    valid_names = [r['name'] for r in results if not np.isnan(r['ic'])]
    n_factors = len(valid_names)
    corr_matrix = np.eye(n_factors)

    if n_factors > 1:
        min_len = min(len(factor_value_map[nm]) for nm in valid_names)
        mat = np.column_stack([
            factor_value_map[nm][:min_len] for nm in valid_names
        ])

        for i in range(n_factors):
            for j in range(i + 1, n_factors):
                valid = (mat[:, i] != 0.0) & (mat[:, j] != 0.0) & np.isfinite(mat[:, i]) & np.isfinite(mat[:, j])
                if valid.sum() > 30:
                    corr_matrix[i, j], _ = stats.spearmanr(mat[valid, i], mat[valid, j])
                    corr_matrix[j, i] = corr_matrix[i, j]

        # 打印
        short_names = [n[:10] for n in valid_names]
        header = f"{'':>11s}" + "".join(f"{sn:>11s}" for sn in short_names)
        print(header)
        for i in range(n_factors):
            row = f"{short_names[i]:>11s}"
            for j in range(n_factors):
                if j <= i:
                    row += f"{'':>11s}"
                else:
                    c = corr_matrix[i, j]
                    marker = "!" if abs(c) > 0.8 else ""
                    row += f"{c:>+8.2f}{marker:>3s}"
            print(row)

    # === 决策建议 ===
    print(f"\n{'='*70}")
    print("重启决策建议")
    print(f"{'='*70}")
    print(f"标准: |IC| > 0.05 且 p < 0.05, 且与已启用因子相关 < 0.8")
    print()

    for r in results:
        if r['enabled']:
            continue
        ic_abs = abs(r['ic']) if not np.isnan(r['ic']) else 0
        sig = r['pval'] < 0.05 if not np.isnan(r['pval']) else False

        # 检查与已启用因子的相关性
        high_corr_with = []
        if n_factors > 1 and r['name'] in valid_names:
            idx = valid_names.index(r['name'])
            for er in results:
                if not er['enabled']:
                    continue
                if er['name'] not in valid_names:
                    continue
                eidx = valid_names.index(er['name'])
                c = corr_matrix[idx, eidx] if not np.isnan(corr_matrix[idx, eidx]) else 0
                if abs(c) > 0.8:
                    high_corr_with.append(f"{er['name']}(r={c:.2f})")

        if ic_abs > 0.05 and sig:
            if high_corr_with:
                verdict = f"有预测力但共线性: {', '.join(high_corr_with)}"
            else:
                verdict = "✅ 建议重启"
        elif ic_abs > 0.03 and sig:
            verdict = "🟡 边际预测力, 可观察"
        else:
            verdict = "❌ 不建议重启"

        ic_str = f"{r['ic']:+.4f}" if not np.isnan(r['ic']) else "   NaN"
        print(f"  {r['name']:>30s}  IC={ic_str}  p={r['pval']:.4f}  → {verdict}")

    print("\n[IC] Done.")
