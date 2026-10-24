# cta_turtle 策略与工程复核（2026-10-20）

> **2026-10-20 更新**：本报告的第一批 P0/P1 已落地（资金口径、累计风险、
> 真值隔离、日损闸、若干确定性缺陷、组合约束回放），测试 386→**413**。
> 实施明细与复现命令见 **§9**；§0-§8 保留为复核时的原始快照。
>
> 范围：`demos/cta_turtle` 全部源码、配置、测试、回测产物与仓库内生产快照。
> 方法：核心文件逐行精读 + 386 例测试复跑 + 配置数值回放 + 回测交易/持仓重建 +
> C++ 源码核对（`wondertrader/src/WtCore`、`WtRiskMonFact`）。
> 说明：本报告不重复早前工程向审阅（REVIEW_2026-10-18，已被本报告 §10
> 对账取代，文件随目录治理移除）的结论，
> 重点回答四个问题：**架构是否可持续、业务功能是否闭环、策略/资金口径是否自洽、
> 代码优化从哪里下手**；并给出按投入产出排序的路线图。

---

## 0. 结论摘要

### 0.1 总体判断

这是一套**工程质量明显高于同类 CTA demo** 的系统：

- 纯逻辑引擎与框架壳分离，`TurtleEngine` 可脱离 wtpy 单测；
- 决策-确认两段式、边沿触发、S1 过滤 + fail-safe、棘轮 N；
- 可插拔过滤器、双层风控、真值源/逐笔成交证据链、幽灵仓自愈；
- 386 例测试全绿（本次复跑 20.6s），数据管道/部署链/运维手册齐全；
- 生产 12 品种、6 个持仓、连续多日运行，事故复盘都沉淀成了代码与测试。

**未发现会让信号状态机算错的 P0 级逻辑错误。** 但发现三个**口径级问题**，
它们不会让某根 bar 算错，却会让“回测结果、实盘行为、研究报告”三者互相对不上，
从而直接误导仓位与资金决策：

1. **资本基数三套并存**：回测 `--capital 5,000,000`、策略风控
   `fallback_capital 15,000,000`、C++ `riskmon.base_amount 5,000,000`；
   且 `unit_var=200,000` 与 `max_risk_per_mkt=2%` 数学上不匹配，
   所有品种实际只能做到名义单位的 **61.5%~75%**。
2. **矩阵回测没有执行组合约束**：每个品种独立进程、独立 `RiskManager`，
   `max_units_total=6` / `max_units_per_group=2` 在回测里**从未生效**。
   用交易记录重建持仓后，50.5% 的交易日同时持仓 >6，化工组 42.4% 的交易日 >2。
   因此 `47.4% / 回撤 30.1% / Sharpe 0.27` 是**无约束上界**，不是实盘可达结果。
3. **真值源没有回测隔离**：`on_init` 无条件探测 `rtdata.json`，
   成交确认与 ghost trim 直接读真值。回测目录里只要有一份“新鲜”的实盘真值
   （`truth_max_age` 生产配了 86400s），回测就可能按**真实账户持仓**裁剪/确认，
   比 2026-09-15 的“陈旧真值污染”更隐蔽。

此外还有若干可复现的 P1：N 缺失时丢弃触发决策但不清 `pending`（9/4 孤儿
pending 事故的同型路径）、多速度名称触发 `TurtleEngine` 断言、日评估路径的
流动性过滤器 fail-open、`_reanchor_on_roll` 与成交回写顺序相反等。

### 0.2 最优先的 8 件事

| 优先级 | 动作 | 一句话理由 |
|---|---|---|
| P0 | 统一“总资金 / 单策略资金 / 单位风险”三个口径，并在 `ConfigLoader` 做交叉校验 | 现在 5M/15M/20万/2% 四个数字互相矛盾，回测与实盘不可比 |
| P0 | 把 `max_units_*`、`max_total_risk_frac`、保证金/可用资金做成**组合级回测** | 当前矩阵 12 个品种同时满仓，实盘只能 6 个 |
| P0 | 回测禁用真值源（`is_backtest` 闸），并把配置/数据哈希写进 summary | 防止实盘账户污染研究结果，保证结果可复现 |
| P0 | 让 `riskmon` 真正能降仓：`risk_scale<1` 或删除该层、在策略层实现日损闸 | 现在 `risk_scale=1.0`，触发后“降到 100%”，等于没有熔断 |
| P1 | `max_risk_per_mkt` 改为**按品种累计**（或改名并重新标定总预算） | 当前 4 层可做到 ~8%/品种，单品种就能吃掉 10% 总预算的大半 |
| P1 | N 缺失丢弃触发决策时同步 `clear_pending()` | 这是孤儿 pending 事故的同型入口，只需一行防御 |
| P1 | 多速度 `name` 与引擎 `system` 解耦（或校验只能是 S1/S2） | 配 `fast/slow` 直接 AssertionError，功能不可用 |
| P1 | 执行层增加订单状态机/订单号归因/参与率上限 | 10-09 零派发、分批窗口、共享账户归属都指向这里 |

---

## 1. 架构审阅

### 1.1 分层与运行拓扑

```
研究/回测   runBT / run_matrix / run_walkforward / run_slippage_sens / run_speed_study
              │  (独立进程；CtaMocker 单上下文 → 每品种一个进程)
数据管道     scripts/update_{contracts,hots,hot_daily}.py / fetch_data_{sina,tqsdk}.py
              │  (systemd timer: 20:30 / 20:35 / 20:40)
部署         make_deploy.py → deploy_tpl/run.py (交易) + runDt.py (datakit)
              │  WatchDog + systemd + WtMonSvr
策略壳       StraTurtle.py (2324 行 / 75 个方法 + 3 个 property)
              ├─ filters/          只读过滤链（方向闸/ADX/波动带/流动性）
              ├─ helpers/RiskManager.py  单品种/组合/保证金/单位/账户资金五道闸
              ├─ helpers/LiveTruth.py    真值源 + 逐笔成交
              ├─ helpers/PositionSizer.py VaR 反算
              ├─ helpers/BarJournal.py   K 线旁路
              └─ helpers/DataChecker.py  数据预检
引擎         helpers/TurtleEngine.py (971 行，零框架依赖)
数据访问     CsvDataLoader / indicators / portfolio
```

分层方向正确。**最值得保留的设计**：

1. 引擎纯逻辑、可单测；
2. 决策-确认两段式（被否决的入场不污染 S1 过滤器状态）；
3. 边沿触发 + 有状态 `_fs_armed` 的 fail-safe；
4. N 口径三选一（`entry/current/ratchet`）且有 A/B 与风险论证；
5. 证据链降级有序（真值增量 > 逐笔成交 > fees 代理），fees 只在真值缺席时采信；
6. 事故驱动出来的自愈：孤儿 pending、catchup 可重试闸、ghost trim、
   exec recover、hotdaily 重启；
7. 配置 fail-fast（未知字段拒绝）+ 敏感口径加载留痕。

### 1.2 架构级问题

#### A1. `StraTurtle` 是 God class（P1）

2324 行、75 个方法（另有 3 个 property），同时承担：生命周期、图表、单/双系统两套执行路由、
风控门控、过滤链、真值源、成交回填、幽灵自愈、执行单元自愈、持久化
（两处重复组装快照）、K 线旁路、资金旁路、涨跌停检测。

这不是“代码不好看”的问题，而是**变更爆炸半径**：历史上 session 路径漏挂
`_reanchor_on_roll`、双系统 re-anchor 口径不一致，都是巨型嵌套结构的产物。
`_eval_core` 只收敛了评估路径，执行/自愈/真值三条路径仍是分散的。

建议拆分（保持测试全绿，逐步迁移）：

| 模块 | 迁出内容 | 约行数 |
|---|---|---|
| `StraTurtleCore` | 生命周期/图表/子系统组装 | ~450 |
| `exec/ExecutionRouter` | `_execute` / `_execute_dual` / `_size_and_cap` / `_apply_*_gates` / `_run_filter_chain` / `_reject_trigger` | ~550 |
| `exec/FillTracker` | `_fill_pending` / `_fill_confirmed` / `_reanchor_*` / `_maybe_ghost_trim` / `_maybe_exec_recover` | ~450 |
| `exec/TruthMonitor` | `_truth_*` / `_periodic_truth_check` / `_account_available` / `_reconcile` | ~350 |
| `exec/Telemetry` | `_alert` / `_veto_log` / `_dump_fund_live` / journal 桥接 | ~200 |

