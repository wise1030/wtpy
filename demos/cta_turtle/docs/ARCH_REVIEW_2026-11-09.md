# cta_turtle 业务 / 架构 / 代码复核（2026-11-09）

> **2026-11-09 复核修订**：对本文全部关键论断回源码/数据二次验证后修正 4 处、
> 补充 2 条——
> ① B1 层数算术修正：单笔 2% 恰允许 **4 层**（原误写 3 层），与 `pyramid_layers=4` 自洽；
> ② B4 修正：执行器 `pricemode=1` 是**对手价主动成交**（原误写"被动排队"）；
>    `fill_timeout` 600s 为**硬编码**（原 Phase 0 误标"配置"）；`stra_set_position`
>    的 limitprice/stopprice 走 C++ 条件单路径（每次 on_calculate 清空），**不可用**
>    作紧急出场杠杆，可达杠杆只有 `executers.yaml` 的 lots/span/offset；
> ③ B8 修正：快照语义不匹配是 `validate_snapshot` 抛 ValueError **fail-loud 崩启动**
>    （原只写"静默继续"，实际两种失败模式并存）；
> ④ B2/Phase 1 补充：统计法换月检测作为低成本先行路径；**既有参数决策
>    （arm_exclude 启用、S1+S2/ADX/h1 负结论）全部建立在未复权数据上，复权后
>    必须重验方向性结论**，不只是重跑成绩留档；
> ⑤ B1 严重度定性：这是"决策可比性 P0"而非"超额风险 P0"——生产相对 60M 研究
>    偏紧 4 倍（方向保守），安全隐患在 `account_gate_mode=warn` + 共享账户。

> 范围：`demos/cta_turtle` 全部 Python 源码、配置、研究脚本、测试与生产运行数据。
> 方法：静态结构统计（AST/依赖面/复杂度）+ 关键路径逐段阅读 + 生产真值与配置核对 +
> 回测产物口径核对。本文不重复 2026-10-20 复核已落地项，重点回答三个问题：
> **业务口径是否自洽、架构是否可持续、代码是否还能优雅地演进**。
>
> 结论先行：**信号状态机本身是这套系统最健康的部分；真正的问题在“资金/数据/执行”
> 三个口径层，以及“实盘与回测是两套实现”的结构性重复。** 下面按影响排序给出证据、
> 方案与验收标准。

---

## 0. 结论摘要

### 0.1 总体判断

**做得好的地方（必须保留）**

1. `core/TurtleEngine` 纯逻辑、零框架依赖，决策-确认两段式、边沿触发、有状态
   fail-safe、棘轮 N 口径，均可脱离 wtpy 单测；
2. 过滤链只读、风控缩量优先、真值/逐笔成交/fees 三级证据链、write-ahead outbox、
   订单级归因链，方向都是对的；
3. 事故驱动出来的自愈（孤儿 pending、catchup 可重试、ghost trim、exec recover、
   hotdaily 重启）都有测试与文档沉淀；
4. 614 例测试 27s 跑完、ruff、黄金基线、部署 manifest、运维手册，工程完整度
   明显高于同类 CTA demo。

**必须正视的地方**

这套系统目前有三个“看起来在运行、实际上口径对不上”的层：

| 层 | 表面 | 实际 |
|---|---|---|
| 资金 | 12 品种、每品种 15M 权益、10% 总风险 | 账户 25.3M；回测 5M/60M；C++ riskmon 15M；`portfolio_capital=0`；同一账户被 12 个策略各按 15M 计算 |
| 数据 | 主力连续 HOT、ATR/通道 | HOT 是**未复权拼接**；现有 12 品种/OOS 产物的 `roll_calendar_products=0`；单次换月跳空 10%~17% 直接进入 N/通道/PnL |
| 执行 | 回测 +86%、实盘同一套逻辑 | 研究用 `PortfolioSim`（1582 行）重写了一遍门控/武装/换月；实盘用 `StraTurtle`；两者没有自动化一致性测试 |

这三层不解决，后面所有“调参、加品种、上多 profile、换执行算法”的收益都不可信。

### 0.2 最优先的 8 件事

| # | 优先级 | 动作 | 一句话理由 |
|---|---|---|---|
| 1 | P0 | **统一资本模型**：账户/组合/策略/品种四级资金，回测与实盘同源 | 5M/15M/25M/60M 五套基数并存，任何收益回撤都不可比 |
| 2 | P0 | **HOT 换月复权**：重建全历史换月日历，信号用复权序列、执行用真实价 | 未复权序列的 10%+ 跳空会同时污染 N、通道和 PnL |
| 3 | P0 | **实盘/研究一致性测试**：同一份 rb 数据，`runBT` 与 `PortfolioSim` 决策逐笔对拍 | 现在只有注释声明“同口径”，没有证据 |
| 4 | P0 | **运维安全网**：notify 启用、tick 静默检测、快照损坏不静默继续、周期快照 | 目前所有 S 级告警只落本地文件；数据断流无人知道 |
| 5 | P1 | **本策略成交账本**（event-sourced PositionLedger） | 共享账户下 ghost trim 被禁用、真值无法归属的根因是缺“自己的成交账” |
| 6 | P1 | **执行紧急度分级**：止损/通道出场走快速通道，入场可耐心；加参与率上限 | 646s 才确认的止损，和 1 手切片，是真实风险 |
| 7 | P1 | **仓位随权益缩放 + 流动性/容量上限** | `unit_var` 固定 20 万，权益翻倍风险减半、权益腰斩风险翻倍 |
| 8 | P2 | **领域核心抽取**：`domain/` + `runtime/pipeline`，StraTurtle 只做 wtpy 适配 | 71 个 host 依赖、33 个薄转发，变更爆炸半径仍然很大 |

### 0.3 不建议做的事

- **不要**为了“架构好看”直接重写实盘壳。生产在跑，正确姿势是 strangler：先抽纯逻辑、
  再换适配器、黄金基线逐位验证。
- **不要**启用 S1+S2、ADX/VolBand、h1。已有 A/B 负结果，证据不支持。
- **不要**在没有复权数据之前继续用 12 品种组合收益做资金决策。
- **不要**把 C++ userTag 当作归因前提。现有 localid/outbox/trades.csv 链已经够用；
  先把账本做实，再评估是否值得改 C++。

---

## 1. 现状量化

