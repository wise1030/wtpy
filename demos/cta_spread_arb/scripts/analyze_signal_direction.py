import csv

# 读取indice.csv - 按时间+index_name分组
data = {}  # {time: {line_name: value}}
with open('outputs_bt/spread_arb/indice.csv') as f:
    reader = csv.reader(f)
    next(reader)
    for row in reader:
        t, name, line, val = row[0], row[1], row[2], float(row[3])
        if t not in data:
            data[t] = {}
        data[t][line] = val

times = sorted(data.keys())
print(f"总时间点数: {len(times)}")

# 1. 信号方向正确率: signal>0 → spread下一bar上升
n_sig = 0
correct_1bar = 0
correct_5bar = 0
n5 = 0

for i in range(1, len(times)):
    t = times[i]
    t_prev = times[i-1]
    d = data[t]
    d_prev = data[t_prev]
    
    s = d.get('signal', 0)
    sp = d.get('spread', 0)
    sp_prev = d_prev.get('spread', 0)
    change = sp - sp_prev
    
    if abs(s) > 0.3:
        n_sig += 1
        if (s > 0.3 and change > 0) or (s < -0.3 and change < 0):
            correct_1bar += 1

# 5bar horizon
for i in range(5, len(times)):
    t = times[i]
    t_prev5 = times[i-5]
    d = data[t]
    d_prev5 = data[t_prev5]
    
    s = d.get('signal', 0)
    sp = d.get('spread', 0)
    sp_prev = d_prev5.get('spread', 0)
    change5 = sp - sp_prev
    
    if abs(s) > 0.3:
        n5 += 1
        if (s > 0.3 and change5 > 0) or (s < -0.3 and change5 < 0):
            correct_5bar += 1

print(f"1bar信号正确率(|s|>0.3): {correct_1bar/n_sig*100:.1f}% 样本={n_sig}")
print(f"5bar信号正确率(|s|>0.3): {correct_5bar/n5*100:.1f}% 样本={n5}")

# 2. 按信号强度分层
for threshold in [0.2, 0.3, 0.4, 0.5]:
    n = 0
    correct = 0
    for i in range(1, len(times)):
        t = times[i]
        t_prev = times[i-1]
        d = data[t]
        d_prev = data[t_prev]
        s = d.get('signal', 0)
        sp = d.get('spread', 0)
        sp_prev = d_prev.get('spread', 0)
        change = sp - sp_prev
        if abs(s) > threshold:
            n += 1
            if (s > threshold and change > 0) or (s < -threshold and change < 0):
                correct += 1
    if n > 0:
        print(f"  阈值>{threshold}: 正确率={correct/n*100:.1f}% 样本={n}")

# 3. 反转信号正确率
n_rev = 0
correct_rev = 0
for i in range(1, len(times)):
    t = times[i]
    t_prev = times[i-1]
    d = data[t]
    d_prev = data[t_prev]
    s = d.get('signal', 0)
    sp = d.get('spread', 0)
    sp_prev = d_prev.get('spread', 0)
    change = sp - sp_prev
    if abs(s) > 0.3:
        n_rev += 1
        # 反转: signal>0 → spread下降, signal<0 → spread上升
        if (s > 0.3 and change < 0) or (s < -0.3 and change > 0):
            correct_rev += 1

print(f"\n反转信号正确率: {correct_rev/n_rev*100:.1f}% 样本={n_rev}")

# 4. signal与zscore方向关系
same_sign = 0
opp_sign = 0
for t in times:
    d = data[t]
    s = d.get('signal', 0)
    z = d.get('zscore', 0)
    if abs(s) > 0.1 and abs(z) > 0.1:
        if (s > 0 and z > 0) or (s < 0 and z < 0):
            same_sign += 1
        else:
            opp_sign += 1

print(f"\nsignal与zscore同号: {same_sign}, 异号: {opp_sign}")
total = same_sign + opp_sign
if total > 0:
    print(f"异号占比: {opp_sign/total*100:.1f}% (MR取反逻辑下应100%异号)")

# 5. 检查spread变化与signal的关系(不做方向假设)
# signal>0.3时, spread变化的均值
pos_changes = []
neg_changes = []
for i in range(1, len(times)):
    t = times[i]
    t_prev = times[i-1]
    d = data[t]
    d_prev = data[t_prev]
    s = d.get('signal', 0)
    sp = d.get('spread', 0)
    sp_prev = d_prev.get('spread', 0)
    change = sp - sp_prev
    if s > 0.3:
        pos_changes.append(change)
    elif s < -0.3:
        neg_changes.append(change)

import numpy as np
if pos_changes:
    print(f"\nsignal>0.3时spread变化: 均值={np.mean(pos_changes):.2f} 中位数={np.median(pos_changes):.2f} 样本={len(pos_changes)}")
if neg_changes:
    print(f"signal<-0.3时spread变化: 均值={np.mean(neg_changes):.2f} 中位数={np.median(neg_changes):.2f} 样本={len(neg_changes)}")

# 6. 交易盈亏分析: 按方向
closes = []
with open('outputs_bt/spread_arb/closes.csv') as f:
    reader = csv.reader(f)
    next(reader)
    for row in reader:
        closes.append({
            'code': row[0], 'direct': row[1], 'opentime': row[2],
            'openprice': float(row[3]), 'closeprice': float(row[5]),
            'qty': int(row[6]), 'profit': float(row[7])
        })

# 配对分析
long_profits = []
short_profits = []
for i in range(0, len(closes), 2):
    if i+1 < len(closes):
        pair_profit = closes[i]['profit'] + closes[i+1]['profit']
        if closes[i]['direct'] == 'LONG':
            long_profits.append(pair_profit)
        else:
            short_profits.append(pair_profit)

print(f"\n做多spread配对: 数={len(long_profits)} 均盈亏={np.mean(long_profits):.0f} 总={sum(long_profits):.0f}")
print(f"做空spread配对: 数={len(short_profits)} 均盈亏={np.mean(short_profits):.0f} 总={sum(short_profits):.0f}")