接口边界即测试边界：现有测试大多用假 context 驱动，迁移成本低。

#### A2. 单/双系统门控链双实现（P1）

`_size_and_cap`（单系统，`StraTurtle.py:1458`）与
`_apply_enter_gates` / `_apply_add_gates`（双系统，`:1802` / `:1846`）
近重复，日志 key 口径还有微妙差异。双系统当前是休眠代码（生产全单系统），
但多速度也会走双系统路径，未来启用时极易漂移。

建议：抽一个 `GateResult` + `run_entry_gates(kind, sub, dec)`，单/双系统只保留
“逐子系统调用”和“净额执行”的差异；日志 key 由统一函数生成。

#### A3. 回测与实盘的模式分支过多（P1）

`entry_mode=close|trigger`、`bt_no_simtick`、`intrabar_stop`、`truth` 自动探测、
`is_backtest`、`WT_EXEC_RECOVER`……同一个 `StraTurtle` 里有 6 组模式开关。
每新增一组，测试矩阵就翻倍，且组合爆炸会产生“只在某个模式下才出现”的 bug
（`with_oi=False` 就是一例）。

建议：把“撮合/成交确认/数据源”收敛为 `ExecutionModel` 接口：
- `BacktestModel`：bar/tick 模拟、无真值；
- `LiveModel`：真值 + 逐笔成交 + 执行单元；
策略只依赖接口，不再到处 `if is_backtest`。

#### A4. 源码树里的陈旧 `deploy/` 快照（P1）

`deploy/cta_turtle/` 是 2026-09-20 的旧快照：
`StraTurtle.py` 2221 行 vs 当前 2324 行；`RiskManager.py` 218 行 vs 228 行；
配置还是 `max_margin_usage: 0.60`、`fallback_capital: 5,000,000`、
全局保证金扣减逻辑。仓库里同时存在“当前源码”和“两代前的部署副本”，
极易误改/误打包/误对照。

建议：删除该目录，统一由 `make_deploy.py` 生成到 `build/deploy/`；
或至少纳入 `deploy.manifest.json` 并在 README 标注“只读产物”。

#### A5. 数据/状态与代码同树（P2）

`generated/`、`Logs/`、`stradata/`、`outputs_bt/` 与源码同级；
`stradata/` 还有手工手术的 `.bak` 文件。测试会向
`generated/outputs/tb/fund_live.csv` 真实追加（`_dump_fund_live` 无
`is_backtest` 守卫）。建议运行时数据统一放 `var/` 或 `runtime/`，
测试用 tmp_path/环境变量隔离。

---

## 2. 业务功能审阅

### 2.1 功能清单与完成度

| 功能 | 状态 | 说明 |
|---|---|---|
| 趋势信号 S1（20/10 + 盈利跳过 + 55 fail-safe） | ★★★★★ | 生产默认，逻辑与测试完整 |
| 趋势信号 S2（55/20） | ★★★★☆ | 可用，但生产未启用；双系统休眠 |
| 多速度（speeds） | ★★☆☆☆ | 研究与配置骨架，生产未用；名称断言会崩 |
| 触发模式（tick 盘中触发） | ★★★★☆ | 生产默认；回测无 m5 时退化为日 bar 触碰 |
| 收盘模式 | ★★★★☆ | 回测/对照用；同 bar 反转与跳空止损有乐观偏差 |
| 加仓金字塔 / 2N 止损 / 棘轮 N | ★★★★★ | 风险口径论证充分 |
| 可插拔过滤器 | ★★★☆☆ | 框架完整，**生产零启用**；日评估路径有 fail-open 差异 |
| 单品种/组合/保证金/单位/账户资金风控 | ★★★★☆ | 五道闸逻辑清晰；per-market 非累计、组合回测未执行 |
| 真值源/逐笔成交/ghost trim | ★★★★☆ | 证据链成熟；回测隔离缺失、共享账户归因有限 |
| 运行时对账/状态恢复/换月 re-anchor | ★★★★★ | 事故驱动，覆盖较全 |
| 盘中止损（m5/m1） | ★★★★☆ | 代码完整，生产未启用 |
| 多品种数据管道 | ★★★★☆ | 12 品种；依赖新浪/定时器；缺数据版本化 |
| 监控/告警/部署 | ★★★☆☆ | 监控齐；notify 未启用；runMonSvr 不在部署链 |
| h1 周期 | ★☆☆☆☆ | schema 接受，数据层实际不支持；建议直接拒绝 |
| 组合级回测 | ★☆☆☆☆ | 只有曲线合成，没有约束执行 |
| 日损/账户级熔断 | ★☆☆☆☆ | 策略层没有；C++ riskmon 实际是 no-op |

### 2.2 业务闭环的两个断点

**断点一：研究 → 实盘的组合约束断裂。**

回测是“12 个独立的 500 万账户”，实盘是“1 个共享账户 + 6 个单位上限”。
`runBT.py:58` 调 `StraTurtle.from_config(...)` 时没有传 `risk_mgr`，
于是 `StraTurtle.py:100` 为每个品种创建私有 `RiskManager`；
`run_matrix.py` 再把 12 条曲线等权平均。组合层的
`max_units_total=6`、`max_units_per_group=2`、`max_total_risk_frac=10%`
在回测里从未参与决策。

用 `outputs_bt/*/trades.csv` 重建每日净持仓后（2019-01~2026-08，1859 个交易日）：

| 指标 | 数值 |
|---|---|
| 平均同时持仓品种数 | 6.55 |
| 最大同时持仓 | 12 |
| 同时持仓 >6 的交易日 | 938 天（**50.5%**） |
| 化工组（TA/MA/pp/l）>2 的交易日 | 789 天（**42.4%**） |
| 单一品种持仓时间占比 | 约 48%~58%（IF 95%） |

这不是“回测略乐观”，而是**策略在回测里做了实盘不允许做的事**。
47.4%/30.1%/0.27 只能当作“无约束上界”；真实可达收益/回撤需要组合约束回测。

**断点二：研究报告与配置版本脱节。**

`matrix_results/` 的时间是 2026-09-16，而 `fallback_capital` 在 2026-10-08
从 5M 改成 15M。用当前配置重跑，风控权益基数变成 3 倍，
同一套信号的手数、收益、回撤都会变；README 里的 54.89% 与
`portfolio_summary.json` 的 47.42% 也不是同一次运行。
summary 里没有记录 `stop_n_mode`、`entry_mode`、`fallback_capital`、
数据截止日、配置哈希，无法判断一张表对应哪套参数。

---

## 3. 策略逻辑审阅

### 3.1 信号链（正确性复核）

已复核为正确的部分：

- **ATR**：Wilder 平滑，种子 `mean(TR[:period])`，与经典海龟 N 一致；
- **Donchian**：不含当前 bar，`upper[i]=max(highs[i-period:i])`；
- **边沿触发**：`close > upper && prev_close <= prev_upper`，避免持续站上通道重复触发；
- **S1 过滤**：上一次 S1 突破盈利 → 跳过；被跳过记 sim 单并武装 `_fs_armed`；
  sim 单按 2N/10 通道推进，终结时解除 fail-safe；
- **fail-safe 双口径**：同 bar 跳空用“已越过 55”宽松判定，滞后突破用严格边沿，
  两路径互补且有注释/测试；
- **两段式结清**：同 bar 出场在决策阶段 `_settle_s1_exit`，避免 sim 记录覆盖真实输赢；
- **棘轮 N**：止损只收紧不放宽，加仓时按新单位重锚，风险记账与真实止损一致；
- **HOT 换月**：`shift_prices` 平移层价/武装位/通道/棘轮位，不动冻结 N。

需要注意的**口径不对称**：

1. **收盘模式允许同 bar 反转，触发模式不允许。**
   `on_bar` 在 `exited_this_bar=True` 后继续评估入场（`TurtleEngine.py:166-205`），
   测试 `test_s1_same_bar_reversal` 也固化了这一行为；而 `check_bar` 明确
   “同 bar 出场后不再评估入场（路径未知，防乐观偏差）”（`:711-715`）。
   同一根大阴/大阳线，两种模式的成交假设不同。建议：
   - 把“同 bar 反转”做成配置项，默认关闭；
   - 或在收盘模式也要求“下一根 bar 才允许反向入场”，并做 A/B 量化。

