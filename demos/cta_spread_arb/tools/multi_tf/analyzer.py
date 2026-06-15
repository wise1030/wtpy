"""
multi_tf.analyzer — 多周期×多窗口分析主入口

工作流:
1. 加载两腿 1m CSV → resample 到目标 TFs
2. 对齐双腿 → 按 spread_mode(price/coint) 计算价差
3. 按 window 切片(滚动) → 对每片提取 features
4. 输出 CSV(展平) + heatmaps(per feature) + summary.md

用法:
  from tools.multi_tf.analyzer import run_pair_analysis
  run_pair_analysis(
      pair_name='l_pp_2609',
      leg1_csv='.../DCE.l.l2609.csv',
      leg2_csv='.../DCE.pp.pp2609.csv',
      tfs=['m5','m15','m30','h1','h2'],
      windows={'1d':1, '3d':3, '1w':5, '2w':10},  # 单位:交易日
      output_dir='analysis_results/l_pp_2609',
  )
"""
from __future__ import annotations
import pandas as pd
import numpy as np
from pathlib import Path
from typing import Dict, List, Optional
from .synth import load_ks_1m_csv, resample_to_tf, TF_RULES
from .features import extract_all


def align_two_legs(df1: pd.DataFrame, df2: pd.DataFrame) -> pd.DataFrame:
    """按 datetime inner-join 双腿,返回 (datetime,trading_date,close1,close2)"""
    a = df1[['datetime', 'trading_date', 'close']].rename(columns={'close': 'p1'})
    b = df2[['datetime', 'close']].rename(columns={'close': 'p2'})
    m = pd.merge(a, b, on='datetime', how='inner')
    return m.dropna().reset_index(drop=True)


def slice_by_trading_days(merged: pd.DataFrame,
                          window_days: int) -> List[pd.DataFrame]:
    """滚动按 trading_date 切窗口(非重叠), 返回每段 DataFrame"""
    dates = sorted(merged['trading_date'].unique())
    out = []
    for i in range(0, len(dates), window_days):
        chunk_dates = dates[i:i + window_days]
        if len(chunk_dates) < window_days:
            continue  # 不足一窗丢弃
        seg = merged[merged['trading_date'].isin(chunk_dates)]
        if len(seg) >= 30:  # 至少 30 bars 才有意义
            out.append(seg)
    return out


def analyze_segment(seg: pd.DataFrame,
                    spread_mode: str = 'price') -> Dict[str, float]:
    """对一段(tf×window)提取所有特征"""
    p1 = seg['p1'].values
    p2 = seg['p2'].values
    if spread_mode == 'coint':
        # 用段内 OLS 算 β,c 后取残差当价差
        from .features import ols_beta_c
        beta, c, resid = ols_beta_c(p1, p2)
        spread = resid if len(resid) > 0 else (p1 - p2)
    else:
        spread = p1 - p2  # 简单价格差(单位 1:1; 后续可接 SpreadCalculator)
    return extract_all(spread, p1=p1, p2=p2)


def run_pair_analysis(pair_name: str,
                      leg1_csv: str | Path,
                      leg2_csv: str | Path,
                      tfs: List[str] = None,
                      windows: Dict[str, int] = None,
                      spread_mode: str = 'price',
                      output_dir: str | Path = './analysis_results') -> pd.DataFrame:
    """
    主入口
    Returns: 长格式 DataFrame [pair,tf,window,seg_start,seg_end,n_bars,<features...>]
    """
    tfs = tfs or ['m5', 'm15', 'm30', 'h1', 'h2']
    windows = windows or {'1d': 1, '3d': 3, '1w': 5, '2w': 10}
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[{pair_name}] loading 1m...")
    df1_1m = load_ks_1m_csv(leg1_csv)
    df2_1m = load_ks_1m_csv(leg2_csv)
    print(f"  leg1 bars={len(df1_1m)}, leg2 bars={len(df2_1m)}")

    rows = []
    for tf in tfs:
        print(f"[{pair_name}] tf={tf} resampling...")
        df1_tf = resample_to_tf(df1_1m, tf)
        df2_tf = resample_to_tf(df2_1m, tf)
        if df1_tf.empty or df2_tf.empty:
            print(f"  tf={tf} empty, skip")
            continue
        merged = align_two_legs(df1_tf, df2_tf)
        if merged.empty:
            print(f"  tf={tf} merged empty, skip")
            continue
        print(f"  tf={tf} merged bars={len(merged)}, dates={merged['trading_date'].nunique()}")

        for wname, wdays in windows.items():
            segs = slice_by_trading_days(merged, wdays)
            print(f"    window={wname}({wdays}d) → {len(segs)} segments")
            for k, seg in enumerate(segs):
                feats = analyze_segment(seg, spread_mode=spread_mode)
                row = {
                    'pair':      pair_name,
                    'tf':        tf,
                    'window':    wname,
                    'seg_idx':   k,
                    'seg_start': seg['trading_date'].min().strftime('%Y-%m-%d'),
                    'seg_end':   seg['trading_date'].max().strftime('%Y-%m-%d'),
                    'n_bars':    len(seg),
                    **feats,
                }
                rows.append(row)

    df_out = pd.DataFrame(rows)
    csv_path = output_dir / f'{pair_name}_features.csv'
    df_out.to_csv(csv_path, index=False)
    print(f"[{pair_name}] saved {len(df_out)} rows → {csv_path}")
    return df_out


