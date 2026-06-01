"""
信号诊断脚本 - 分析策略信号强度分布和过滤原因
"""
import yaml
import sys
import os
import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# 恢复基线参数
with open(os.path.join(BASE_DIR, 'config/factor_config.yaml'), 'r') as f:
    fc = yaml.safe_load(f)
fc['signal_aggregation']['entry_threshold'] = 0.30
fc['signal_aggregation']['exit_threshold'] = 0.05
with open(os.path.join(BASE_DIR, 'config/factor_config.yaml'), 'w') as f:
    yaml.dump(fc, f, default_flow_style=False)

with open(os.path.join(BASE_DIR, 'config/strategy_config.yaml'), 'r') as f:
    sc = yaml.safe_load(f)
sc['risk_control']['max_loss_per_pair'] = 10000
sc['risk_control']['max_hold_bars'] = 30
sc['risk_control']['cooldown_bars'] = 12
sc['risk_control']['trailing_decay_ratio'] = 0.5
with open(os.path.join(BASE_DIR, 'config/strategy_config.yaml'), 'w') as f:
    yaml.dump(sc, f, default_flow_style=False)

print("Config restored to baseline")

# 读取CSV数据直接计算信号
import pandas as pd

csv_dir = os.path.join(BASE_DIR, '..', 'storage', 'csv')
rb_file = os.path.join(csv_dir, 'SHFE.rb.HOT_m5.csv')
i_file = os.path.join(csv_dir, 'DCE.i.HOT_m5.csv')

rb_df = pd.read_csv(rb_file)
i_df = pd.read_csv(i_file)

print(f"rb数据: {len(rb_df)}行, 列={list(rb_df.columns)}")
print(f"i数据: {len(i_df)}行")

# 合并
rb_df['datetime'] = pd.to_datetime(rb_df['date'].astype(str) + ' ' + rb_df['time'].astype(str))
i_df['datetime'] = pd.to_datetime(i_df['date'].astype(str) + ' ' + i_df['time'].astype(str))

merged = pd.merge(rb_df[['datetime','close']], i_df[['datetime','close']], on='datetime', suffixes=('_rb','_i'))
merged = merged.sort_values('datetime').reset_index(drop=True)

print(f"合并后: {len(merged)}行")
print(f"日期范围: {merged['datetime'].iloc[0]} ~ {merged['datetime'].iloc[-1]}")

# 计算价差
merged['spread'] = merged['close_rb'] - merged['close_i'] * 5  # 简单价差
merged['spread_pct'] = merged['close_rb'] / merged['close_i']

# Z-score
window = 60
merged['spread_ma'] = merged['spread'].rolling(window).mean()
merged['spread_std'] = merged['spread'].rolling(window).std()
merged['zscore'] = (merged['spread'] - merged['spread_ma']) / merged['spread_std']

# 信号分布
zscore_valid = merged['zscore'].dropna()
print(f"\n=== Z-score分布 ===")
print(f"count: {len(zscore_valid)}")
print(f"mean: {zscore_valid.mean():.4f}")
print(f"std: {zscore_valid.std():.4f}")
print(f"min: {zscore_valid.min():.4f}")
print(f"max: {zscore_valid.max():.4f}")

# 不同阈值下的信号数
for threshold in [0.20, 0.30, 0.40, 0.50, 0.60]:
    long_signals = (zscore_valid < -threshold).sum()
    short_signals = (zscore_valid > threshold).sum()
    print(f"  threshold={threshold}: long={long_signals}, short={short_signals}, total={long_signals+short_signals}")

# 2019年的信号
mask_2019 = (merged['datetime'] >= '2019-01-01') & (merged['datetime'] < '2020-01-01')
zscore_2019 = merged.loc[mask_2019, 'zscore'].dropna()
print(f"\n=== 2019年 Z-score ===")
print(f"count: {len(zscore_2019)}")
for threshold in [0.20, 0.30, 0.40, 0.50]:
    long_signals = (zscore_2019 < -threshold).sum()
    short_signals = (zscore_2019 > threshold).sum()
    print(f"  threshold={threshold}: long={long_signals}, short={short_signals}")

# 价差百分比Z-score
merged['pct_ma'] = merged['spread_pct'].rolling(window).mean()
merged['pct_std'] = merged['spread_pct'].rolling(window).std()
merged['pct_zscore'] = (merged['spread_pct'] - merged['pct_ma']) / merged['pct_std']

pct_z_2019 = merged.loc[mask_2019, 'pct_zscore'].dropna()
print(f"\n=== 2019年 价差百分比Z-score ===")
print(f"count: {len(pct_z_2019)}")
print(f"mean: {pct_z_2019.mean():.4f}, std: {pct_z_2019.std():.4f}")
print(f"min: {pct_z_2019.min():.4f}, max: {pct_z_2019.max():.4f}")
for threshold in [0.20, 0.30, 0.40, 0.50]:
    long_signals = (pct_z_2019 < -threshold).sum()
    short_signals = (pct_z_2019 > threshold).sum()
    print(f"  threshold={threshold}: long={long_signals}, short={short_signals}")
