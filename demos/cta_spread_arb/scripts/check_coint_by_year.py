"""检查不同年份的协整特性"""
import csv
import numpy as np
from statsmodels.tsa.stattools import adfuller

rb_file = '/mnt/d/gf_pc/WonderTrader/wtpy/demos/storage/csv/SHFE.rb.HOT_m5.csv'
i_file = '/mnt/d/gf_pc/WonderTrader/wtpy/demos/storage/csv/DCE.i.HOT_m5.csv'

rb_data = []
i_data = []
with open(rb_file) as f:
    reader = csv.reader(f)
    next(reader)
    for row in reader:
        rb_data.append({'date': row[0][:8], 'close': float(row[4])})

with open(i_file) as f:
    reader = csv.reader(f)
    next(reader)
    for row in reader:
        i_data.append({'date': row[0][:8], 'close': float(row[4])})

min_len = min(len(rb_data), len(i_data))
rb_dates = [d['date'] for d in rb_data[:min_len]]
rb_closes = np.array([d['close'] for d in rb_data[:min_len]])
i_closes = np.array([d['close'] for d in i_data[:min_len]])

# 按年份分析
years = sorted(set(d for d in rb_dates))
print("=== 不同年份spread=rb-6i的协整特性 ===")
for year in years:
    mask = np.array([d.startswith(year) for d in rb_dates])
    if mask.sum() < 100:
        continue
    rb_y = rb_closes[mask]
    i_y = i_closes[mask]
    spread = rb_y - 6 * i_y
    
    adf_result = adfuller(spread, maxlag=1)
    
    # 半衰期
    dy = np.diff(spread)
    y_lag = spread[:-1]
    if np.std(y_lag) == 0:
        continue
    matrix = np.vstack([y_lag, np.ones(len(y_lag))]).T
    result = np.linalg.lstsq(matrix, dy, rcond=None)
    beta = result[0][0]
    if beta < 0:
        half_life = -np.log(2) / beta
        hl_str = f"{half_life:.0f} bars ({half_life*5/60:.0f}h)"
    else:
        hl_str = "非MR"
    
    # MR正确率
    lookback = 60
    correct = 0
    total = 0
    for i in range(lookback, len(spread)-20):
        recent = spread[i-lookback:i]
        mean = np.mean(recent)
        std = np.std(recent)
        if std == 0:
            continue
        z = (spread[i] - mean) / std
        if abs(z) > 1:
            total += 1
            change = spread[i+20] - spread[i]
            if (z > 1 and change < 0) or (z < -1 and change > 0):
                correct += 1
    
    mr_rate = f"{correct/total*100:.1f}%" if total > 0 else "N/A"
    coint = "✓" if adf_result[1] < 0.05 else "✗"
    print(f"  {year}: ADF p={adf_result[1]:.3f} {coint} | 半衰期={hl_str} | MR正确率(20bar)={mr_rate} n={total}")
