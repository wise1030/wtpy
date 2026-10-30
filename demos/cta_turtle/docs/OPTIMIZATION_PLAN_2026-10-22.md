# cta_turtle 优化改进方案（2026-10-22）

> 基于 2026-10-20 复核报告（`STRATEGY_REVIEW_2026-10-20.md`，其 §10 已记录落地
> 对账）与 10-21 生产部署后的最新现状。**本文档是前瞻方案，不是遗留清单复述**：
> 已知遗留项只在"为什么现在做/怎么做"有增量时才展开。
>
> 现状基线：测试 449 全绿；生产 10-21 15:33 部署后连续 2 日零故障
> （0 error/traceback/ghost_fill/truth_stale，No-lastest 仅出现在 08:47/21:00
> 重启预热窗口）；持仓 rb/p/CF/IF 四空头账本健康。
> 代码基线：StraTurtle 2730 行/85 方法/26 处模式分支；core 16 模块；
> research 7 工具。

---

## 1. 架构设计

### A1. StraTurtle 拆分（P1，现在是正确时机）

10-20 报告已给蓝图，当时评估"时机偏早"（生产未部署）。**现在部署已完成且
两日稳定，变更爆炸半径的顾虑解除，正是动手窗口**。

| 迁出模块 | 内容 | 当前行数占比 | 拆出理由 |
|---|---|---|---|
| `exec/ExecutionRouter` | `_execute/_execute_dual/_size_and_cap/_apply_*_gates/_run_filter_chain/_reject_trigger` | ~550 行 | 单/双系统两套门控已实质漂移过一次（10-08 margin 方案B 时 dual 路径差点漏改）；多速度启用前必须合并 |
| `exec/FillTracker` | `_fill_pending/_fill_confirmed/_reanchor_*/_maybe_ghost_trim` | ~450 行 | 成交证据链是事故最密集区，独立后可用属性测试穷举"分批/部分成交/跨日"状态空间 |
| `exec/TruthMonitor` | `_truth_*/_periodic_truth_check/_account_available/_reconcile` | ~350 行 | 真值口径的每次失误都是 P0（9-15/9-21 两次），独立模块强制"所有外部真值先过时效闸"的单一入口 |
| `Telemetry` | `_alert/_veto_log/_dump_fund_live/journal 桥接` | ~200 行 | fund_live 无 is_backtest 守卫写源码树（A5）随之修复 |

**手法约束**：每步只迁不改语义，rb 基线（32.92%/314 笔）+ 组合回测
（+77.07%）双 A/B 逐位对拍后才合入；四步各自独立 PR。

### A2. ExecutionModel 接口：收敛 26 处模式分支（P1）→ v1 已落地

当前 `close/trigger × bt/live × d1/h1 × intrabar_stop × bt_no_simtick`
是隐式多维矩阵，26 处 `if` 散布全文件。历史教训：`with_oi=False` 只在
session_end 路径出现、`_reanchor_on_roll` 曾漏挂一条评估路径——都是
分支组合爆炸的产物。

方案：定义 `ExecutionModel` 协议（`fill_price()/confirm_evidence()/truth()/clock()`），
三个实现 `BacktestModel / LiveModel / BacktestNoSimtickModel`；StraTurtle 持有
一个 model 实例，模式分支收敛到 on_init 的一次选择。**收益不在行数，而在
"新模式 = 新实现类"而不是"全文件加 if"**——h1 二期（组合回测 h1）、broker-side
条件单都将受益。

### A3. 引擎镜像逻辑合并（P2，纯减熵）

`on_price`/`check_bar` 长短仓镜像约 140 行；`_advance_sim*` 三份推进逻辑。
抽 `_check_levels(px, is_bar)` + `_advance_sim(price_or_bar)`，参数化跳空口径。
预计净减 120+ 行且消除"改一边忘另一边"的事故类。

### A4. 运行时数据隔离（P2）

`generated/Logs/stradata` 与源码同树；测试的 `_dump_fund_live` 真实追加到
源码树。迁 `var/`（或 WT 惯例的 `generated/` 集中根），测试强制 tmp_path。
这是低风险纯卫生项，随 A1 顺带做。

### A5. 执行层：从"观察"到"可控"（P1，两阶段）