2. **收盘模式的止损按止损价成交，不处理跳空。**
   `on_bar` 在 `low <= stop` 时返回 `price=stop_price`（`:145-166`）；
   如果开盘已跳空穿过止损，真实成交会明显更差。触发模式 `check_bar`
   已经按 `open` 处理跳空（`:727-735`）。生产用 trigger 所以影响有限，
   但 close 模式回测/对照不能忽略。建议 close 模式也按
   `min(open, stop)` / `max(open, stop)` 成交，或强制使用 trigger 做基线。

3. **触发模式的“当根包络”是次优入场，不是经典突破。**
   `_arm_daily` 把通道与当根 high/low 取包络（`:585-600`），
   即次日挂单位 = 今日最高/最低。如果今日已经创出 20 日新高，
   次日再买在今日高点，本质是“回踩/追涨”，与经典“突破前 20 日高点”不同。
   这在 close 模式下对应“今日收盘入场”，两种口径的成交价差异没有被量化。
   建议做一次 `channel_only` vs `envelope` 的 A/B，并把结果写进
   README 的“与经典海龟的偏离”。

4. **日 bar 触碰仿真每根最多一个加仓。**
   `check_bar` 每次调用至多一个决策（`:711-715`），`_run_trigger_bar` 对一根
   日 bar 只调一次。若一天内价格连续穿过 2~3 个 0.5N 加仓位，回测只加一层，
   而实盘 tick 会逐层加。没有 m5 的品种（仓库里只有 rb/IF/i 有 m5）会系统性
   低估金字塔层数。建议：无 m5 时在 `check_bar` 内循环处理多个 add 位，
   或明确“日 bar 只加一层”的口径并写进文档。

5. **加仓/止损的 N 口径与风控记账的交互。**
   `booked_risk(stop=...)` 用当前止损位算最坏亏损（`:466-493`），
   这是正确的；但 `max_risk_per_mkt` 是“本笔”而非“本品种累计”（见 3.2）。
   两者叠加会让单品种 4 层风险达到总预算的大半。

### 3.2 仓位与风控（本次最重要的量化发现）

#### 3.2.1 单位风险预算与单笔风险上限不匹配

配置：`unit_var=200,000`（1N 口径），`stop_atr_mult=2.0`，
`max_risk_per_mkt=2%`，`fallback_capital=15,000,000`。

- 1 个名义单位的 1N 风险 = 200,000；
- 2N 止损风险 = 400,000；
- 单笔风险上限 = 15,000,000 × 2% = 300,000；
- 300,000 / 400,000 = **0.75**。

所以 `cap_entry` 的 `mkt_cap` 永远只能给到名义手数的 75%（再叠加保证金闸，
部分品种更低）。用 2026-09-01 数据回放：

| 品种 | N | 名义手数 | 2N/手 | 单笔风险 cap | 保证金 cap | 最终 | 占名义 |
|---|---:|---:|---:|---:|---:|---:|---:|
| rb | 31.27 | 639 | 625 | 479 | 393 | 393 | **61.5%** |
| cu | 1170.41 | 34 | 11,704 | 25 | 22 | 22 | **64.7%** |
| al | 262.09 | 152 | 2,621 | 114 | 103 | 103 | **67.8%** |
| sc | 24.69 | 8 | 49,384 | 6 | 19 | 6 | **75.0%** |
| TA | 168.30 | 237 | 1,683 | 178 | 421 | 178 | **75.1%** |
| MA | 93.93 | 212 | 1,879 | 159 | 407 | 159 | **75.0%** |
| pp | 231.64 | 172 | 2,316 | 129 | 299 | 129 | **75.0%** |
| l | 195.03 | 205 | 1,950 | 153 | 307 | 153 | **74.6%** |
| m | 44.68 | 447 | 894 | 335 | 370 | 335 | **74.9%** |
| p | 151.69 | 131 | 3,034 | 98 | 121 | 98 | **74.8%** |
| CF | 218.28 | 183 | 2,183 | 137 | 146 | 137 | **74.9%** |
| IF | 77.42 | 8 | 46,451 | 6 | 9 | 6 | **75.0%** |

结论：

- 文档里的“1 unit = 20 万 1N 风险”**从未真正达到**，实际是 12.3~15 万；
- 对 rb/cu/al/CF，保证金闸比风险闸更紧，最终只有名义的 61~68%；
- 若想把 1 unit 做成 1N=1% 权益（15 万），应设 `unit_var=150,000`；
  若想保留 20 万，应设 `max_risk_per_mkt≈2.67%`；
- 建议在 `ConfigLoader` 加交叉校验：
  `units × unit_var × stop_atr_mult <= fallback_capital × max_risk_per_mkt`
  （对每个品种/全局最保守值），不满足直接报错或 WARN 并打印实际比例。

#### 3.2.2 `max_risk_per_mkt` 是“每笔”而不是“每品种”

`RiskManager.cap_entry`：

```python
rpl = n * volscale * stop_mult
mkt_cap = int(equity * share * self.cfg.max_risk_per_mkt // rpl)
used = sum(e["risk"] for e in self._book.values())
tot_cap = int(max(0.0, equity * self.cfg.max_total_risk_frac - used) // rpl)
```

`mkt_cap` **没有减去该品种已占用的风险**，只有 `tot_cap` 减了全局。
于是同一品种可以连续加仓，每层都按“本笔 ≤2%”放行：

- 4 层、每层顶满 2% → 单品种 2N 风险约 **8%**；
- 组合总风险上限 10% → **一个品种就能吃掉 8%**，剩下 2% 给其他 5 个持仓；
- 这与“单品种风险预算 2%”的直觉/命名不一致。

两种修法，必须先明确业务意图：

- **方案 A（推荐，若 2% 是品种总预算）**：
  `mkt_cap = (equity*share*max_risk_per_mkt - self._book[code]["risk"]) // rpl`；
  这样单品种最多约 0.75 个名义单位，金字塔基本被关闭。若仍要 4 层，
  需把 `max_risk_per_mkt` 提高到约 8%，并把 `max_total_risk_frac` 相应提高。
- **方案 B（若 2% 确实是单笔）**：改名为 `max_risk_per_order`，
  新增 `max_risk_per_market`（默认 8%）并做累计检查；
  README/配置注释同步，避免“单品种 2%”的误读。

无论哪种，都应加一个“4 层满仓”的风险回放测试，断言单品种最大风险不超过配置值。

#### 3.2.3 资本基数三套并存

| 位置 | 数值 | 用途 |
|---|---:|---|
| `runBT.py --capital` | 5,000,000 | 回测报告/`WtBtAnalyst` 分母 |
| `config/turtle_config.yaml risk.fallback_capital` | 15,000,000 | `RiskManager.equity()` 基数 |
| `config.yaml riskmon.base_amount` | 5,000,000 | C++ 组合风控基数 |
| 生产账户（仓库记录，2026-09-15） | balance 23.36M / available 4.37M / margin 18.29M | 共享账户真实值 |

后果：

- 2026-10-08 之后用当前配置重跑，手数是 09-16 矩阵的约 3 倍；
  旧结果不可复现，新旧结果不可比；
- 回测 summary 的 `capital=5,000,000` 与 `RiskManager` 的 15M 基数不一致，
  收益/回撤的分母与仓位的风险基数不是同一个账户；
- 12 个策略各自按 15M 权益计算，合计 180M 风险资本，
  而账户只有 23.36M；保证金上限 12×1.5M=18M 恰好接近账户 18.29M 保证金，
  属于“满杠杆”运行，可用资金只剩 4.37M；
- `account_gate_mode=warn` 只告警不减仓，9/9 “资金耗尽、p 追单 3357 次被拒”
  的防线并未真正生效。

建议：

1. 定义**唯一总资金** `portfolio_capital`（例如账户权益 23.36M 的一部分，
   扣除其他策略占用后的分配额）；
2. 按品种/策略分配 `allocated_capital`，`RiskManager` 用它而不是全局 15M；
3. `runBT/run_matrix` 的 `--capital` 必须与 `fallback_capital` 一致，
   或由 runner 显式覆盖 `cfg.risk.fallback_capital = args.capital`；