| 指标 | 数值 | 说明 |
|---|---|---|
| Python 源码（不含 deploy/generated） | 27,939 行 | `find ... wc -l` |
| `StraTurtle.py` | 2,347 行 / 102 方法 / 33 个薄转发 | 仍是最大单体 |
| `core/` + `execution/` + `filters/` | 6,938 行 | 纯逻辑 + 执行适配 |
| `TurtleEngine.py` | 1,148 行 | 信号状态机，零框架依赖 |
| 测试 | 63 文件 / 614 例 / 27.5s | 全绿 |
| 函数返回注解覆盖 | 162/397 = 40% | 无 mypy/pyright |
| `except Exception` | 44 处，其中 11 处静默 `pass/return None` | 事故史证明静默降级代价高 |
| ≥150 行函数 | 5 个（`on_init` 264、`ConfigLoader.load` 199、`execute_dual` 171、`cap_entry` 160、`_process_open_intents` 150） | 变更热点 |
| `StrategyHost` 依赖面 | 71 个唯一属性、94 条声明 | 4 个模块仍通过字符串白名单读写策略私有状态 |
| 配置字段 | base 26 + risk 14 + instrument 10 | 生产 YAML 约 200 行 |
| 生产品种 | 12 品种 / 6 相关性组 / 单 profile S1 | `entry_mode=trigger`、`stop_n_mode=ratchet` |
| 生产账户（2026-11-09 读取） | balance 25.32M / margin 12.82M / available 12.47M | SimNow 共享账户，含其他策略 |
| 黄金基线 | rb d1 trigger 2018-01~2023-04，5M，+9.73% | 与生产 15M 不同基数 |

---

## 2. 业务逻辑：功能矩阵与五个口径级问题

### 2.0 功能矩阵：经典海龟 vs 当前实现

| 能力 | 经典海龟 | 当前实现 | 评价 |
|---|---|---|---|
| 多市场 | 是 | 12 品种 / 6 组 | 完整 |
| N / ATR | ATR(20) | Wilder ATR(20)，向量化 | 完整 |
| 入场 | 20/55 日 Donchian 突破 | S1 20 / S2 55，close/trigger 双模式 | 完整 |
| 出场 | 10/20 日通道 | S1 10 / S2 20，trigger 盘中触碰 | 完整 |
| 止损 | 2N | 2N + gap_stop + ratchet N | 完整，且比经典更细 |
| 加仓 | 每 0.5N 一层，最多 4 层 | 同左，逐层冻结 N | 完整 |
| S1 过滤器 | 上一笔盈利则跳过 + 55 fail-safe | 同左，sim 单推进 | 完整 |
| S2 / 双系统 | 双系统各半单位并行 | 能力有，生产未启用（A/B 负结果） | 有意为之 |
| 单位规模 | 1 unit = 账户 1% / (N×合约乘数)，随权益复利 | `unit_var` 固定 20 万，不随权益缩放 | **缺口** |
| 相关性组 | 同组单位数限制 | 静态组 + 组内品种数上限，无组风险预算 | 部分 |
| 换月 | 真实合约移仓，连续价格 | 实盘 re-anchor 持仓价，但 HOT 数据未复权 | **缺口** |
| 流动性/容量 | 市场容量约束 | 有 LiquidityFloor 过滤器但未启用；无 ADV 上限 | **缺口** |
| 执行 | 市价/限价，关注冲击 | `set_position` + MiniImpact 1 手切片，无紧急度 | 部分 |
| 组合风险 | 单位/组/总风险 | 单笔/单品种累计/总敞口/保证金/单位/账户/日损 | 完整，但基数口径混乱 |
| 共享账户 | — | 真值 + 归因链 + outbox，但无本策略成交账本 | 部分 |
| 回测/实盘一致 | — | 两套执行实现，仅注释声明同口径 | **缺口** |
| 可观测性 | — | 日志 + alerts.log，无 metrics/心跳/结构化事件 | 部分 |

**结论**：海龟“信号层”的功能是完整的；缺的是**资金复利、连续合约、容量、执行时效、
本策略账本、可观测性**——都属于生产化能力，而不是策略逻辑。

### B1（P0）资金与风险预算存在五套口径

**证据**

| 位置 | 资金基数 | 用途 |
|---|---|---|
| `runBT.py --capital` 默认 | **5,000,000** | 黄金基线、单品种回测 |
| `research/run_portfolio_bt.py --capital` 默认 | **60,000,000** | 12 品种组合、OOS、容量研究 |
| `config/turtle_config.yaml risk.fallback_capital` | **15,000,000** | 生产每策略权益基数 |
| `deploy/cta_turtle/config.yaml riskmon.base_amount` | **15,000,000** | C++ SimpleRiskMon |
| 实际账户 | **25,322,393**（available 12.47M） | SimNow 共享账户 |
| `risk.portfolio_capital` | **0（关闭）** | 分配机制未启用 |

更细的数学不一致（`ConfigLoader.risk_coherence_warning` 已经打印，但只是 WARN）：

```
unit_var × stop_atr_mult = 200,000 × 2 = 400,000   # 1 个名义单位的 2N 风险
fallback_capital × max_risk_per_mkt = 15,000,000 × 2% = 300,000
→ 每笔最多只能下 75% 个名义单位
```

也就是说：**“1 个 unit = 20 万 1N 风险”的仓位语义，被 2% 单笔上限削成了 0.75 unit**。
单笔闸 2%（300k）恰好允许 **4 层**（4×2%=8% 正好顶满 `max_risk_per_market`，
也与 `pyramid_layers=4` 自洽）；`max_total_risk_frac=10%`（1.5M）组合口径约
**5 个首层**。12 个品种的宇宙，实际只能同时持有约 5 个首层，加仓很快吃满预算。

**影响**

0. **严重度定性**：这是“决策可比性”层面的 P0，不是“超额风险”层面的 P0——
   生产相对 60M 研究是偏紧的（各项预算恰为 1/4），方向保守；真正的安全隐患
   是 `account_gate_mode=warn` + 共享账户（2026-09-09 资金耗尽 3357 次拒单的同类）。
1. 生产实际风险/收益无法与任何回测对齐：生产 15M 的风险预算是 60M 组合回测的 1/4；
2. 12 个策略各自拿 15M 算保证金（10% = 1.5M/策略，合计上限 18M），
   但 `account_gate_mode=warn` 只告警不拦截；共享账户的真实可用资金不是硬约束；
3. 任何“12 品种 +58.5%/+77%”的结论都不能直接映射到生产，因为生产的下注规模
   和约束完全不同；
4. 仓位不随权益缩放，权益上涨时风险占比下降、权益下跌时风险占比上升，
   与经典海龟的复利口径相反。

**方案：资本模型 v2（建议作为独立配置段）**

```yaml
capital:
  mode: portfolio              # portfolio | per_strategy
  portfolio_capital: 25000000  # 真实可支配资金（用户确认）
  alloc_weights:               # 缺省等权；可按品种风险/保证金调整
    SHFE.rb.HOT: 1.0
    ...
  unit_risk_frac: 0.01         # 1 个 unit 的 1N 风险 = 分配资金 × 1%
  max_risk_per_trade_frac: 0.02
  max_risk_per_market_frac: 0.08
  max_portfolio_risk_frac: 0.10
  max_margin_usage: 0.10
  account_gate_mode: veto      # 共享账户必须硬约束
  account_reserve_frac: 0.20
```

