# cta_spread_arb 项目评估报告（2026-06-09）

> 上次评估：`PROJECT_ASSESSMENT_20260605.md`
> 本次评估视角：功能架构 + 业务流程 + ROI 优化路径

## 1. 项目现状盘点

### 1.1 代码规模

| 模块 | 文件数 | 行数 | 备注 |
|---|---|---|---|
| `strategies/` | 4 | 876 | ArbCore(415) + Lifecycle(119) + SpreadArb(339) |
| `helpers/` (含 pipeline) | 23 | 4,596 | 12 个组件 + 7 个 pipeline step |
| `factors/` | 7 | 1,306 | 7 个因子文件，FactorEngine 385 行 |
| `tools/` | 2 | 398 | PairScreener + BacktestAnalyzer |
| `scripts/` | 9 | ~1,500 | walk_forward/factor_ic/profile_bt 等 |
| `tests/` | 18 | - | **153/153 全绿** ✅ |
| 顶层 (runBT/runLive/run_multi/run_batch/schemas) | 7 | ~1,200 | - |
| **合计** | **70** | **~7,900** | + 44 个 yaml/json/md |

### 1.2 配置/数据

- `config/experiments/`: default + l_pp_2509/2609 + rb_hc_2510/2601 + profile_p3_6
- `config/profiles/`: trending_contract + mr_contract（auto_profile 自动选）
- `config/contracts/`: l_pp + rb_hc
- 数据源：KS S3 (kline_1d/1m/15m/1h)，CSV 转 DSB 入回测

### 1.3 已完成的关键能力

- ✅ Pipeline 化（context/entry/exit/regime/persist）
- ✅ auto_profile：R²>0.55 → trending / 否则 mr
- ✅ RegimeDetector 三态（range/mr/trend）
- ✅ DiagLogger jsonl 全量埋点
- ✅ trend_blocked 软衰减（×0.5）
- ✅ walk_forward 框架（已验证 rb_hc_2601）
- ✅ factor_ic_analysis 工具
- ✅ PairScreener 工具（未集成主流程）
- ✅ P0-γ trend_block 硬阻断（保留，当前 threshold 下 unreachable）

---

## 2. C2 诊断结论 — rb/hc 结构性亏损根因

### 2.1 三合约横向对比

| 指标 | rb/hc 2601 (mr) | rb/hc 2510 (trending) | l/pp 2609 (trending) |
|---|---|---|---|
| 交易笔数 | 616 | 634 | 382 |
| Gross PnL | +470 | +840 | **+1,340** |
| Fee 合计 | -2,551 | -2,719 | -1,041 |
| **Net PnL** | **-2,081** | **-1,879** | **+299** |
| 单笔 avg_win/avg_loss | 141 / -152 | 168 / -176 | 280 / -284 |
| 单轮 fee (开+平) | 4.14 | 4.28 | 2.72 |
| **fee / avg_win** | **1.47%** | **1.27%** | **0.49%** |
| 盈亏平衡胜率 | 51.5% | 51.3% | 50.5% |
| 实际胜率 | 49.7% | 50.5% | 50.3% |
| Win capture | n/a | 71.2% | 82.8% |
| Loss drag | n/a | 72.6% | 80.3% |

### 2.2 根因分析

**rb 费率按金额（万分之1，~3.2 元/手）vs hc 按手数（~1 元/手）**：
- `SHFE.rb.2601`: avg_fee = 3.20 元/手（3198 × 10 × 0.0001 = 3.20 ✓）
- `SHFE.hc.2601`: avg_fee = 0.94 元/手（按手数定额）
- `DCE.l.2609`:  avg_fee = 1.25 元/手
- `DCE.pp.2609`: avg_fee = 1.47 元/手

**叠加效应**：
1. rb/hc 单轮 fee 是 l/pp 的 **1.57 倍**（4.28 vs 2.72）
2. rb/hc spread 波动幅度只有 l/pp 的 **60%**（avg_win 168 vs 280）
3. 两者相乘 → fee/avg_win 是 l/pp 的 **2.5 倍**（1.27% vs 0.49%）