4. `riskmon.base_amount` 与总资金一致，且 `risk_scale<1` 才有意义；
5. `account_gate_mode` 逐步从 `warn` 升到 `veto`，并做**跨策略预留**
   （同一 tick 12 个策略不能都按同一份 available 计算）。

#### 3.2.4 日损/账户级熔断实际是 no-op

`config.yaml`：

```yaml
riskmon:
  base_amount: 5000000
  inner_day_active: true
  inner_day_fd: 20.0
  multi_day_active: false
  risk_scale: 1.0
```

C++ `WtSimpRiskMon` 在触发时执行 `_ctx->setVolScale(_risk_scale)`
（`WtSimpRiskMon.cpp:97`）。`risk_scale=1.0` 表示“降到 100%”，
即**不改变任何仓位**；`multi_day_active=false` 又关闭了多日回撤熔断。
所以 README 写的“C++ SimpleRiskMon（账户级回撤熔断）”目前只是日志，
不是防线。策略层的 `RiskManager` 也没有“当日亏损 X% 后禁止新开仓/减仓”的逻辑。

建议（按优先级）：

1. 先设 `risk_scale: 0.5`（或 0.3），让日内回撤触发时真正减仓；
2. 把 `base_amount` 改成组合分配资金；
3. 在策略层加 `daily_loss_limit`（例如 -3% 停止新开仓，-5% 全平/只减不加），
   因为 C++ riskmon 是**进程级/组合级**，无法区分策略；
4. 对 12 个品种做“相关性冲击”压力测试：金属/化工同时 2N 止损、
   涨跌停无法成交、隔夜跳空 1.5N 等场景，检查最大回撤/保证金。

#### 3.2.5 `max_units_*` 统计的是“品种数”，不是“单位数”

`RiskManager.units_in_group` 只数 `_book` 里的 code 数量（`:222-225`），
`max_units_total` 也是 `len(self._book)`。生产 `units=1` 时等价；
一旦 `units=2`、`S1+S2` 或多速度，一个品种实际占 2 个单位，
限制仍然是 1，风控会被绕过。建议在 book 里记录 `units`（或 `risk_units`），
组/总限制按单位数累计；这也是经典海龟“4 units/market、6/group、12/total”的正确口径。

#### 3.2.6 双系统 `share` 与保证金扣减

`cap_entry` 对保证金预算按 `share` 缩放，同时扣减 `cur_margin * share`
（`:109-114`）。两个子系统各 50% 时，先入场者占用 50% 预算，
后入场者看到“自己的预算 50% − 全部自有保证金×50%”，会被过度压缩。
当前双系统休眠，影响有限；启用前建议改成“组合级保证金预算 + 按子系统顺序扣减”，
并加一个双系统同时入场的测试。

### 3.3 过滤链

- 方向闸已从硬编码迁到 `DirectionGateFilter`，行为保持；
- `AdxTrend` / `VolatilityBand` / `LiquidityFloor` 实现完整，测试覆盖；
- **生产配置零启用**，设计初衷（可配置辅助业务逻辑）没有落地验证；
- `_run_filter_chain` 捕获异常后 `continue`（放行）（`StraTurtle.py:1622-1625`）。
  对辅助过滤器可以接受，但如果未来把“风险类过滤器”放进来，异常=放行是危险的。
  建议给 `SignalFilterBase` 增加 `on_error: allow|deny`，默认 deny 或至少计数告警；
- `_session_eval_daily` 调用 `_eval_core(..., with_oi=False)`（`:1367-1369`），
  于是 `volumes/open_interests=None`，`LiquidityFloor` 在该路径走
  `on_warmup`（默认 allow）。而 `_on_calc_daily`/`_catchup_eval` 用 `with_oi=True`。
  同一品种在“收盘合成日 K”与“盘中/补算”两条路径上的流动性过滤结果可能相反。
  建议：his bars 本身有 volume/OI，`with_oi=True` 即可；或者显式记录
  “本路径不启用流动性过滤”并让配置校验拒绝这种组合。

### 3.4 执行与成交确认

- 执行单元 `WtMinImpactExeUnit`：`span=500s`、`lots=1`、`expire=5`。
  大单（rb 数百手）在 500s 内可能只成交一部分，`fill_timeout=600s` 很容易触发；
  10-09 的 pp 116 手零派发、IF 加仓零派发是已知事故；
- `stra_set_position` 是目标语义，框架内部净额化；策略侧 `confirm_*` 立即按
  决策价记账，真实成交靠 `_reanchor_actual_fill` 回写。这个设计是对的，
  但成交归属仍靠“同合约前缀 + 确认顺序”，没有订单号/策略标签；
- 共享账户下，`_fill_confirmed` 的“增量 == 本层手数”只在**没有其他策略
  同时交易同一品种**时成立；`trades.csv` 也没有策略标签。若 futu_mm 等
  策略交易同一品种，增量/逐笔都会被污染；
- `_periodic_truth_check` 在共享账户下按**账户绝对持仓**对比本策略框架仓，
  只要其他策略持有同一品种，就会持续 `truth_mismatch` 告警；
  `truth_rebuild_mode=alert` 只阻止重建，不阻止告警刷屏；
- 建议：
  1. 每笔订单带唯一 `userTag`（策略名+序号），成交/持仓按 tag 归因；
  2. 执行单元增加订单状态机（未创建/已报/部分成交/成交/撤单/拒绝），
     状态落盘，重启恢复；
  3. 增加参与率上限（如 ≤5% 分钟成交量）和最大滑点保护；
  4. 能下条件单的品种优先用 broker-side stop，而不是靠 tick 触发；
  5. 共享账户的 `truth_mismatch` 改为“本策略订单窗口内增量”告警，
     不要用账户绝对持仓。

### 3.5 真值源与回测隔离（P0）

`_truth_source` 没有 `is_backtest` 判断（`StraTurtle.py:155-180`），
`on_init` 无条件探测（`:656`）。回测时：

- `_fill_confirmed` 直接读 `_truth_net`（`:1995`），用**实盘账户增量**确认回测成交；
- 确认失败 600s 后会触发 `ghost_fill` → `_maybe_ghost_trim`（`:2047`），
  按**实盘账户持仓**裁剪回测引擎的层；
- `alert` 模式只影响 `_truth_position`（`:215-218`），不影响上述两条路径；
- 陈旧闸只挡住 `age > truth_max_age`，而生产配置是 86400s；
  一份 1 小时前的实盘真值在回测里是“新鲜”的。

2026-09-15 的事故是“陈旧真值把 cu 基线从 335% 拉到 76%”；
**新鲜真值的破坏力更大**，而且更难发现。修复很简单：

```python
def on_init(self, context):
    if getattr(context, "is_backtest", False):
        self._truth = None
        self._truth_probed = True      # 不再重探
```

并在 `runBT/run_matrix` 里显式传 `truth_json="off"`；
加一个测试：回测 context + 新鲜 rtdata.json，断言 `_fill_pending` 不使用真值、
`_maybe_ghost_trim` 不被调用。

### 3.6 研究报告的可复现性

- `run_matrix` 每个品种独立进程，`RiskManager` 私有，组合约束不生效（见 2.2）；
- `run_matrix` 的 `--capital` 只影响 `WtBtAnalyst` 与组合分母，不影响手数；
- `run_walkforward` 用 `summary.json` 是否存在判断成功，不检查 returncode/mtime，
  失败窗口会拿上一次的旧结果（早前工程向审阅已列 P0，10-21 已修）；
- `run_speed_study` 的 `pnl[i]` 只按 **1 个单位**计算：
  `dir_before * Δclose / n * UNIT_VAR`（`:101-102`），
  虽然引擎执行了 `confirm_add`，但 PnL 没有乘层数。
  所以 `single_20/10 Sharpe 0.632` 不是“4 层金字塔”的策略收益，
  不能直接和线上/矩阵比较。修复：按 `sum(lots)` 计算当日 PnL，
  或明确标注“单单位、无金字塔”的研究口径；
- 建议所有 research summary 记录：
  `config_sha256`、`data_first/last`、`entry_mode`、`stop_n_mode`、
  `fallback_capital`、`capital`、`slippage`、`fees_version`，
  并在 HTML/CSV 报告头部打印；否则一张表无法回溯。