派生规则：

- `unit_var(code) = allocated_capital(code) × unit_risk_frac`，不再手写 200k；
- `unit_risk_frac × stop_atr_mult = max_risk_per_trade_frac`（1% × 2 = 2%），
  保证“1 个 unit 的 2N 止损”正好等于单笔上限，不再出现 0.75 unit；
- `portfolio_capital` 是唯一总池，`allocated_capital` 是策略/品种子账户；
- 回测、`runLive`、`runBT`、`run_portfolio_bt` 全部从同一个 `CapitalModel` 取值；
- `account_gate_mode=veto` + `account_reserve_frac` 把券商 `available` 变成硬闸门。

**验收**

1. `ConfigLoader --check` 打印“总资金 → 分配 → unit_var → 单笔上限 → 组合上限”
   一条链，且不出现 WARN；
2. 同一配置在 `runBT`、`run_portfolio_bt`、`runLive` 三处打印的 `unit_var` 完全一致；
3. 12 品种组合在 15M/25M/60M 三个资金档位各跑一遍，输出“资金-收益-回撤-保证金峰值”
   对照表；生产选择其中一档并写进配置注释；
4. 共享账户 `available` 低于安全垫时，新开仓被否决（有单测）。

> 注：这会改变生产手数。建议先以 `shadow` 模式跑 1~2 周：按新模型计算手数并记录
> `shadow_lots`，实际仍按旧手数下单，确认无异常后再切换。

---

### B2（P0）主力连续数据未做换月复权

**证据**

1. `research/run_portfolio_bt.py` 有 `_roll_adjusted_hlc()`，但只在
   `common/hots.json` 存在换月规则时生效；
2. 本地 `demos/common/hots.json` 的规则数是 **0**；生产服务器 `common/hots.json`
   只有 2026-09 之后的 23 条规则，且 `oldclose/newclose` 大多为 0；
3. 研究产物明确记录 `"roll_calendar_products": 0`、`"roll_events": 0`
   （`matrix_results/portfolio_oos_phase2.json`、`portfolio_oos_capacity40.json`）；
4. 原始 HOT 日线存在远超涨跌停的跳空：

| 品种 | 日期 | 前收 → 开 | 跳空 | 备注 |
|---|---|---|---|---|
| INE.sc | 2020-05-06 | 229.0 → 267.3 | **+16.72%** | 明显换月 |
| DCE.m | 2023-08-14 | 4487 → 3860 | **-13.97%** | 明显换月 |
| INE.sc | 2022-10-10 | 634.4 → 705.0 | **+11.13%** | 明显换月 |
| INE.sc | 2026-03-24 | 834.6 → 740.1 | **-11.32%** | 明显换月 |
| DCE.pp | 2026-03-10 | 8034 → 8727 | **+8.63%** | 明显换月 |
| SHFE.rb | 2010-03-09 | 4189 → 4615 | **+10.17%** | 明显换月 |

   同期中位跳空：rb 0.089%、m 0.249%、sc 0.551%。10%+ 是 20~100 倍中位数；
   且多数超过对应交易所的日内涨跌停，**极可能是换月拼接**（最终以重建的换月日历核对为准）。

**影响**

1. **N/ATR 被放大**：一次 14% 跳空进入 20 日窗口，ATR 可能翻倍，随后 20 天
   仓位被腰斩；55 日通道也包含旧合约价格，产生假突破/假出场；
2. **PnL 被伪造**：研究侧没有 `_apply_rolls` 时，持仓按新合约价格重估，
   单次换月可产生相当于权益 5%~10% 的虚假盈亏（按 1 个 unit、15M 基数估算）；
3. **实盘与研究的偏差来源不是策略，而是数据**：实盘 `stra_get_bars` 读的是同一份
   未复权 HOT；`_reanchor_on_roll` 只平移持仓价格，**不平移历史 bars**，所以
   换月后的指标窗口仍然带跳空；
4. 黄金基线（rb）也建立在未复权数据上；它只能保证“代码改动不漂移”，
   不能保证“策略口径正确”。

**方案：连续合约复权管道（信号/执行分离）**

推荐“**后复权到最新合约**”的口径（最新一段价格 = 真实可交易价格）：

```
原始 HOT:   ... old_close | new_open ...
复权 HOT:   ... old_close + Δ | new_open ...      Δ = new_close - old_close
其中 Δ 在每次换月时累加，历史 bars 全部平移，最新 bar 保持真实价
```

这样：

- ATR/Donchian 在连续价格上计算，不再被跳空污染；
- 当前 bar 的价格就是真实合约价格，触发位/止损位不需要二次换算；
- 换月时，策略层仍然按 Δ 平移持仓层价/武装位（现有 `_reanchor_on_roll` 已做）；
- 回测和研究使用同一份复权序列，结果可比。

落地步骤：

1. **重建全历史换月日历**：用真实合约日线（Sina 逐合约或 tqsdk）按持仓量选主力，
   记录 `date/from/to/oldclose/newclose`，写入 `common/hots.json`（全历史，不只 2026-09）；
2. **`scripts/update_hot_daily.py` 增加复权**：维护 `adj_offset`，重建 HOT CSV/DSB 时
   对历史段加 Δ；保留 `*_raw.csv` 供执行/审计；
3. **`DataChecker.check_continuity()`**：检测单日跳空 > 品种涨跌停、重复日期、
   缺失交易日、复权后跳空是否收敛；异常在 preflight 阻断或告警；
4. **研究侧**：`run_portfolio_bt` / `run_matrix` / `golden_bt` 统一读取复权序列，
   并在 summary 写 `roll_calendar_products`、`roll_events`、`adj_mode`；
5. **重跑基线**：黄金、12 品种矩阵、OOS、容量研究全部重跑；旧结果标注
   `raw_hot_unadjusted` 并存，不覆盖。

**两条补充（复核后新增）**

- **低成本替代路径**：全历史换月日历若暂时拿不到逐合约数据，可先用**统计法**——
  对现有 HOT 序列检测“跳空 > 品种涨跌停（或 > k×ATR）”的 bar，`delta =
  open − prev_close` 即换月价差，自动生成日历并人工抽查清单。这能让
  Phase 1 在一周内先跑通复权与重验，逐合约日历后续再精化。
- **必须重验的不只是成绩，还有参数决策**：现有生产参数选择（
  `arm_exclude_signal=true` 启用、S1+S2 不启用、ADX/VolBand 过滤器不启用、
  h1 不启用、口径 A/B 结论）**全部建立在未复权数据上**。复权后必须重验这些
  决策的方向性结论，而不只是把新成绩留档。

**验收**