def make_heatmap(df: pd.DataFrame,
                 feature: str,
                 output_path: str | Path,
                 agg: str = 'mean') -> None:
    """tf × window heatmap (聚合所有 segments)"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    pivot = df.pivot_table(index='tf', columns='window',
                           values=feature, aggfunc=agg)
    # TF 排序
    tf_order = ['m5', 'm15', 'm30', 'h1', 'h2']
    pivot = pivot.reindex([t for t in tf_order if t in pivot.index])
    win_order = ['1d', '3d', '1w', '2w']
    pivot = pivot[[w for w in win_order if w in pivot.columns]]

    fig, ax = plt.subplots(figsize=(8, 4))
    im = ax.imshow(pivot.values, cmap='RdYlGn_r', aspect='auto')
    ax.set_xticks(range(len(pivot.columns)))
    ax.set_xticklabels(pivot.columns)
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels(pivot.index)
    ax.set_title(f'{feature} ({agg})')
    for i in range(len(pivot.index)):
        for j in range(len(pivot.columns)):
            v = pivot.values[i, j]
            txt = f'{v:.3f}' if not np.isnan(v) else 'n/a'
            ax.text(j, i, txt, ha='center', va='center',
                    color='black', fontsize=9)
    plt.colorbar(im)
    plt.tight_layout()
    plt.savefig(output_path, dpi=100)
    plt.close()


def summarize(df: pd.DataFrame, output_path: str | Path) -> None:
    """生成 summary.md"""
    lines = ['# Multi-TF Analysis Summary', '']
    pairs = df['pair'].unique()
    for pair in pairs:
        sub = df[df['pair'] == pair]
        lines.append(f'## {pair}')
        lines.append(f'- segments: {len(sub)}, tfs: {sub["tf"].nunique()}, windows: {sub["window"].nunique()}')
        for key in ['reg_adf_p', 'reg_hurst', 'coi_half_life', 'coi_beta_cv',
                    'sig_win_rate', 'sig_avg_pnl_z', 'sig_n_signals']:
            if key in sub.columns:
                m = sub[key].mean()
                lines.append(f'- {key} mean: {m:.4f}' if pd.notna(m) else f'- {key}: n/a')
        # 最优 tf×window (按 sig_avg_pnl_z)
        if 'sig_avg_pnl_z' in sub.columns:
            best = sub.groupby(['tf', 'window'])['sig_avg_pnl_z'].mean().sort_values(ascending=False)
            lines.append(f'- best tf×window (avg sig_pnl_z): {best.head(3).to_dict()}')
        lines.append('')
    Path(output_path).write_text('\n'.join(lines), encoding='utf-8')


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--pair', required=True)
    ap.add_argument('--leg1', required=True)
    ap.add_argument('--leg2', required=True)
    ap.add_argument('--tfs', default='m5,m15,m30,h1,h2')
    ap.add_argument('--spread-mode', default='price', choices=['price', 'coint'])
    ap.add_argument('--output', default='./analysis_results')
    args = ap.parse_args()

    tfs = args.tfs.split(',')
    out_dir = Path(args.output) / args.pair
    df = run_pair_analysis(args.pair, args.leg1, args.leg2,
                           tfs=tfs, spread_mode=args.spread_mode,
                           output_dir=out_dir)
    print(f'rows: {len(df)}')
    if not df.empty:
        for feat in ['reg_hurst', 'coi_half_life', 'sig_avg_pnl_z']:
            if feat in df.columns:
                make_heatmap(df, feat, out_dir / f'heatmap_{feat}.png')
        summarize(df, out_dir / 'summary.md')
        print(f'outputs → {out_dir}/')