### 3.7 其他策略逻辑缺口

| 缺口 | 影响 | 建议 |
|---|---|---|
| h1 周期 | `schemas` 接受，`CsvDataLoader` 对未知 period 静默返回 False，`DataChecker` 路径退化 | 在 `ConfigLoader` 直接拒绝 h1，或补齐数据层与 trigger/session 语义 |
| 涨跌停 | 只检测“价格钉住 + fill_pending”，入场不受限 | 增加“当日锁死禁止新开/加仓”的显式状态；出场用可成交价模拟 |
| 换月 | HOT 自动换月 + avgpx 平移；无换月日/价差保护 | 记录换月日、价差、持仓变化；换月窗口加过滤器/人工确认 |
| 数据陈旧 | `check_staleness` 只 warn；策略仍用旧窗口 | 策略层加 `max_data_lag`，超过则拒绝武装/入场并告警 |
| catch-up 补票 | 停机后首 tick 可能按远离武装位的价格入场 | 加“最大追价距离”（如 >1N 放弃/等回踩） |
| 外部收养 | `adopt_position` 不写 `s1_last` | 收养时建立 `taken/open` 记录，退出时恢复 S1 过滤语义 |
| 订单状态 | 无持久化 pending/order 状态 | 执行层状态机 + 重启恢复；当前只靠真值/对账兜底 |
| 流动性 | `LiquidityFloor` 只看均量，不限制订单占 ADV | 在 `PositionSizer` 加 `max_lots <= k × ADV`，按品种配置 |

---

## 4. 代码级问题与优化

### 4.1 具体缺陷（可复现/可定位）

| # | 级别 | 位置 | 问题 | 修复 |
|---|---|---|---|---|
| C1 | P0 | `StraTurtle.py:155-180, 656, 1995, 2047` | 真值源无回测隔离，回测可被实盘 `rtdata.json` 影响成交确认/ghost trim | `is_backtest` 时禁用真值；测试覆盖 |
| C2 | P0 | `runBT.py:58` + `StraTurtle.py:100` | 每个回测品种私有 RiskManager，组合约束不生效 | 组合回测或共享 book；见路线图 |
| C3 | P0 | `config.yaml:23,30` | `riskmon.risk_scale=1.0`，日内回撤触发后不降仓 | 设 0.3~0.5，或删除该层并在策略层实现日损闸 |
| C4 | P0 | `RiskManager.py:100` | `max_risk_per_mkt` 不扣该品种存量风险，4 层可达 ~8% | 减 `book[code]["risk"]`，或改名+新增累计预算 |
| C5 | P1 | `StraTurtle.py:1195-1208` | `_last_n` 非有限时丢弃触发决策，但引擎 `_pending` 已置位且未清 | 丢弃前 `sub["engine"].clear_pending()` 或 `reject_pending()`；加测试 |
| C6 | P1 | `TurtleEngine.py:67` + `StraTurtle.py:71-86` | 多速度 `name` 直接作为 `system` 传给引擎，`fast/slow` 触发 AssertionError | `name` 与 `system` 解耦；`ConfigLoader` 校验或映射 |
| C7 | P1 | `StraTurtle.py:817, 906` | 日评估先 `_reanchor_on_roll`（全层平移）再 `_reanchor_actual_fill`（末层精确），顺序与注释相反；`_fill_pending` 未清时存在“末层误差被摊到所有层”的风险（该组合未被测试覆盖） | 先回写末层再 roll；或 `_fill_pending` 非空时跳过 roll；加双测 |
| C8 | P1 | `StraTurtle.py:1367-1369` | session_end 合成日 K 用 `with_oi=False`，LiquidityFloor 在该路径 fail-open | his bars 传 `with_oi=True`，或显式禁止该组合 |
| C9 | P1 | `StraTurtle.py:1622-1625` | 过滤器异常一律放行 | `on_error` 配置；风险类过滤器默认 deny |
| C10 | P1 | `runBT.py:20` | `_precheck` 永远读默认 `./config/turtle_config.yaml`，忽略 `--config` | 传入 `args.config` |
| C11 | P1 | `runBT.py:54-56` | `bt_no_simtick` 只看 `storage/csv/<code>_m5.csv`，不认 DSB | 用 `DataChecker`/`storage/his/min5` 判断 |
| C12 | P2 | `TurtleEngine.py:67-69` | 配置校验用 `assert`，`python -O` 下失效 | 改 `ValueError`；配置层已校验，但直测/脚本会绕过 |
| C13 | P2 | `TurtleEngine.py:552-580, 845-901` | `_advance_sim` / `_advance_sim_price` / `_advance_sim_bar` 三份镜像逻辑 | 抽 `_touch_exit`/统一推进函数，参数化 close/tick/bar |
| C14 | P2 | `TurtleEngine.py:678-779` | `on_price`/`check_bar` 长短仓镜像各约 70 行 | 抽 `_check_levels(px, is_bar)`，保留跳空/触碰差异参数 |
| C15 | P2 | `StraTurtle.py:923-927, 1226-1232` | 快照结构两处重复组装 | `_snapshot_payload()` |
| C16 | P2 | `StraTurtle.py:1925-1941` | 否决日志用 `time.time()`，NTP 回拨会抑制日志 | 冷却用 `time.monotonic()`，时间戳仍用 `time.time()` |
| C17 | P2 | `_alert` / `fund_live.csv` | 无轮转，长期增长 | logrotate / 按月分文件 / 上限 |
| C18 | P2 | `run_speed_study.py:101-102` | PnL 未乘层数，研究结论与线上不可比 | 按 `sum(lots)` 计算或明确单单位口径 |
| C19 | P2 | `run_matrix.py:127-141` | `collect` 不校验 summary 新鲜度/失败 | 记录子进程开始时间，mtime 早于开始则报错 |
| C20 | P2 | `README.md:215,289` | 测试数 383 vs 实际 386；性能表未绑定配置哈希 | 更新数字；报告加 config/data hash |

### 4.2 复杂度热点

1. **StraTurtle 拆分**（收益最大，见 A1）；
2. **执行/风控门控统一**（A2）：单/双系统两套 `_size_and_cap`/`_apply_*`；
3. **引擎触发分支合并**（C13/C14）：预计净减 120+ 行；
4. **模式开关收敛**（A3）：`close/trigger`、`bt_no_simtick`、`intrabar`、`truth`
   四组开关组合爆炸，建议用 `ExecutionModel` 接口；
5. **异常处理审计**：`except Exception` 在 `_truth_source`、`_trade_journal_source`、
   `_alert`、`_dump_fund_live`、`_run_filter_chain`、`_maybe_exec_recover` 等
   多处静默；至少加 debug 日志/计数器，避免“功能悄悄失效”。

### 4.3 DRY 清单

| 重复项 | 份数 | 位置 |
|---|---|---|
| 周期后缀/目录映射 | 7 | `BarJournal:20` / `CsvDataLoader:21` / `DataChecker:16` / `deploy_tpl/run.py:56` / `prepare_data:41` / `fetch_data_tqsdk:156` / `convert_csv_to_dsb:162` |
| 日期/时间解析 | 4 | `convert_csv_to_dsb:21` / `CsvDataLoader:27` / `BarJournal:105-132` / `DataChecker:65` |
| 默认回测区间 | 5 | `runBT:44` / `run_matrix:193` / `run_slippage_sens:51` / `run_walkforward:159` / `configbt.yaml` |
| kill 进程实现 | 2 | `update_hots:90` / `update_hot_daily:246` |
| HTTP 请求 | 2 | `pick_hot_sina:121` / `notifier:19` |

建议：`helpers/periods.py`（周期映射/解析）、`scripts/_proc.py`（进程重启）、
`helpers/timeutil.py`（交易日/时间键）；默认区间读配置。

### 4.4 测试体系

现状：34 个测试文件、386 例全绿（本次复跑 20.6s）。亮点：幽灵 trim 13 例、
证据链 8+、catchup 闸 4、棘轮 N 22、保证金方案 B 事故回放、重放幂等 3、
过滤器对拍、配置正反分支。CI 有 py3.9-3.11 + ruff + cov≥70%。

缺口（按风险）：