1. 复权后各品种最大单日跳空 ≤ 该品种涨跌停（或明确列为例外并人工核对）；
2. `roll_calendar_products=12`、`roll_events>0`；
3. rb 黄金新基线（声明式更新）与旧基线的差异可解释：差异主要来自换月窗口的
   N/通道/PnL，而不是代码；
4. 生产 `DataChecker` 启动日志打印“复权模式 + 最近换月日 + 复权后最大跳空”。

> 这是本文**最建议先做**的一项研究修复。它不需要改 C++，只改数据管道和策略读取层；
> 但会改变所有历史成绩，必须先和用户确认“旧结果作废/并存”的口径。

---

### B3（P0/P1）回测与实盘是两套执行实现

**证据**

- `research/run_portfolio_bt.py` 1,582 行，`PortfolioSim` 重新实现了：
  `_arm_day`（武装）、`_size_cap`（门控）、`_entry` / `_add` / `_exit`、
  `_filter_ok`、`_apply_rolls`、`_dispatch_trigger`、`_trigger_open`、
  h1 聚合、组合账本；
- 实盘路径是 `StraTurtle` + `execution/router.py` + `fill_tracker.py`；
- 代码注释多处写“与实盘 `_arm_daily` 同口径”“与实盘 trigger 管线逐一对应”，
  但**没有测试证明**；
- 黄金回测只覆盖 rb 单品种、d1、trigger、5M；12 品种组合研究全部走
  `PortfolioSim`，两者从未逐笔对拍。

**影响**

1. 研究结论（12 品种 +77%、trigger +86%、容量 40%）无法保证是实盘可复现的；
2. 任何一侧修 bug（例如武装口径、换月、S1 结算），另一侧可能漏改；
3. 历史上已经出现过“研究口径修了、实盘漏挂”的事故（`_reanchor_on_roll`
   漏挂 session 路径、catchup 闸门等），这是同一类风险。

**方案：`StrategyCore` + 一致性测试**

短期（1~2 周，不改生产行为）：

1. 写 `tests/test_live_vs_sim_parity.py`：
   - 同一份 rb bars + m5 + 配置；
   - `PortfolioSim` 与 `runBT`（WtBtEngine + `StraTurtle`）各跑一遍；
   - 比较 **决策序列**（日期、kind、direction、price、lots），允许成交价/费用差异，
     但信号与门控必须逐笔一致；
   - 先只覆盖 rb d1 trigger；稳定后扩到 close 和第二个品种。
2. 如果对不上，先把差异分类：数据窗口、武装时点、过滤链、风控份额、撮合时点；
   每类修一侧或写进“已知差异”白名单，禁止静默。

中期（3~4 周）：

3. 抽 `core/StrategyCore`（纯逻辑）：
   ```
   evaluate_bar(snapshot) -> list[Decision]
   evaluate_tick(snapshot) -> list[Decision]
   size(decision, portfolio_state) -> Intent
   ```
   `StraTurtle` 与 `PortfolioSim` 都只做“数据适配 + 撮合适配”，不再各自实现门控。
4. `PortfolioSim` 逐步退化为 `StrategyCore` 的薄适配器；研究脚本不再复制逻辑。

**验收**

- parity 测试在 CI 中常驻；
- 12 品种组合重跑，`PortfolioSim` 与逐品种 `runBT` 的信号数量/方向差异 < 0.1%
  （撮合差异单独统计）；
- 新增一个“决策日志”字段，生产/回测可直接 diff。

---

### B4（P1）执行质量与止损时效

**证据**

- 生产 2026-11-04：pp 止损 89 手，21:08 触发 → 21:53 才 `exit confirmed`，
  耗时 **646s**，滑点 1.15 点；
- 执行单元 `MiniImpact` 按 1 手切片（`byvol@1.00`），大单需要数分钟；
- CTA 层没有撤单 API（`CtaContext` 只有 `stra_set_position`，无 `stra_cancel`），
  订单状态完全依赖 C++ 执行单元；
- 已知 MiniImpact 盘中懒创建会 tick 断供卡死，现有对策是 on_init 预热、每日重启、
  `WT_EXEC_RECOVER=restart`。

**影响**

1. 止损是保护性订单，646s 的延迟在快速行情中可能多亏数 N；
2. 执行器 `pricemode=1`（对手价）是**主动成交**而非被动排队，但单次仅 1 手、
   发单间隔 500ms（`executers.yaml`: `lots:1, span:500`），大单完成时间被
    market impact 和 5s 超时重挂支配；
3. 入场和止损用同一套执行策略，无法区分“可以等”和“不能等”；
4. `fill_timeout` 600s **硬编码**（`fill_tracker.py:172/289/402/458`，fees
   兜底路径 1800s），且只告警不升级，也无“订单未派发”检测。

**方案**

1. **紧急度分级**（策略层）：`EXIT_STOP`/`EXIT_CHANNEL` 紧急、`ENTRY`/`ADD`
   耐心，在 `TargetIntent` 上加 `urgency`。
2. **执行器配置杠杆**（不改 C++ 的可达手段，按品种调 `executers.yaml`）：
   提高 `lots`（如 1→5）、缩短 `span`、调 `offset`/`pricemode`——大单边出场
   从 ~89 笔降到 ~18 笔，完成时间数量级下降，代价是冲击成本，对止损是值得的。
   ⚠️ 已核验排除的伪杠杆：`stra_set_position(..., limitprice/stopprice)` 走
   C++ **条件单**路径（`CtaStraBaseCtx.cpp:981` 每次 on_calculate 清空），
   无法用于紧急出场（2026-08-24 已论证弃用）。
3. **超时升级按紧急度分档**：止损 30~60s / 入场 600s（需代码改造 + 单测，
   注意与 fees 兜底 2x 放行窗口、ghost/exec_recover 状态机的联动）。
4. **订单派发检测**：`set_position` 后 N 秒无该 code 新订单/成交记录 →
   立即“零派发”告警（早于 600s）。
5. **预热/重启保留**，加重启前检查：无 open intents/exit orders 才允许计划内重启。

**验收**

- 止损从触发到确认的 p50/p95 纳入日报；目标 p95 < 120s（按品种流动性分档）；
- 新增零派发检测单测/故障注入；
- 滑点按紧急度分组统计，写进 `run_slippage_sens` 报告。

---

### B5（P1）仓位不随权益缩放，缺流动性/容量约束

**证据**

- `PositionSizer.layer_lots()` 只用固定的 `unit_var`，不看当前权益；
- `RiskManager` 的预算是比例制，但 `unit_var` 是绝对值，二者只在配置标定时一致；
- 没有 ADV/OI 参与率上限；`LiquidityFloor` 过滤器存在但生产未启用；
- `margin_rate=0.12` 是全局估值，未按品种区分（IF 与 rb 的实际保证金率不同）。

**影响**