- **阶段一（纯策略层，已就绪可上）**：`scripts/exec_journal.py` 侧车接入生产
  systemd（零侵入），落盘 TRD_ORDER/TRADE 事件 → ExecAttributor 按周出
  滑点/部成率/拒单率报告。先建立执行质量基线（当前完全无量化）。
- **阶段二（需 C++ 执行单元改造，评估后决策）**：`userTag` 归因
  （WtMinImpactExeUnit 下单时注入策略名+序号）解决共享账户成交归属；
  参与率上限；条件单下沉到柜台。**前置条件是先有阶段一的测量**——
  没有"每分钟成交量占比"的实际分布，参与率上限是拍脑袋。

---

## 2. 策略逻辑优化

### S1. 资金分配体系（P0，唯一硬阻塞，其余风控项都等它）

当前矛盾：12 策略 × fallback 15M = 180M 名义风险资本 vs 账户实际 23.85M。
这不是参数问题是**制度缺失**：没有 `portfolio_capital → allocated_capital` 的
分配层。

方案（按依赖序）：
1. 定义 `risk.portfolio_capital`（共享账户中本策略组合的可支配额度，人工设定，
   例如 12M）；
2. 每策略 `allocated = portfolio_capital × weight`（weight 按品种流动性/波动
   分层，初版等权 1M/品种即够用）；
3. `RiskManager.equity` 基数从 `fallback_capital` 改为 `allocated_capital`；
4. **然后**才能开：日损闸阈值（3%/45万→3%/3万/策略才有意义）、
   `account_gate_mode` 升 veto、riskmon.base_amount 对齐。

联动决策（10-20 报告遗留的拍板项）：分配落地后 `unit_var` 标定重新评估——
1M/品种基数下 2% = 2 万/笔，`unit_var=200k` 只用到 10%，建议顺势降到
`unit_var=50k`（1N=5% 单策略分配额，4 层满仓=20%，与 max_risk_per_market
的语义重新自洽）。

### S2. 过滤器从"零启用"到"证据驱动启用"（P1）

过滤框架完整但生产零启用，设计初衷未落地。不要凭直觉开，按流程：
1. 用 `research/run_portfolio_bt.py` 做**逐一 A/B**（ADX≥25 / VolBand /
   LiquidityFloor 各开一次，约束后口径对比）；
2. 用 `run_walkforward` 验证 OOS 稳健性（防止过滤器参数过拟合 IS）；
3. 只启用 OOS 夏普改善且换手成本可覆盖的；其余保持 off 并把负结果写进文档
   （负结果也是结论，防止后人重复试）。

预期方向（先验，待证伪）：趋势策略在**高 ADX 区间**的期望显著更高是文献共识，
ADX 过滤器最可能过审；VolBand 上限可能砍掉最好的趋势段（警惕）。

**实证结果（2026-10-22 晚, `research/run_filter_ab.py`, 组合约束口径
60M/1tick/2019-01~2026-08, close+strength）——先验被证伪, 两个过滤器全部
否决, 证据留档防重复尝试**:

| 变体 | 总收益 | 回撤 | Sharpe | ΔSharpe | 笔数 |
|---|---:|---:|---:|---:|---:|
| baseline（无过滤器） | **+77.07%** | **29.17%** | **0.414** | — | 1927 |
| AdxTrend(14,≥25) | −2.48% | 44.03% | 0.133 | −0.281 | 1147 |
| VolatilityBand(250,10-90%) | −21.24% | 54.46% | 0.024 | −0.391 | 1590 |
| LiquidityFloor(5,≥5000) | +77.07% | 29.17% | 0.414 | 0.000 | 1927 |

解读：①海龟入场是"突破即趋势起点"，ADX≥25 过滤恰好在趋势刚起步
（ADX 尚低）时拦掉入场，强趋势段被迫迟到或错过 → 先验错误；
②VolBand 上限砍掉波动放大段，而最好的趋势恰在波动放大时展开 → 
负收益；③LiquidityFloor 5000 手地板对 12 品种全放行（横截面不可比，
绝对阈值不适合组合层面 —— 若需要应另写**相对流动性**变体）；
④口径注意：close 模式下引擎决策先于过滤链，过滤器否决不回滚引擎
S1 过滤器状态（与实盘 trigger 武装时裁决口径不同），A/B 结论的量级
足够大，该口径差不改变方向性结论。