| 级别 | 缺口 |
|---|---|
| P0 | `StraTurtle.from_config` 零测试（生产装配唯一入口） |
| P0 | 组合约束回测零测试（max_units/group/total risk 的组合行为） |
| P0 | 回测真值隔离零测试（新鲜 rtdata.json 污染） |
| P1 | `on_backtest_end`、`_dump_fund_live`、`deploy_tpl/run.py` 启动编排 |
| P1 | `_reanchor_on_roll` + `_fill_pending` 叠加顺序 |
| P1 | 多速度（speeds）装配/份额/名称 |
| P1 | `_session_eval_daily` 的 LiquidityFloor 路径 |
| P2 | `_first_active_sub` / `_avgpx_layer_price` 降级路径 |
| P2 | 测试卫生：`_dump_fund_live` 写入源码树；56 份 `stra_*` mock 复制 |

建议：
- 用 `conftest.py` 提供 `make_ctx(**overrides)` 工厂，收敛 mock；
- 加属性测试（hypothesis）：持仓/层数/止损单调性、snapshot-restore 幂等、
  `trim_to_lots` 只减不增；
- 加一个“黄金回测”：固定配置/数据/滑点，断言 rb 的 summary 哈希；
  配置或成交口径一变就失败，强制更新基线。

---

## 5. 优化提升路线图

### 阶段 0（P0，1~2 周）：让三个口径先对账

| # | 动作 | 验收 |
|---|---|---|
| 0.1 | 定义 `portfolio_capital` / `allocated_capital`，统一 `runBT --capital`、`risk.fallback_capital`、`riskmon.base_amount` | 三个数字一致；配置加载打印总资金分配表 |
| 0.2 | `ConfigLoader` 加交叉校验：`units*unit_var*stop_mult <= allocated*max_risk_per_mkt`；`fallback_capital>0`；`risk_scale<1` 才允许 `inner_day_active` | 当前配置会报错/给出明确 WARN 与实际比例 |
| 0.3 | `max_risk_per_mkt` 改为按品种累计（或改名并新增累计项） | 4 层满仓回放 ≤ 配置上限；单测 |
| 0.4 | 回测禁用真值；`runBT/run_matrix` 传 `truth_json="off"` | 新鲜 rtdata 存在时回测结果不变；测试 |
| 0.5 | 启用 notify（webhook），`timer_notify` 失败落 alerts.log 并非零退出 | 人为触发 ghost_fill，手机/群收到 |
| 0.6 | `riskmon` 设 `risk_scale=0.5`（或删除并在策略层实现日损） | 模拟日内回撤，日志显示仓位降至 50% |
| 0.7 | 修 C5（孤儿 pending）、C6（多速度断言）、C10/C11（runBT 预检/m5） | 新增 4 个回归测试 |

### 阶段 1（P1，2~4 周）：组合级回测 + 执行闭环

| # | 动作 | 验收 |
|---|---|---|
| 1.1 | 组合约束回测：共享 `RiskManager`/PortfolioBook，按日推进 12 品种，执行 max_units/group/total risk/margin/available | 最大同时持仓 ≤6、组 ≤2；输出“约束前/后”两条曲线 |
| 1.2 | 矩阵报告加 config/data hash、capital、entry_mode、stop_n_mode、slippage | 任一张表可回溯到唯一配置 |
| 1.3 | 重跑 12 品种矩阵（约束前/后），更新 README/AGENTS | 结果可复现；删除/标注旧数字 |
| 1.4 | 执行层订单状态机 + `userTag` 归因 + 参与率/滑点上限 + 状态持久化 | 10-09 类零派发可观测/可恢复；共享账户不再靠前缀猜 |
| 1.5 | 日损闸：-3% 停新开、-5% 只减不加；组合级可用资金 veto | 模拟触发；与 riskmon 不重复 |
| 1.6 | 过滤器 A/B：一次启用一个（ADX/VolBand/Liquidity），walk-forward + 成本 | OOS 改善才启用；否则保持 off 并记录 |
| 1.7 | StraTurtle 拆分（A1 蓝图，分 4 步） | 每步 386+ 测试全绿；单文件 <800 行 |
| 1.8 | 单/双系统门控合并（A2） | 行为对拍；日志 key 不变 |

### 阶段 2（P2，持续）：策略完备性与工程卫生

| # | 动作 |
|---|---|
| 2.1 | 收盘模式跳空止损、同 bar 反转配置化；与 trigger 做 A/B |
| 2.2 | 无 m5 日 bar 多次加仓仿真；或明确“日 bar 一层”口径 |
| 2.3 | `max_units` 按单位数统计；双系统 `share` 保证金口径修正 |
| 2.4 | 换月窗口保护、catch-up 最大追价、数据陈旧硬闸 |
| 2.5 | h1 支持或 ConfigLoader 直接拒绝 |
| 2.6 | 引擎三份 sim 推进/长短仓镜像合并；`_snapshot_payload` 去重 |
| 2.7 | 测试 mock 工厂、fund_live 隔离、黄金回测、属性测试 |
| 2.8 | 删除陈旧 `deploy/`；make_deploy 原子化；runMonSvr 纳入部署链；systemd Persistent/OnFailure |
| 2.9 | 日志轮转、alerts.log 归档、stradata 备份清理 |
| 2.10 | README/GUIDE/OPS 数值与配置口径同步；风险表加“实际可达”列 |

---

## 6. 建议的验收指标（KPI）

### 6.1 研究口径

- 任一回测报告可由 `(config_sha256, data_first, data_last, capital,
  entry_mode, stop_n_mode, slippage)` 唯一复现；
- 组合回测同时持仓 ≤ `max_units_total`，组 ≤ `max_units_per_group`；
- 单品种累计 2N 风险 ≤ `max_risk_per_market`；
- 无未来函数：信号只用 `<= t` 的数据；成交价不优于可成交价；
- 成本：至少 1 tick 滑点 + 手续费；报告含 0/1/2 tick 敏感性。

### 6.2 实盘风控

- 单品种累计风险 ≤ 配置上限；组合 ≤ `max_total_risk_frac`；
- 账户保证金/可用资金有硬闸（veto），跨策略预留；
- 日损 -3% 停新开、-5% 只减不加；
- 任一时刻 `engine target == 券商真实持仓`（或差额有告警/自愈记录）；
- `pending` 不跨 tick、不跨日；孤儿 pending 告警为 0；
- 成交确认：真值增量/逐笔成交/订单号三选一，不依赖 fees 单独确认。

### 6.3 执行质量

- 订单派发成功率 >99%；零派发/卡死 30 分钟内告警；
- 大单分批完成率 >95%，`fill_timeout` 比例 <1%；
- 实际成交均价与决策价偏差的分布（按品种）可观测；
- 滑点超过阈值自动撤单/重报，不无限追单。

### 6.4 运维

- notify 通道每周至少一次心跳；
- 数据时效：日线滞后 >1 交易日告警，>3 交易日禁止新开仓；
- tick 静默：session 内 30 分钟无 tick 告警；
- 崩溃有 core/faulthandler；重启后 0 重复单、0 状态丢失；
- 部署 manifest 校验通过；无陈旧 `deploy/` 副本。

---

## 7. 附录：本次核对的证据

### 7.1 测试

```text
python3 -m pytest tests/ -q
386 passed in 20.62s
```

### 7.2 关键代码位置

| 主题 | 位置 |
|---|---|
| 风控权益/单笔/总敞口/保证金 | `helpers/RiskManager.py:37-156` |
| 单笔风险不累计 | `helpers/RiskManager.py:100-104` |
| 单位限制按品种数 | `helpers/RiskManager.py:218-225` |
| 引擎 N 口径/棘轮 | `helpers/TurtleEngine.py:21-36, 429-531` |
| 触发/触碰 | `helpers/TurtleEngine.py:678-901` |
| 真值源 | `StraTurtle.py:155-298` |
| 评估骨架 | `StraTurtle.py:797-832` |
| 单/双系统门控 | `StraTurtle.py:1458-1514, 1802-1876` |
| 成交回写 | `StraTurtle.py:1965-2283` |
| 幽灵 trim | `StraTurtle.py:2047-2112` |
| 回测 runner | `runBT.py:20,46,54-60` |
| 矩阵合成 | `run_matrix.py:76-117,127-141,195` |
| 速度研究 PnL | `run_speed_study.py:85-102` |
| 风控参数 | `config/turtle_config.yaml:6,59-65`; `config.yaml:17-30` |
| C++ riskmon | `wondertrader/src/WtRiskMonFact/WtSimpRiskMon.cpp:30-125` |