1. 权益增长时，实际风险占比下降，资金利用率越来越低；权益回撤时，实际风险占比上升，
   与“固定比例风险”相反；
2. 在流动性较差的品种/合约上，理论手数可能超过市场可承接量，回测无法体现冲击成本；
3. 保证金预估偏差会让 `margin` 闸门过松或过紧。

**方案**

1. `unit_var` 改为 `equity × unit_risk_frac`（按品种分配权益），在每次入场/加仓时重算；
   加 `unit_var_floor`/`unit_var_cap` 防止极端值；用 A/B 验证复利效果。
2. `RiskManager` 增加流动性规则：
   ```
   lots ≤ ADV20 × participation_rate × horizon_days / holding_days
   lots ≤ OI × oi_rate
   ```
   默认 `participation_rate=0.1`、`horizon=1`；`LiquidityFloor` 作为可选的信号过滤。
3. `margin_rate` 改为 `margins.json` 按品种配置（或从交易所参数表生成），
   至少给 IF/SC/黑色/化工分档；`DataChecker` 校验缺失品种。
4. 在 `cap_entry` 返回的 `reasons` 中增加 `liquidity(cap=...)`，监控可见。

**验收**

- 组合回测在 40% 容量约束下重跑，结果与现有 `portfolio_oos_capacity40` 对照；
- 生产 `fund_live.csv` 增加 `risk_used/portfolio_heat/liquidity_cap` 字段；
- 单品种手数不超过 ADV20 的 10%（可配置）。

---

### B6（P2）组合层约束仍是静态的

**现状**

- 6 个静态相关性组，`max_units_per_group=2`、`max_units_total=6`；
- 没有组级风险预算、没有滚动相关性、没有组合波动率目标；
- `portfolio_heat`（总在途风险/权益）没有暴露到监控。

**影响**

- 同组两个品种可能高度相关（如 m/p、TA/MA），2 个 unit 的名义约束不等于 2 个 unit 的
  风险约束；
- 12 品种的分散化收益无法量化，也无法根据市场状态调整。

**方案（不急于上，先度量）**

1. 在 `RiskManager` 增加 `group_risk` 账本：同组在途风险 ≤ `equity × group_risk_frac`；
2. 用 60 日滚动收益相关矩阵做“有效单位数”统计，写进日报；
3. 组合波动率目标可选：按 20 日组合波动缩放 `unit_var`（与 B5 一起做）；
4. 不要上复杂优化器；海龟的核心是规则透明，先把“组风险”做实。

---

### B7（P1）真值/账本/幽灵仓的根问题：缺“本策略成交账本”

**现状**

- `RiskManager` 的账本来自 `engine.layers`，`engine.layers` 来自 `confirm_*`；
- 成交确认依赖真值增量 / `trades.csv` / fees 代理；
- 共享账户下 `truth_rebuild_mode=alert`，`ghost_trim` 直接禁用（因为券商净仓含其他策略）；
- `OrderAttributor` 已经能把 localid/成交映射到意图，但只用于观测，没有形成持久账本。

**影响**

- 幽灵仓只能告警，不能安全自愈；10-09 pp 233 手悬空就是例子；
- 引擎层与信号/券商不一致时，要等 `recon_patience=3` 天；
- 风险账本可能虚高/虚低，导致误否决或超预算。

**方案：event-sourced `PositionLedger`**

```
Intent(enter/add/exit, lots, profile, id)
  → Order(localid, status)
  → Fill(trade_id, lots, price, ts)
  → PositionLedger (per code/profile)
  → engine.layers / RiskManager / reconcile 都读同一账本
```

要点：

1. 账本只消费**本策略**的成交（localid/归因链/trades.csv 前缀过滤），不读账户净仓；
2. `engine.layers` 变为账本的投影（或至少启动/对账时以账本为准）；
3. `ghost_trim` 用“账本仓 vs 引擎仓”判断，不再用券商净仓 → 共享账户也能安全 trim；
4. `recon_patience` 对大额失配自适应：失配 > 10% 或 > N 手时，1 次确认即重建；
5. 账本持久化进快照（v6），支持重启回放。

**验收**

- 用 2026-09/10 的 CF/p/pp 事故数据回放，账本能复现“真实成交 → 引擎层”的收敛；
- 共享账户下 ghost trim 单测：只裁本策略幽灵，不碰其他策略；
- `recon_patience=3` 的盲周场景不再依赖“重启不归零”补丁。

---

### B8（P0 运维）安全网缺口

| 缺口 | 现状 | 风险 | 建议 |
|---|---|---|---|
| notify 未启用 | `notify.enabled=false`，告警只落 `Logs/alerts.log` | S 级事件无人知道（9-17 事故根因） | 用户提供 webhook；启用 + 自测 |
| tick 静默 | 无“session 内 N 分钟无 tick”检测 | 9-28 商品全天零 tick，靠人工从盈亏发现 | 每策略记录 `last_tick_ts`，心跳进程告警；静默时禁止新开仓 |
| 快照失败 | JSON 损坏静默继续；语义不匹配则 `validate_snapshot` 抛 ValueError **fail-loud 崩启动** | 前者空状态收养/重复开仓，后者单品种不可用 | DEGRADED 模式统一覆盖：不新开仓、按信号/真值重建、告警 |
| 周期快照 | 只在事件点 `_persist` | 崩溃窗口丢最近状态 | 每 N 分钟/每根主 K 落一次；保留最近 3 份 |
| 定时器失败 | 已有 `timer_notify.py` | 依赖 webhook | 与 notify 一起启用 |
| 进程级心跳 | 无 | 进程卡死/事件循环停滞不可见 | 写 `heartbeat.json`，外部 watchdog 检查 |

---

## 3. 架构设计

### A1（P1）状态与依赖：从 God object 到 typed state

**现状**

- `StraTurtle` 2,347 行、102 方法、33 个薄转发；
- 4 个 execution 模块通过 `StrategyHost` 白名单访问 **71 个唯一属性**；
- `StrategyHost` 是“防呆”，不是“解耦”：模块仍能读写策略的全部可变状态，
  只是必须先声明名字；
- `_subs` 是 `list[dict]`，`dec` 是 `dict`，`_fill_pending`/`_exit_orders` 是
  `dict[str, dict]`，类型信息全靠约定。

**方案：typed state + 显式依赖**

```python
@dataclass
class EngineSlot:
    name: str
    units: float
    share: float
    engine: TurtleEngine

@dataclass
class SignalState:
    slots: list[EngineSlot]
    anchors: DataAnchors          # d1_date/m5_key/h1_key...
    recon_miss: int = 0

@dataclass
class ExecutionState:
    intents: dict[str, PositionIntent]
    exit_orders: dict[str, ExitOrder]
    fill_pending: dict[str, FillPending]
    deferred: dict[str, list[Decision]]
    outbox_seq: int = 0

@dataclass
class OpsState:
    day_guard: DayGuard
    limit_lock: dict[str, bool]
    veto_last: dict[str, float]
    ...
```