### S3. 多速度组合落地（P1，h1 一期已铺路）—— 二期已落地，**结论：暂不生产化**

h1 一期（10-21）实证：rb h1 vs d1 同窗 +2.95%/Sharpe0.23/126笔 vs
+11.29%/1.59 —— 单看 h1 更弱，但速度研究的分散化结论（10/5 与 250/125 相关
仅 0.40）表明**价值在混合**。

**二期落地（2026-10-24）**：
- `run_portfolio_bt --period h1`：m5→h1 聚合（core/aggregate.py 同源
  函数）+ 小时级推进 + 日界保证金标记；`--codes` 品种子集过滤；
  触发模式逐 bar 武装（catch-up 跨日连续）+ check_bar 触碰。
- `research/run_h1_mix.py`：同篮子 d1/h1 各半资金混合 + 日收益
  相关（Pearson/Spearman）+ 分散化门槛判定（corr<0.7 且
  mix_sharpe>单速度）。
- 数据修复：本地 `CFFEX.IF.HOT_m5.csv` MetaTrader 格式头转标准格式
  （原文件 .bak_20261024 备份；此前 IF 的 h1 被静默跳过）。

**实证（rb+IF 篮子/60M/1tick/2019-01~m5 末端 2023-04, 留档
matrix_results/h1_mix{,_trigger}.json）——门槛未通过**：

| 口径 | d1 全资金 | d1@半 | h1@半 | 混合 | pearson |
|---|---|---|---|---|---|
| close | +24.1%/0.285 | +48.3%/0.345 | **−99.9%/−5.16** | −25.8%/−0.085 | 0.023 |
| trigger | +22.9%/0.281 | +45.8%/0.343 | **−99.9%/−5.60** | −27.1%/−0.111 | −0.041 |

解读：①相关≈0 证实**速度分散化理论成立**（d1/h1 收益流几乎正交）；
②但 h1 速度（20/10 窗口按 h1 bar 计）在本篮子独立期望深负——
手续费+滑点+假突破磨损把 −99.9% 打出来，混合被拖死；
③"相关低"不等于"值得混合"，独立期望必须为正是前提。
**结论：h1 不生产化**（该篮子/窗口口径）。若未来重启多速度路线，
方向是**优化 h1 速度本身的期望**（更宽窗口/过滤器/成本口径），
而非直接混合。rt m5 日历口径验证（步骤 4）随之搁置。

### S4. 信号口径的三个 A/B（P2）—— 已落地，附重大发现 S4-1b

已配置化（`base` 段四个 bool，默认全部保持旧行为，组合基线逐位
零漂移已验证）并全窗口 A/B（12 品种/60M/1tick/2019-2026/
strength，留档 matrix_results/caliber_ab.json）：

| 变体 | 总收益 | 回撤 | Sharpe | 笔数 |
|---|---:|---:|---:|---:|
| close 基线（默认口径） | +77.07% | 29.17% | 0.414 | 1927 |
| S4-3 旧跳空口径（--no-gap-stop） | +88.89% | 26.59% | 0.453 | 1927 |
| S4-2 禁同 bar 反转 | +65.45% | 31.83% | 0.377 | 1922 |
| trigger 基线 | +85.97% | 38.77% | 0.448 | 2589 |
| S4-1 纯通道武装（--no-envelope） | +85.97% | 38.77% | 0.448 | 2589 |
| **S4-1b 严格经典武装（--arm-exclude-signal）** | **+139.94%** | 37.84% | **0.572** | 2389 |

1. **S4-1 envelope vs channel_only：证伪——两者逐位相同**。根因：
   当前管线传入 `_arm_daily` 的通道窗口**已含武装用 bar**
   （chan[i] 不含 bar i，武装用 bar T-1=i-1 恰在窗口内），
   "max(通道, 当根极值)"恒等于通道本身（数学恒等，非统计巧合）。
   "envelope 是 trigger 与经典语义偏离源"的假设被排除——偏离源在
   通道窗口归属，即 S4-1b。