### 2.3 结论

**rb/hc 在当前价差套利框架下结构性不可盈利**：
- 盈亏平衡胜率 51.3%，实际胜率 50.5%，gap 0.8pp 已在策略本身的统计波动内
- profile（mr vs trending）只能影响 ±100 元的 net PnL，扭转不了 fee 结构性问题
- 持仓时长分桶分析：所有桶 avg_pnl 趋近 0，无"黄金时长"可优化

**不是策略调参/profile 切换能解决的，是品种经济性问题。**

### 2.4 P0-γ 状态

- 代码已加注释保留（ArbCore.py:347+），未来 threshold 下调时启用
- 单测 4 项保留作回归保护
- 当前实验所有 ON/OFF 完全等价，因 entry_judge 软衰减 ×0.5 已挡住 should_trade

---

## 3. 未完成功能与优化路径

### 3.1 业务功能（按 ROI 排序）

| 优先级 | 项 | 价值 | 工作量 | 风险 |
|---|---|---|---|---|
| **P1-A** | **实验矩阵化** `run_matrix.py` | 每次实验节省 30min | 4h | 低 |
| **P1-B** | PairScreener 集成主流程（fee/avg_win > 阈值警告） | 防止再踩 fee 坑 | 2h | 中 |
| **P2-B** | l/pp walk-forward 4-6 折验证 | 确认 +299 非过拟合 | 6h（跑时间长） | 中 |
| **C2-exit** | l/pp exit capture 82% → 90%+ | 净 PnL +60~100/合约 | 4h | 中 |
| **P2-A** | 因子 IC 自适应权重 | 期望 +20% alpha | 8h | 高（动因子核心） |
| **C4** | regime_detector lookback 30/60/120 对照 | +5~10% | 3h | 低 |
| **P3** | 实盘准入 | 最终交付 | 8h+ | 高 |

### 3.2 架构优化

| 项 | 现状 | 建议 |
|---|---|---|
| **run_multi + run_batch + walk_forward** | 三个入口职责重叠 | 合并为统一 `run_matrix.py` |
| **factor_ic_analysis** | 374 行独立脚本，不联动 | 接入 SignalGen 自动降权 |
| **PairScreener** | 184 行独立工具 | 接入 preflight 自动跑 |
| **ArbCore 415 行** | P0-γ 注释后偏臃肿 | 暂不动，先把 P1/P2 业务完了 |

---

## 4. 本次执行路径（已拍板）

| 步骤 | 内容 | 状态 |
|---|---|---|
| **C2 诊断落盘** | 本报告 | ✅ |
| **P0-γ 代码加注释** | ArbCore.py:347+ | ✅ |
| **rb/hc 处理** | 保留做数据验证（不弃用） | ✅ |
| **P1-A 实验矩阵化** | `run_matrix.py` 笛卡尔积 + 并行 + 汇总 | 🚧 进行中 |
| **P2-B l/pp walk-forward** | 4-6 折，矩阵化后做 | ⏳ 待 P1-A |
| **C2-exit l/pp capture 优化** | 82% → 90%+ | ⏳ 待 P2-B |

---

## 5. 关键决策记录

1. **rb/hc 保留**：作为 "结构性亏损" 的对照样本，用于 PairScreener 标定阈值
2. **P0-γ 不撤回**：当前 threshold 下 unreachable，但作为兜底/未来 threshold 调整的安全网
3. **C2 不调参修 rb/hc**：诊断证明无解，调参纯浪费
4. **优先 l/pp 链路打通**：l/pp +299 是当前唯一正样本，必须 walk-forward 确认稳定后才能进 C2-exit

---

## 6. 风险与盲点

1. **样本极小**：l/pp 仅一个合约期 (+299)，需 walk-forward 防过拟合
2. **WT 日期解析 bug**（2025→3509）：log/jsonl 文件名仍受污染，不影响 PnL 但影响诊断脚本
3. **mr_contract profile 的 408% fee 比** (rb_hc 2601)：本质同 rb/hc 结构性问题，profile 层无解
4. **实盘 0 验证**：runLive.py 未做端到端干跑