- `ExecutionRouter(state: ExecutionState, risk: RiskManager, ...)`；
- `FillTracker(state: ExecutionState, ledger: PositionLedger, ...)`；
- `StraTurtle` 只持有 `StrategyState` 和 wtpy context，不再暴露 71 个属性；
- 测试直接构造 state，不再 monkeypatch 策略私有字段。

**收益**：变更爆炸半径从“整条策略”缩到“某个 state + 某个模块”；快照序列化可以直接
`asdict`；类型检查可发现 key 拼写错误。

---

### A2（P1）执行路径：从多入口到单一 pipeline

**现状**

执行相关入口至少有：

```
on_calculate
  ├─ _on_calc_h1
  ├─ _on_calc_daily
  ├─ _on_calc_intrabar
  └─ _eval_core
on_tick
  ├─ _process_open_intents
  ├─ _process_exit_orders
  ├─ _catchup_eval
  ├─ _update_daily_guard
  ├─ _periodic_truth_check
  ├─ _reanchor_actual_fill
  └─ _exec_trigger_batch
on_session_end
  ├─ _session_eval_daily
  └─ _drop_stale_pending
```

历史上“某条路径漏挂某一步”已经发生多次（reanchor、catchup、roll）。

**方案：事件 + 阶段化 pipeline**

```python
class EventKind(Enum):
    BAR_CLOSED = "bar"
    TICK = "tick"
    SESSION_END = "session_end"
    CATCHUP = "catchup"
    TIMER = "timer"

@dataclass
class Event:
    kind: EventKind
    code: str
    as_of: int
    bars: Bars
    price: float | None
    source: str

class Pipeline:
    def run(self, event: Event):
        snap = self.data.snapshot(event)
        decisions = self.signals.evaluate(snap)
        decisions = self.filters.apply(decisions, snap)
        intents = self.sizing.plan(decisions, snap)
        intents = self.risk.review(intents, self.portfolio)
        self.execution.submit(intents)
        self.execution.confirm(self.ledger)
        self.state.persist_if_needed(event)
```

- 每个阶段只做一件事，输入/输出是 dataclass；
- 入口只负责“把事件翻译成 Event”，不再各自编排；
- 新增一个入口（例如定时再平衡）只需发一个新 Event；
- 每一步都有结构化日志，便于回放。

这是中期目标，不需要一次重写；先把 `_eval_core` 扩成 `evaluate + size` 两段，
再把执行/确认搬进 pipeline。

---

### A3（P1）持久化：从 ad-hoc dict 到 versioned snapshot

**现状**

- `_persist()` 手工拼 12 个键；`on_init()` 手工解析 264 行；
- 版本 v3/v4/v5 分支 + 兼容逻辑散落在 `on_init`；
- 没有周期快照、没有校验和、没有 run_id；
- 快照损坏时继续运行。

**方案：`SnapshotV6` dataclass + 迁移函数**

```python
@dataclass
class SnapshotV6:
    version: int = 6
    run_id: str = ""
    created_at: float = 0.0
    profiles: list[ProfileMeta] = field(default_factory=list)
    signals: dict[str, EngineSnapshot] = field(default_factory=dict)
    execution: ExecutionSnapshot = field(default_factory=ExecutionSnapshot)
    anchors: AnchorSnapshot = field(default_factory=AnchorSnapshot)
    reconcile: ReconcileSnapshot = field(default_factory=ReconcileSnapshot)

    def to_json(self) -> str: ...
    @classmethod
    def from_json(cls, raw: str) -> "SnapshotV6": ...
    @classmethod
    def migrate(cls, data: dict) -> "SnapshotV6": ...
```

- 每个子结构有自己的 `to_dict/from_dict`，字段增减只改一处；
- `migrate` 集中处理 v2→v3→v4→v5→v6，删除 `on_init` 里的分支；
- 加载时校验 `run_id`/`created_at`/checksum；损坏 → DEGRADED，不静默；
- 写快照用“临时文件 + 原子替换”（如果 `user_save_data` 不支持，至少在策略侧
  保留最近 3 份 JSON 备份）。

---

### A4（P1）研究与实盘：共享 core（与 B3 同一件事）

目标目录：

```
core/                     # 纯逻辑，零 wtpy
  model.py                # Decision/Intent/Fill/Position/Snapshot dataclass
  TurtleEngine.py         # 信号状态机（保留）
  sizing.py               # PositionSizer + equity scaling + liquidity cap
  risk/                   # RiskRule 链 + PortfolioLedger
  filters/                # 过滤链
  roll.py                 # 换月日历 + 复权
  StrategyCore.py         # evaluate/size 纯函数
runtime/
  pipeline.py             # Event → Decision → Intent
  state.py                # Snapshot/migrate
  reconcile.py            # Ledger vs engine vs broker
adapters/
  wtpy_strategy.py        # BaseCtaStrategy 壳
  wtpy_execution.py       # set_position/truth/fill evidence
  wtpy_data.py            # bars/ticks/session
research/
  sim.py                  # StrategyCore + 模拟撮合
```

`StraTurtle` 最终只保留：生命周期、订阅、图表、wtpy 适配、pipeline 调用。
`PortfolioSim` 只保留：数据加载、模拟撮合、组合记账。

---

### A5（P1）可观测性：结构化事件、指标、心跳

**现状**

- `stra_log_text` + `alerts.log` + 零散 print；
- 没有统一的事件 schema，事故复盘靠 grep 日志；
- 没有 metrics/心跳。

**方案**

1. **结构化事件 JSONL**（每策略每天一个文件）：
   ```json
   {"ts":"2026-11-09T09:00:01","code":"SHFE.rb.HOT","event":"arm",
    "n":52.3,"entry":3203,"exit":2968,"filters":["direction_gate"],
    "risk":{"equity":15230000,"used":420000,"cap":1500000}}
   ```
   事件类型：`arm/filter_veto/risk_veto/intent/submit/fill/confirm/exit/
   reconcile/state/alert`。
2. **指标**（Prometheus textfile 或 pushgateway）：
   `position_lots{code}`、`equity{strategy}`、`risk_used`、`reconcile_miss`、
   `tick_age_seconds`、`fill_latency_seconds`、`order_rejects_total`、
   `alerts_total{key}`。
3. **心跳**：每策略写 `heartbeat.json`（ts/last_tick/last_bar/last_eval）；
   外部 watchdog（systemd timer 或 runMonSvr 旁路）检查 session 内超时。
4. **日报**：每日 15:30 生成一份 Markdown/JSON，含持仓、权益、风险占用、
   当日信号/否决、成交滑点、对账状态；通过 webhook 推送。