2. **S4-1b 重大发现**：`arm_exclude_signal=True`（武装位=prev_*
   通道，突破前 N 日高低点，完全排除信号 bar = 经典"次日挂单"
   语义）**约束 +139.94%/Sharpe 0.572（+53.9pp/+0.124），无约束
   口径同向改善（+152.5%→+206.3%）**。机制：避免"信号 bar 已把
   通道顶的自我延伸"型追涨入场，入场更少（2589→2389）而更优。
   已配置化（`base.arm_exclude_signal`）。**2026-10-24 用户拍板启用，
   默认已翻 True**（config 显式 `arm_exclude_signal: true`；黄金基线
   声明式刷新：rb 5M 单品种 35.58%→33.37%/0.258→0.239——单品种略降
   但 12 品种组合截面 +53.9pp 显著改善，组合口径才是决策依据）。
3. **S4-2 同 bar 反转**：close 模式的同 bar 出场后再入场贡献
   +11.6pp/+0.037 Sharpe → 默认保持 True；close/trigger 成绩差
   的归因自由度已量化（该自由度值这个钱）。
4. **S4-3 close 引擎跳空止损**：诚实口径（默认）使 close 基线
   比旧口径低 11.8pp（77.1% vs 88.9%）——"close 诚实地差于
   trigger"得以量化（旧口径美化了 close）。注意组合 sim 层 10-20
   已按 min(open,stop) 处理，该差异在 runBT/实盘引擎层生效；
   引擎层补齐消除了 sim/引擎双层口径差。

### S5. 风险口径的剩余缝隙（P2）

- **涨跌停只停加仓不停新开**（3.7 半落地项）：锁死时新开仓=把止损概率
  压到次日跳空上，应禁新开；
- **catch-up 最大追价**：停机后首 tick 远离武装位时按市价追入，滑差无界。
  加 `max_chase_n`（如 >1N 放弃等回踩）；
- **数据陈旧硬闸**：check_staleness 只 warn；HOT 冻结 16 天的事故（9-16）
  证明 warn 不够——`max_data_lag_days` 超过即拒绝武装；
- **换月窗口保护**：换月日 HOT 价差跳空会污染 N/通道一瞬，加换月日
  ±1 天的入场冷却（或至少观测记录）。

### S6. 双系统/多速度的激活前提（P2）—— 基础统一已落地，仍未生产启用

`S1+S2` 与 speeds 均休眠。2026-10-25 复核纠正了“二者无条件等价”
的过强表述：S1/S2 是引擎入场语义及 base 窗口预设，speeds 是同一
执行/风控单元内的 profile 组合；legacy 双系统的 ceil/floor units
不能机械映射为 50/50 speeds。

已落地：
- `core/profiles.py` 唯一 ProfileSpec 解析，StraTurtle/ConfigLoader/
  runLive/run_portfolio_bt 共用；legacy name、顺序、units/share 保留；
- 多档 speeds 强制显式 name/system，拒绝未知键，fs 跟随 base；
  长窗口按实际 profiles 校验 bar_cnt，防止永久 NaN 空转；
- 快照增加 profile 集合与语义指纹（system/windows/units/share），
  改名、漏子系统、错语义 fail-loud；TurtleEngine.restore 校验 system；
- 多子系统每次 confirm 后立即同步账本（同批预算预约），单位闸改为
  open_units + 新单位的前置容量检查；
- run_portfolio_bt 单 speed 使用真实 profile；多 profile 明确拒绝，
  不再静默按单 S1；run_speed_study 增加 --system S1|S2、open 跳空
  止损与完整口径 meta（55/20 + S1 不再误称生产 S2）；独立审查另修复
  出场 bar PnL：按实际出场价而非一律 close，剔除止损后的无持仓时段。

验证：498 测试全绿、ruff、黄金回测逐位一致；close 组合基线新旧 CSV
md5 相同（+77.07%/DD29.17%/Sharpe0.414，1927 笔），trigger 严格经典
武装基线亦逐字节一致（+139.94%/DD37.84%/Sharpe0.572，2389 笔）。