### 7.3 本次回放的数值

- 单位风险：`unit_var=200,000`，`stop_atr_mult=2` → 2N/单位=400,000；
  `fallback_capital=15,000,000`，`max_risk_per_mkt=2%` → 单笔上限 300,000；
  比例 0.75。
- 2026-09-01 数据：最终手数占名义 61.5%~75.1%（见 3.2.1 表）。
- 持仓重建（2019-01~2026-08，1859 交易日）：平均同时持仓 6.55，
  最大 12，>6 占 50.5%，化工组 >2 占 42.4%。
- 矩阵结果（09-16 产物）：总收益 47.42%，年化 5.14%，最大回撤 30.13%，
  Sharpe 0.27；贡献前三 cu +51.6%、CF +49.3%、al +27.9%，
  TA/MA/pp 分别 -13.5%/-13.8%/-11.7%。
- 生产快照（仓库记录，2026-09-15）：balance 23.36M，available 4.37M，
  margin 18.29M；6 个持仓（rb/CF/MA/pp/p/IF），恰好等于 `max_units_total=6`。

### 7.4 局限

- 生产账户数字来自仓库内 AGENTS/审计记录，未在本机实时核验；
- 组合持仓重建基于 `trades.csv` 的 OPEN/CLOSE 净额，未考虑盘中多次
  开平与部分成交，属于近似；
- 未运行 12 品种全量回测（数据与时间成本），矩阵数值引用 09-16 产物；
- C++ 执行单元/风控的运行时行为以源码与既有事故记录为准，
  未在本机重新编译验证。

---

## 8. 一句话总结

**信号引擎是可靠的，问题在“账户/组合/研究”这三层口径没有对齐：
先把资金基数、组合约束、真值隔离三件事做成硬约束，再谈参数优化和过滤器；
否则回测越漂亮，实盘越容易在资金和相关性上翻车。**

---

## 9. 实施进展（2026-10-20 第一批）

> 本节记录报告落地情况；上面 §0-§8 保留为复核时的原始发现（历史快照）。
> 测试：**386 → 421 passed**，`ruff check . --config ruff_cta_turtle.toml` 通过。

### 9.1 已完成

| 原发现 | 状态 | 落地内容 |
|---|---|---|
| 资本基数 5M/15M/5M 三套 | ✅ 回测侧 | `runBT/run_matrix --capital` 覆盖 `risk.fallback_capital`；`from_config(capital=)`；`ConfigLoader` 对单位风险 vs 单笔上限 WARN |
| 单品种风险只按“本笔” | ✅ | 新增 `risk.max_risk_per_market=0.08`；`cap_entry` 扣该 code 已占风险 |
| 真值源污染回测 | ✅ | `on_init` 检测 `is_backtest` 后禁用 rtdata/trades；新增回归测试 |
| riskmon 空转 | ✅ 配置 | `base_amount` 5M→15M；`risk_scale` 1.0→0.5（触发时真正减半） |
| 无策略层日损闸 | ✅ 机制 | `risk.daily_loss_stop_frac` / `daily_loss_flatten_frac`（默认 0=关闭；开启前需先定单策略分配资金） |
| N 缺失丢弃决策留孤儿 pending | ✅ | `_exec_trigger_decs` 丢弃前 `clear_pending()`；新增测试 |
| 多速度 `name` 触发断言 | ✅ | `SpeedConfig.system` 与 name 解耦；单速度通道 key 用子名；新增 `test_speeds.py` |
| session_end 流动性过滤器 fail-open | ✅ | `_session_eval_daily` 改 `with_oi=True`；新增测试 |
| 过滤器异常一律放行 | ✅ | `SignalFilterBase.on_error=allow/deny`；策略层按声明处理 |
| 日评估 roll/fill 顺序 | ✅ | `_reanchor_actual_fill` 先于 `_reanchor_on_roll`；新增双测 |
| runBT 预检读错配置 / m5 只认 CSV | ✅ | `_precheck(code, args.config)`；`DataChecker.has_bars` 认 CSV+DSB |
| 研究脚本失败仍收旧产出 | ✅ | `run_matrix` 失败即 SystemExit；walkforward/slippage 先删 summary+rc 检查；runBT 写 fallback summary |
| speed_study PnL 未乘层数 | ✅ | 按决策前 `lots_before` 计算；研究结论需重跑 |
| 组合约束未在回测执行 | ✅ v1 | 新增 `run_portfolio_bt.py` 共享账本逐日回放（close 模式）；trigger/m5 组合回测仍待下一阶段 |
| `max_units_*` 按品种数而非单位数 | ✅ | book 记录 `units`，`open_units`/`units_in_group` 按单位累计；新增 2 例测试 |
| `notify.enabled=false` | ⬜ | 需用户提供 webhook URL |
| 陈旧 `deploy/` 快照 | ⬜ | 建议删除或由 make_deploy 生成到 build/ |
| h1 实际不可用 | ⬜ | 建议 ConfigLoader 直接拒绝或补数据层 |
| 执行层订单状态机/归因 | ⬜ | 下一阶段（MiniImpact/订单号/userTag） |

### 9.2 组合约束回放的结论

用 `outputs_bt` 的 12 品种交易流回放（2019-01~2026-08，1859 交易日）：

- 若严格执行 `max_units_total=6`：728 个持仓 episode 中 **222 个被拒**
  （177 个因总单位、45 个因组上限）；
- 化工组 42.4% 的交易日会超过 2 个单位；全组合 50.5% 的交易日超过 6 个；
- 约束回放的 PnL 是**事后筛选**结果：被拒 episode 里若有亏损，组合曲线
  反而可能高于无约束曲线（本样本即如此），所以**不能当作“约束能提升收益”的
  证据**；它只用于量化“旧矩阵有多少交易是实盘做不了的”。

真正可交易的组合回测需要共享 `RiskManager`/PortfolioBook 逐日推进 12 品种，
并让被拒信号影响后续武装/加仓 —— 已由 §9.5 的 `run_portfolio_bt.py` v1
覆盖（close 模式；trigger/m5 版本仍待下一阶段）。

### 9.3 复现命令

```bash
cd demos/cta_turtle
python3 -m pytest tests/ -q
ruff check . --config ruff_cta_turtle.toml

# 回测资金口径 (--capital 同时作为风控基数)
python3 runBT.py --code SHFE.rb.HOT --start 202301010900 \
    --end 202312291500 --capital 5000000 --slippage 1

# 共享账本组合回测 (真约束, close 模式)
python3 run_portfolio_bt.py --config config/turtle_config.yaml \
    --capital 60000000 --start 20190101 --end 20260831 \
    --slippage 1 --priority strength
cat matrix_results/portfolio_bt.json

# 组合约束暴露 (基于现有 outputs_bt 的事后诊断)
python3 run_portfolio.py --config config/turtle_config.yaml --bt-dir outputs_bt
```

### 9.5 共享账本组合回测 v1（2026-10-20 新增）

`run_portfolio_bt.py`：12 品种共用一个 `RiskManager`/账本，逐日推进；
每个入场/加仓前统一过单品种、累计、总敞口、保证金、单位上限。
与 `run_portfolio.py` 的区别：后者是“已生成交易流的事后筛选”，
本工具会让被拒信号**不再产生持仓/后续加仓**，是真正的组合约束回测。

口径：日线 close 模式、1 tick 滑点、`common/fees.json` 手续费、
6000 万总资金（= 12×500 万，与旧矩阵口径对齐）、2019-01~2026-08。
名额满时默认按配置顺序；`--priority strength` 按突破距离/N 降序。

| 组合 | 总收益 | 年化 | 最大回撤 | Sharpe | Calmar | 交易数 | 拒绝数 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 约束后（6 单位/组 2/风险 10%/保证金 60%） | **+77.1%** | 7.66% | **29.2%** | **0.414** | 0.263 | 1927 | 382 |
| 无组合约束（相同单笔风险） | +97.4% | 9.17% | 38.2% | 0.446 | 0.240 | 2476 | 0 |