这比“再加一个日志级别”更有价值：事故复盘、参数评估、客户/自己查看都用同一份数据。

---

### A6（P2）进程拓扑与故障隔离

**现状**：12 个策略 + 数据/交易通道在 1 个 `run.py` 进程；C++ 段错误/执行单元卡死
会带走全部策略，靠 WatchDog 10s 拉起。每日 08:47 / 20:40 两次计划内重启。

**权衡**

- 单进程：共享 `RiskManager` 简单、部署简单；但故障爆炸半径 = 12 品种。
- 多进程：故障隔离好；但共享风险账本需要 IPC/文件锁，复杂度高。

**建议**：暂不拆分。先把“计划内重启”做成有保护的操作（检查 open intents/exit orders、
重启后对账），并测量 MTTR。若未来要拆，按相关性组拆 2~3 个进程，风险账本用
“每进程本地账本 + 汇总文件”或一个轻量 risk server；不要在没做账本前拆。

---

### A7 目标架构图

```
                    ┌─────────────────────────────┐
                    │        Data Service          │
                    │  contracts/hots/roll/adj     │
                    │  d1/m5/h1 bars, ticks        │
                    └──────────────┬──────────────┘
                                   │
┌──────────────────────────────────▼──────────────────────────────────┐
│                         runtime/Pipeline                             │
│  Event(Bar/Tick/Session/Catchup) → Snapshot → Decisions → Intents    │
│       → RiskRules → Execution → FillEvidence → PositionLedger        │
└───────┬───────────────────┬───────────────────┬─────────────────────┘
        │                   │                   │
┌───────▼────────┐  ┌───────▼────────┐  ┌───────▼──────────────┐
│ domain/Signals │  │ domain/Risk    │  │ execution/Adapters    │
│ TurtleEngine   │  │ rules+ledger   │  │ wtpy set_position     │
│ filters        │  │ portfolio      │  │ truth/trades/fees     │
│ sizing         │  │ capital model  │  │ reconcile/heartbeat   │
└───────┬────────┘  └───────┬────────┘  └───────┬──────────────┘
        │                   │                   │
        └───────────────────┴───────────────────┘
                            │
                 ┌──────────▼──────────┐
                 │  Telemetry/State     │
                 │  JSONL + metrics     │
                 │  Snapshot v6         │
                 └─────────────────────┘

研究侧: research/sim.py = domain + Pipeline + 模拟撮合（不再重写门控）
```

---

## 4. 代码优雅：量化问题与具体重构

### C1 量化指标

| 指标 | 现状 | 目标 |
|---|---|---|
| 返回注解覆盖 | 40% | 80%+（core/ 先到 90%） |
| `except Exception` 静默 | 11 处 | 0（必须日志/告警） |
| ≥150 行函数 | 5 个 | 0（拆到 <80 行） |
| 薄转发 | 33 个 | 0（迁移调用方后删除） |
| `StrategyHost` 依赖 | 71 个 | 0（typed state 注入） |
| `print` | 7 处 | 0（统一日志） |
| 测试私有方法 | 较多 | 以公开契约/纯函数为主 |
| golden 覆盖 | rb 单品种 1 条 | rb/IF/sc × close/trigger × 2 资金档 |

### C2 十个具体重构

**C2-1 用 dataclass 替换决策/子系统/成交字典**

```python
# 现在
dec = {"decision": "enter", "direction": 1, "price": 3200.0, "plan": {...}}
if dec.get("decision") == "enter": ...

# 目标
@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    direction: int
    price: float
    plan: dict = field(default_factory=dict)
    reason: str = ""
```

`TurtleEngine.on_bar` 返回 `list[Decision]`；JSON 序列化用 `asdict`；测试用属性断言。
这一步会触及引擎/快照/所有调用方，建议单独一个 PR，黄金基线验证。

**C2-2 把 `cap_entry` 拆成 RiskRule 链**

```python
@dataclass
class RiskRequest:
    code: str; profile: str; kind: str
    lots: int; n: float; price: float; volscale: float
    stop_mult: float; equity: float; share: float
    group: str; units: float; ...

@dataclass
class RiskResult:
    lots: int
    reasons: list[str]

class RiskRule(Protocol):
    def apply(self, req: RiskRequest, book: BookView) -> RiskResult: ...
```

规则顺序：`invalid → per_trade → per_market_cum → portfolio_total → margin →
units → account_available → daily_loss`。每条 15~30 行，单测直接构造 `RiskRequest`，
不再需要 17 个关键字参数。

**C2-3 Snapshot v6 + migrate**

见 A3。先写 `SnapshotV6.from_v5()`，用现有快照做 golden 往返测试，再逐步替换。

**C2-4 删除 33 个薄转发**

步骤：
1. `grep -rn "self\._truth_position\|self\._execute\|..."` 找调用方；
2. 调用方改为 `self._truthmon.truth_position(...)` / `self._router.execute(...)`；
3. 测试改为模块 API；
4. 删除转发；`python -m pytest` + golden。

**C2-5 拆分 5 个超长函数**

- `on_init`：按“模式/订阅/图表/恢复/对账/预热”六段抽私有方法，主函数 <80 行；
- `ConfigLoader.load`：`_parse_base/_parse_risk/_parse_instruments/_cross_check`；
- `execute_dual`：`_plan_dual/_apply_risk/_submit_batch/_confirm_batch`；
- `cap_entry`：C2-2；
- `_process_open_intents`：`_classify_intent/_confirm_one/_expire_one`。

**C2-6 统一异常与静默处理**

```python
class TurtleError(Exception): ...
class DataUnavailable(TurtleError): ...
class StateCorrupt(TurtleError): ...
class ExecutionUnknown(TurtleError): ...

def safe_call(label, fn, default=None, alert=None):
    try:
        return fn()
    except Exception as e:
        log.warning("%s failed: %s", label, e, exc_info=True)
        if alert: alert(label, str(e))
        return default
```

CI 加 `ruff` 的 `BLE001`（blind except）或自定义 grep；禁止 `except Exception: pass`。

**C2-7 枚举替代魔法字符串**

`DecisionKind`、`AlertKey`、`EntryMode`、`StopNMode`、`TruthMode`、
`AccountGateMode`。`schemas.py` 的 `validate` 用枚举校验，配置错误在加载期 fail-loud。

**C2-8 类型检查**

- `from __future__ import annotations` 全量；
- `mypy --ignore-missing-imports core/ execution/` 先跑起来，逐步收紧；
- CI 加 `mypy`（允许 baseline，新增错误阻断）。

**C2-9 配置可读性**

- 保留单文件，但加 `ConfigLoader.describe()`：
  打印“有效配置 + 派生值 + 风险预算链 + 警告”；