**激活前提更新（2026-10-30）**：多 profile 组合账本模拟与 share
保证金/累计风险分账已落地（RiskManager 按 `(code, profile)` 记账，
PortfolioSim 支持 S1+S2/speeds，gross 风险/费用、net 批末结算；
详见 Phase3 文档 §7）；反向 net/gross 订单级归属已在 Python 层解决
（`core/OrderAttributor.py` localid 归因链，零 C++ 改动，设计见
docs/ORDER_ATTRIBUTION_NO_CPP.md，C++ userTag 降级为加固备选项）；
故障注入回归 16 例；S1+S2 vs S1 组合 A/B 完成（matrix_results/
dual_ab.json）——**负结果**（约束后 +6.2%/DD55.6% vs 纯 S1
+75.3%/DD29.4%，双倍速消耗品种预算被勒死）→ **生产维持单 profile
S1**。剩余非代码项：notify webhook URL（待用户提供）；归因链生产
观测窗实证（部署后自动累积，偏差 >0.5 tick 告警并维持旧口径）。

---

## 3. 研究/验证体系

### R1. 黄金回测（P1，防回归的最后一道闸）

固定 (config_sha, 数据快照, 窗口) 断言 rb summary 哈希。本次 core/ 改名、
h1 改造都是"语义应保持零漂移"的重构，靠的是 449 例单测——但单测不覆盖
"整条回测链路的端到端等价"。黄金回测把这条链固化：任何 PR 导致基线漂移
必须在描述里显式声明预期差异。

### R2. 属性测试（hypothesis）（P2）

对不变量做随机穷举而非样例：快照 restore 幂等、`trim_to_lots` 只减不增、
棘轮位单调、RiskManager 账本守恒（Σfilled = book 总量）。FillTracker 拆出后
（A1）尤其值得做——分批成交的状态空间样例测试覆盖不完。

### R3. 研究产物全面带哈希（P2）

目前只有 portfolio_bt.json 带 config_sha256/eval 区间；run_matrix/
walkforward/slippage 的报告仍无配置/数据指纹。统一 summary schema：
`{config_sha256, data_first, data_last, entry_mode, stop_n_mode, capital,
slippage, code_version(git describe)}`。这是 10-20 报告 KPI 6.1 的落地。

---

## 4. 运维与告警

| 项 | 动作 | 现状 |
|---|---|---|
| notify | 给 webhook URL 即启用（分钟级） | **S 级告警只落本地 alerts.log，风险持续累积中**（10-20 pp mismatch_persist 20 分钟无人感知） |
| tick 静默检测 | session 内 30 分钟无 on_tick → alert（9-28 商品全天中断靠人工发现） | 未做，实现简单 |
| 日志轮转 | alerts.log/fund_live.csv 按月分文件 | 未做，长期增长 |
| 执行质量周报 | exec_journal 落盘 + 周度归因报表 | 侧车就绪待接入 |
| futu_mm 监控死循环 | wt-monitor-turtle.service 持续 auto-restart | 其侧问题，建议知会 |

---

## 5. 优先级矩阵与排期建议

| 优先级 | 事项 | 依据 |
|---|---|---|
| **P0** | S1 资金分配（含 unit_var 联动标定） | 唯一硬阻塞：日损闸/veto/组合真实口径全等它 |
| **P0** | notify 启用 | 一行配置的事，风险最高暴露面 |
| **P1** | A1 StraTurtle 拆分（四步） | 部署窗口已过、运行稳定，变更爆炸半径的正当时机 |
| **P1** | A5-1 执行侧车生产接入 | 零侵入、立即获得执行质量基线 |
| **P1** | S2 过滤器 A/B → 证据驱动启用 | 设计初衷的最后一块未验证拼图 |
| **P1** | R1 黄金回测 | 后续所有重构（A1/A2/A3）的安全网，应先于拆分落地 |
| **P2** | A2 ExecutionModel / S3 多速度组合 / S4 口径 A/B | 依赖 A1 或 S1 完成 |
| **P2** | S5 风险缝隙 / S6 双系统激活 / A3 引擎合并 / R2/R3 | 持续项 |
| **P3** | A5-2 C++ 执行单元改造（userTag/参与率） | 需 C++ 编译链，先测量再动手 |

**一句话**：资金分配与告警通道是"让系统敢上实盘"的最后两块；拆分与
ExecutionModel 是"让系统敢继续改"的关键投资；过滤器 A/B 与多速度是
"让策略多赚"的证据驱动路径——三者分别对应风险、工程、收益三条主线，
P0 两项当天可完成，其余按依赖序推进。
