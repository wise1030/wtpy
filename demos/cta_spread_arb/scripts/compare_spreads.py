"""对比不同spread定义的MR特性"""
import csv
import numpy as np
from statsmodels.tsa.stattools import adfuller

# 读取原始价格
# 从indice.csv中无法反推原始价格，需要从CSV数据文件读取
import os
import sys

# 直接从回测数据读取
rb_prices = []
i_prices = []

rb_file = '/mnt/d/gf_pc/WonderTrader/wtpy/demos/storage/csv/SHFE.rb.HOT_m5.csv'
i_file = '/mnt/d/gf_pc/WonderTrader/wtpy/demos/storage/csv/DCE.i.HOT_m5.csv'

with open(rb_file) as f:
    reader = csv.reader(f)
    next(reader)  # skip header
    for row in reader:
        rb_prices.append(float(row[4]))  # close

with open(i_file) as f:
    reader = csv.reader(f)
    next(reader)  # skip header
    for row in reader:
        i_prices.append(float(row[4]))  # close

min_len = min(len(rb_prices), len(i_prices))
rb = np.array(rb_prices[:min_len])
ip = np.array(i_prices[:min_len])

print(f"数据长度: {min_len}, rb范围: [{rb.min()}, {rb.max()}], i范围: [{ip.min()}, {ip.max()}]")

# 不同spread定义
spreads = {
    'rb - i (旧)': rb - ip,
    'rb - 6*i (新, lots×mult归一化)': rb - 6*ip,
    '50*rb - 300*i (PnL直接)': 50*rb - 300*ip,
}

for name, spread in spreads.items():
    adf_result = adfuller(spread, maxlag=1)
    
    # 半衰期
    dy = np.diff(spread)
    y_lag = spread[:-1]
    matrix = np.vstack([y_lag, np.ones(len(y_lag))]).T
    result = np.linalg.lstsq(matrix, dy, rcond=None)
    beta = result[0][0]
    if beta < 0:
        half_life = -np.log(2) / beta
        hl_str = f"{half_life:.1f} bars ({half_life*5/60:.1f}小时)"
    else:
        hl_str = "非均值回归"
    
    # MR正确率
    lookback = 60
    correct5 = 0
    total5 = 0
    correct20 = 0
    total20 = 0
    for i in range(lookback, len(spread)-20):
        recent = spread[i-lookback:i]
        mean = np.mean(recent)
        std = np.std(recent)
        if std == 0:
            continue
        z = (spread[i] - mean) / std
        if abs(z) > 1:
            total5 += 1
            change5 = spread[i+5] - spread[i]
            if (z > 1 and change5 < 0) or (z < -1 and change5 > 0):
                correct5 += 1
            total20 += 1
            change20 = spread[i+20] - spread[i]
            if (z > 1 and change20 < 0) or (z < -1 and change20 > 0):
                correct20 += 1
    
    print(f"\n=== {name} ===")
    print(f"  范围: [{spread.min():.0f}, {spread.max():.0f}]")
    print(f"  ADF p-value: {adf_result[1]:.4f} {'协整' if adf_result[1] < 0.05 else '不协整'}")
    print(f"  半衰期: {hl_str}")
    if total5 > 0:
        print(f"  MR正确率(5bar): {correct5/total5*100:.1f}% 样本={total5}")
    if total20 > 0:
        print(f"  MR正确率(20bar): {correct20/total20*100:.1f}% 样本={total20}")