- 交叉校验从 WARN 升级为 ERROR（在 `--strict` 模式），生产启动用 strict；
- 删除 deprecated `system`/`speeds` 的代码路径（配置别名可保留一个版本，
  但代码只走 `profiles`）。

**C2-10 注释与文档分层**

- 代码注释只留“不变量/口径/为什么”；
- 历史事故叙事移到 `docs/`（已有 AGENTS/DATA_OPS），代码里用一行引用；
- 每个模块顶部写“职责/输入/输出/依赖”，不再写 30 行背景故事。
  这能让 `StraTurtle.py` 至少减 10%~15% 行数，且不影响可读性。

---

## 5. 路线图

### Phase 0（P0，约 1 周）：先让系统“可观测、可刹车”

| 动作 | 产出 | 验收 |
|---|---|---|
| 启用 notify（需 webhook URL） | 告警到企微/钉钉 | 自测脚本收到测试消息 |
| tick 静默检测 + 心跳文件 | 每策略 `last_tick_ts` | 断流 5 分钟告警；静默时禁止新开仓 |
| 快照损坏 → DEGRADED | 不静默继续 | 故障注入：坏 JSON 不新开仓、告警 |
| 周期快照 + 保留 3 份 | 崩溃窗口缩小 | 重启恢复测试 |
| 止损 fill_timeout 硬编码 600s → 按紧急度分档可配置 | 更早告警 | 代码改造 + 单测（联动 fees 2x 放行/ghost 状态机） |
| `--doctor` CLI | 查看快照/信号/真值/账本差异 | 生产只读运行 |

> 这一阶段不改策略行为，黄金基线必须零漂移。

### Phase 1（P0，约 2~3 周）：研究口径修复

| 动作 | 产出 | 验收 |
|---|---|---|
| 全历史换月日历（统计法先行，逐合约精化后置） | `hots.json` 全量规则 | `roll_calendar_products=12` |
| HOT 复权管道 | `*_adj` 序列 + 校验 | 最大跳空 ≤ 涨跌停 |
| 资本模型 v2 | `capital` 配置段 + `CapitalModel` | 三处 unit_var 一致 |
| rb 实盘/研究 parity 测试 | CI 常驻 | 决策序列一致 |
| 重跑黄金/矩阵/OOS/容量并**重验参数决策** | 新结果 + 旧结果标注 + arm_exclude/S1+S2/过滤器结论复权后重验 | 差异可解释 |

### Phase 2（P1，约 3~4 周）：领域核心抽取

| 动作 | 产出 | 验收 |
|---|---|---|
| `core/model.py` dataclass | Decision/Intent/Fill/Position | 引擎/快照迁移 |
| `core/risk/` RiskRule 链 | 7 条规则 | `cap_entry` 行为逐位一致 |
| `runtime/state.py` Snapshot v6 | 迁移 + 校验 | v5→v6 往返测试 |
| `runtime/pipeline.py` | Event→Intent | 所有入口走同一 pipeline |
| `PositionLedger` | 事件溯源账本 | 事故回放通过 |
| 删除薄转发/typed state | StraTurtle < 1,500 行 | 黄金零漂移 |

### Phase 3（P1/P2，约 2~3 周）：执行与组合

| 动作 | 产出 | 验收 |
|---|---|---|
| 执行紧急度分级 | `urgency` 字段 | 止损 p95 < 120s |
| 参与率/容量上限 | `liquidity` 规则 | 40% 容量回测 |
| 动态 unit_var | 复利/回撤 A/B | 生产 shadow 2 周 |
| 组风险预算 | `group_risk` | 相关性组不超预算 |
| 保证金分品种 | `margins.json` | 预估 vs 实际偏差 < 20% |

### Phase 4（P2，持续）：工程与运维

- mypy/ruff BLE001/覆盖率 80%；
- 结构化事件 + Prometheus + 日报；
- 进程拓扑评估（非必须）；
- 文档收敛：ARCH/OPERATIONS/STRATEGY 三本，历史进附录。

---

## 6. 需要用户拍板的 5 个问题

1. **真实可支配资金是多少？** 25.3M 账户里，这个策略 sleeve 是 15M、25M 还是其他？
   `portfolio_capital` 是否启用？这决定 B1 的全部数值。
2. **旧回测结果如何处理？** 复权后重跑，旧结果标注作废/并存？这决定 B2 的验收口径。
3. **是否接受“先 shadow 再切换”的资金模型？** 还是直接按新模型生产？
4. **是否投入 3~4 周做 StrategyCore 抽取？** 还是先做 Phase 0/1 的止血和口径修复？
5. **notify webhook 谁来提供？** 企业微信/钉钉/generic URL；没有它，Phase 0 的
   安全网无法闭环。

---

## 7. 附录：证据与复现

### 7.1 代码规模

```bash
cd demos/cta_turtle
find . -name "*.py" -not -path "./deploy/*" -not -path "./generated/*" \
  -not -path "./Logs/*" -not -path "./__pycache__/*" | xargs wc -l | tail -1
grep -c "    def " StraTurtle.py
```

### 7.2 资金口径

```bash
python3 - <<'EOF'
from core.ConfigLoader import ConfigLoader, risk_coherence_warning
cfg = ConfigLoader.load('config/turtle_config.yaml', warn=False)
print(risk_coherence_warning(cfg.base, cfg.risk))
EOF
```

### 7.3 换月证据

```bash
python3 - <<'EOF'
import csv, statistics
for f in ['../../storage/csv/INE.sc.HOT_d.csv',
          '../../storage/csv/DCE.m.HOT_d.csv']:
    rows = list(csv.DictReader(open(f)))
    gaps = []
    for i in range(1, len(rows)):
        c0 = float(rows[i-1]['close']); o1 = float(rows[i]['open'])
        gaps.append((abs(o1-c0)/c0, rows[i]['date'], c0, o1))
    gaps.sort(reverse=True)
    print(f, 'median %.3f%%' % (statistics.median(g[0] for g in gaps)*100))
    for g in gaps[:5]:
        print('  %.2f%% %s %.2f->%.2f' % (g[0]*100, g[1], g[2], g[3]))
EOF
```

### 7.4 研究产物

```bash
grep -n "roll_calendar_products" matrix_results/portfolio_oos_phase2.json
grep -n "capital" matrix_results/portfolio_bt.json | head
```

### 7.5 生产账户

```bash
ssh ubuntu@129.211.5.54 \
  'python3 -c "import json;print(json.load(open(\".../rtdata.json\"))[\"funds\"])"'
```

---

## 8. 一句话总结

**信号逻辑已经足够好，下一步的收益不在“再优化海龟参数”，而在把资金、数据、执行
三个口径做实，并把“实盘/研究两套实现”收敛成一套可对拍的核心。** 先做 Phase 0/1，
再谈核心抽取；每一阶段都用黄金基线 + parity 测试 + 生产 shadow 验证，不赌大重构。
