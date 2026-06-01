#!/usr/bin/env python3
import json

with open('opt_results.json') as f:
    data = json.load(f)

traded = [r for r in data if r.get('sharpe', -999) not in (9999, -999)]
traded.sort(key=lambda x: x.get('sharpe', -999), reverse=True)

print(f"Total:{len(data)} Traded:{len(traded)} NoTrade:{len(data)-len(traded)}")
print()
for i, r in enumerate(traded[:10], 1):
    p = r['params']
    s = r['sharpe']
    w = r['win_rate']
    ret = r['total_return']
    e = p['entry_threshold']
    x = p['exit_threshold']
    d = p['direction_threshold']
    m = p['signal_mode']
    dv = p['max_spread_deviation']
    l = p['max_loss_per_pair']
    print(f"#{i} S={s:.2f} W={w:.1f}% R={ret:.2f}% e={e} x={x} d={d} m={m} dev={dv} loss={l}")