（`--priority strength`；config 顺序为 +61.9%/30.6%/0.369，说明
“名额给谁”对结果影响很大。旧矩阵 +47.4%/30.1%/0.273 是无约束上界，
且口径不同，不能直接对比。）

同时用 1500 万总资金跑同一配置：约束后 **-36.2% / 回撤 82.3% /
254 天保证金超限（峰值 19.1M）** —— 12 品种满配需要约 20M 以上保证金；
这也说明“15M 策略侧 fallback”不是组合可用的真实资金，资金分配必须先定。

### 9.6 本批未解决 / 已知限制

- 本地环境的 `WtBtAnalyst` 在当前 pandas 版本下会在 Excel 统计段抛
  `Length mismatch`；本次已给 `runBT` 加**最小 summary 兜底**
  （从 funds.csv 写 `fallback=True`、`win_rate=0`），回测本身与
  run_matrix/walkforward 收集不再中断；Excel 报表仍需兼容的 pandas 版本。
- `run_portfolio_bt.py` v1 只做 close 模式日线回测；trigger 模式/m5 撮合、
  盘中止损、真实成交排队不在本版范围。
- `run_portfolio.py` 仍是“事后筛选”诊断，不能替代 `run_portfolio_bt.py`。
- 日损闸默认关闭：15M 基数下 3% = 45 万/策略，12 个策略同时触发会远超
  共享账户承受力；应先落“单策略分配资金”再开阈值。
- `notify` 仍未启用；`deploy/` 陈旧副本仍在仓库里；h1 仍不可用。

## 10. 实施进展（2026-10-21 第二/三批 + 生产部署）

> 测试：**449 passed (44 文件)**，ruff 通过。生产已于 2026-10-21 15:33 全量
> 部署至 129.211.5.54（10-09 版 → 最新，备份 wt_turtle_backup_20261021_1538.tgz）。

### 10.1 自 §9 以来新完成

| 条目 | 状态 | 说明 |
|---|---|---|
| §0.2-2 组合级回测 trigger 模式 | ✅ | `run_portfolio_bt --entry-mode trigger`（m5 check_bar 触碰仿真/平今费率/first-come 名额），60M 全窗口 +85.97%/DD38.77%/0.448 |
| §3.4 执行层订单状态机/归因 | 🚧 实验 | CTA 层源码级确认无订单回调；`core/ExecJournal.py`（OrderTracker/ExecAttributor，7 例单测）+ `scripts/exec_journal.py` 旁路侧车（订阅 TRD_ORDER/TRADE 总线）。**userTag/参与率上限/状态持久化未做**（需 C++ 执行单元改造） |
| §3.7 h1 | ✅ 超预期 | 未采纳"直接拒绝"，一期实现策略层 m5→h1 聚合（core/aggregate.py），backtest/live 同源；rb 实证 6 个月可交易 |
| A4 陈旧 deploy/ 快照 | ✅ | 已删除；make_deploy 一键重建；本次生产部署后本地重新生成 |
| C18 后续 speed_study 重跑 | ✅ | 含层数口径重新生成 + meta.caliber 防跨版本误比 |
| P2 小项×5（临时目录泄漏/平今费率/slippage fallback 列/speed_study 口径/评估区间 WARN） | ✅ | 见 AGENTS.md 2026-10-21 批次 |
| §3.2.4 riskmon 降仓 | ✅ 生产生效 | 今日部署 + config.yaml 补丁（15M/0.5），启动日志实测 "Risk Scale: 0.50" |
| 目录治理 | ✅ | helpers/→core/、research/ 归拢（报告 A 系列架构建议未动，见 §10.2） |

### 10.2 报告建议的合理性评估（复核结论）

- **方案 B（保留"每笔 2%"+ 新增累计 8%）优于方案 A**：生产实证支持——rb 当前 408 手空（2 层含历史缩量层）、p/CF/IF 满 4 层，若按方案 A"单品种 0.75 个名义单位"金字塔基本关闭，与海龟核心逻辑冲突；方案 B 在"单笔风险可控"与"金字塔可运行"间取得平衡。✅ 已按 B 落地。
- **h1"直接拒绝"的建议被"策略层聚合"替代，方向正确**：WtDt 不写 h1 且 reader 缓存无失效 API（9/21 事故教训），数据底盘改造不可行；m5→h1 策略层合成是唯一 backtest/live 同源的路径。成本是 O(窗口²) 聚合（全窗口约 10 分钟，可增量优化）。
- **A1/A3（God class 拆分 / ExecutionModel 接口）方向正确但时机偏早**：生产刚部署大版本，拆分应等日损闸/资金分配（0.1/1.5）落定后再动，否则变更爆炸半径恰是报告自己警告的风险。建议维持"接口边界即测试边界"逐步迁。
- **§3.1-3"当根包络是次优入场"的 A/B 建议仍值得做**（channel_only vs envelope），未排期。
- **KPI §6.3 执行质量指标**：ExecJournal 侧车落盘后已具备统计条件，建议接入生产观察 2 周后再定阈值。

### 10.3 遗留项全景（按报告编号）

**资金/组合口径（最深遗留）：**
- §3.2.3 完整版 **未做**：`portfolio_capital`/`allocated_capital` 资金分配体系未定义——12 策略 ×15M=180M 风险资本 vs 账户 23.85M 的根本矛盾仍在；`account_gate_mode` 仍 warn 未升 veto；跨策略可用资金预留未做。日损闸（§3.2.4 策略层版）因资金分配未定保持默认关闭（默认 0=关闭）。
- §3.2.1 只做了 WARN 未改配置：`unit_var=20 万` 与 2% 上限的 75% 不协仍在（WARN 打印实际比例），改 `unit_var=150k` 或 `max_risk_per_mkt=2.67%` 需用户拍板（影响手数标定）。

**执行层（§3.4）：**
- userTag 归因/参与率上限/broker-side 条件单/共享账户 truth_mismatch 增量口径告警——除 ExecJournal 观察链外未动（前三项需 C++ 执行单元改造）。

**策略完备性（§3.1/3.7）：**
- 同 bar 反转配置化 + close 跳空止损（引擎层）+ envelope vs channel_only A/B + 日 bar 多层加仓——均未做（组合回测 sim 层已补跳空/平今，引擎 close 路径未动）。
- 涨跌停只暂停**加仓**，入场未禁（§3.7 建议的"禁止新开"半落地）。
- 换月窗口保护 / 数据陈旧硬闸（max_data_lag）/ catch-up 最大追价 / 收养写 s1_last / 流动性 ADV 上限——均未做。
- 过滤器生产零启用 + A/B（路线图 1.6）未做。

**工程卫生（§4）：**
- A1 God class 拆分 / A2 双系统门控合并 / A3 ExecutionModel / C12 assert→ValueError / C13-C14 引擎镜像合并 / C15 快照去重 / C16 monotonic 冷却 / C17 日志轮转——均未做（P2 级）。
- DRY 清单（周期映射/时间解析——norm_m5_time 已进 aggregate 但未抽象共享 timeutil）/ 测试 mock 工厂 / hypothesis 属性测试 / 黄金回测——均未做。
- A5 运行时数据同树、测试写 generated/——未隔离。
- 2.8 残余：make_deploy 原子化 / runMonSvr 纳入部署链 / systemd Persistent+OnFailure——未做（futu_mm 监控服务 auto-restart 死循环属其侧问题）。

**运营（§0.2-5/6.4）：**
- notify 仍 disabled（用户决策暂不启用，S 级告警仍只落 alerts.log——生产 10-20 夜盘 pp mismatch_persist 实例证明风险在持续累积）。
- 研究可复现性：仅 portfolio_bt.json 落 config_sha256/eval 区间；run_matrix/walkforward/slippage 报告未加配置/数据哈希。
- KPI 六组指标未建常态化度量（ExecJournal 侧车是 §6.3 的数据基础）。

### 10.4 建议的下一步优先级

1. **资金分配（0.1/1.5）**：定 portfolio_capital + 单策略 allocated_capital → 才能开日损闸阈值、才能评估 account_gate 升 veto。这是当前唯一阻塞"实盘风控闭环"的事项。
2. **notify 启用**（给一个 webhook URL 即几分钟接入）。
3. 执行侧车生产接入观察（今晚可起，零侵入）。
4. §3.1 四个口径 A/B（envelope vs channel_only 优先，影响所有历史成绩可比性）。
