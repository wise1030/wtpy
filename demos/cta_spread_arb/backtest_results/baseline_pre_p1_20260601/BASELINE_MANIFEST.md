# Baseline Manifest — Pre-P1 Refactor

**Date**: 2026-06-01 16:39  
**Code Version**: rbhc v8.1 (auto_profile + Hurst+R²联合判定)  
**Backtest Range**: 202501200900 → 202510151500 (181 trading days)  
**Capital**: 1,000,000  
**Slippage**: 0  
**Config**: config/strategy_config.yaml (双 sub: rb_hc_2510_m5 + rb_hc_2601_m5)

## Summary Metrics

| Metric | Value |
|---|---|
| Total Return | 10.022% |
| Annual Return | 13.292% |
| Max Drawdown | 31.66% |
| Calmar | 0.420 |
| Win Rate | 50.28% |
| Sharpe (WtBtAnalyst, unreliable) | -8.53 |
| Final PnL | +5,620 |
| Signals | 2,210 records |

## File Fingerprints (SHA256)

P1/P3 重构后必须**完全一致**(允许 summary.json sharpe/std 等浮点字段 ±1e-6 误差):

```
3785bd5f2e15c30d1566b27cdd4ec2a7bf044a0196bc604cf2f90c2784fd9ad3  signals.csv
3ed238182633ba030c30a52998f42850b6991a987bf00151eba1c62769c5539b  positions.csv
0958bfa77ef5b8247d81104848c7db7244e4c27c39eea78c0fb5a55908ade865  funds.csv
324d2d955a816e40a6af54a4acc0cc39e7b7ad588b51a6c937a525648322e895  marks.csv
d85b5cdfaf4be1a99c7c8c8f948165a4ed6aab4cd64dd4345804a34108c4fea6  summary.json
```

## Recovery

如需复现 baseline:
```bash
cd demos/cta_spread_arb_rbhc  # P0 之前的路径
python runBT.py --start 202501200900 --end 202510151500 --capital 1000000
diff -q outputs_bt/spread_arb_rbhc/signals.csv backtest_results/baseline_pre_p1_20260601/signals.csv
```

## P3 回归测试通过判定

- positions.csv, signals.csv, marks.csv, funds.csv → **逐字节一致**
- summary.json → 关键字段 (total_return/annual_return/max_falldown/win_rate) 浮点完全一致

任一文件出现差异即视为 P3 改造引入了不期望的行为变化，必须定位 root cause。
