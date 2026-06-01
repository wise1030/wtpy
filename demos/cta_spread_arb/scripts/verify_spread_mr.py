"""验证spread=rb-6i的均值回归特性"""
import csv
import numpy as np

# 读取indice.csv
data = {}
with open('outputs_bt/spread_arb/indice.csv') as f:
    reader = csv.reader(f)
    next(reader)
    for row in reader:
        t, name, line, val = row[0], row[1], row[2], float(row[3])
        if t not in data:
            data[t] = {}
        data[t][line] = val

times = sorted(data.keys())
spreads = [data[t].get('spread', 0) for t in times]
signals = [data[t].get('signal', 0) for t in times]

spreads = np.array(spreads)
signals = np.array(signals)

print(f"spread范围: [{spreads.min():.0f}, {spreads.max():.0f}]")
print(f"spread均值: {spreads.mean():.0f}, std: {spreads.std():.0f}")

# ADF检验
from statsmodels.tsa.stattools import adfuller
adf_result = adfuller(spreads, maxlag=1)
print(f"\nADF检验: statistic={adf_result[0]:.3f}, p-value={adf_result[1]:.4f}")
print(f"  p<0.05: {'协整(均值回归)' if adf_result[1] < 0.05 else '不协整(无均值回归)'}")

# 计算半衰期
dy = np.diff(spreads)
y_lag = spreads[:-1]
matrix = np.vstack([y_lag, np.ones(len(y_lag))]).T
result = np.linalg.lstsq(matrix, dy, rcond=None)
beta = result[0][0]
if beta < 0:
    half_life = -np.log(2) / beta
    print(f"半衰期: {half_life:.1f} bars ({half_life*5/60:.1f}小时)")
else:
    print(f"beta={beta:.4f} >= 0, 非均值回归!")

# MR正确率: spread偏离均值后是否回归
lookback = 60
correct = 0
total = 0
for i in range(lookback, len(spreads)-5):
    recent = spreads[i-lookback:i]
    mean = np.mean(recent)
    std = np.std(recent)
    if std == 0:
        continue
    z = (spreads[i] - mean) / std
    if abs(z) > 1:
        total += 1
        # 5bar后spread是否回归
        change = spreads[i+5] - spreads[i]
        if (z > 1 and change < 0) or (z < -1 and change > 0):
            correct += 1

if total > 0:
    print(f"\nMR回归正确率(|z|>1, 5bar): {correct/total*100:.1f}% 样本={total}")

# 更长horizon
correct20 = 0
total20 = 0
for i in range(lookback, len(spreads)-20):
    recent = spreads[i-lookback:i]
    mean = np.mean(recent)
    std = np.std(recent)
    if std == 0:
        continue
    z = (spreads[i] - mean) / std
    if abs(z) > 1:
        total20 += 1
        change = spreads[i+20] - spreads[i]
        if (z > 1 and change < 0) or (z < -1 and change > 0):
            correct20 += 1

if total20 > 0:
    print(f"MR回归正确率(|z|>1, 20bar): {correct20/total20*100:.1f}% 样本={total20}")

# 对比: 旧spread=rb-i的MR特性
# 用原始价格重新计算
print("\n=== 对比: 如果用旧spread=rb-i ===")
# 从spread=rb-6i反推rb-i不可行，需要原始价格
# 但我们可以看: 当前spread的MR特性是否足够好
print(f"当前spread(rb-6i) ADF p-value: {adf_result[1]:.4f}")
if adf_result[1] < 0.05:
    print("→ 协整关系成立，MR策略可行")
else:
    print("→ 协整关系不成立，MR策略不可行!")
    print("  建议: 调整lots/multiplier使名义价值匹配，或换用趋势策略")
