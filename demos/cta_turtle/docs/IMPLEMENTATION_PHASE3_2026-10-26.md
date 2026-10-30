# 第三阶段实施记录（2026-10-26）

> 范围：多 profile 统一执行、换月指标连续性、组合 OOS/容量与告警自检。
> 未部署生产，未刷新黄金基线，未根据 OOS 调整生产参数。

## 1. 多 profile ExecutionBatch

- 新增 `execution/batch.py` 的 `TargetIntent`：记录 before/after/delta/side/
  offset，跨零反向标记 mixed，快照可恢复。
- trigger 同一 tick/bar 先收集全部 profile 决策，再调用一次净额执行，
  消除中间目标单。
- 多 profile 出场以整个批次的净变化确认，不再要求每个 profile 分别出现
  足额账户增量。批次只处理 exit，混合 enter/add 延至下一事件。
- 全部平仓成交到达后才分别 confirm 各子系统；部分成交、超时保持风险与
  重入封锁。净零可结清，跨零反向保持 UNKNOWN。
- 多 profile 不再使用账户净增量证明成交；反向净单没有可归属证据时保持
  UNKNOWN，不以超时假确认。风险账明确 gross 层风险、net 执行目标。

回归覆盖：同 tick 一次净单、S1 部分退出时 S2 风险保留、净成交后结算、
快照 intent 恢复、跨零/净零 TargetIntent。

## 2. 换月指标连续性

- `build_channels` 增加逐 bar 换月窗口调整：当前 i 只应用 i 前已发生的
  换月价差，绝不把未来换月用于过去。
- ATR/Donchian 与 `_apply_rolls` 的层价/估值锚处于同一坐标；执行、费用、
  保证金仍使用原始价。缺 old/newclose 时仅换月日用 open-prev_close 估算并告警。
- 合成测试证明拼接跳空不再放大 ATR，且换月前数据不受未来规则影响。
- 当前本地 hots 无历史规则时仍显式标记 `roll_calendar_products=0`，不宣称
  实际历史组合已完成换月验证。

## 3. 组合 OOS 与容量验证

新增 `research/run_portfolio_oos.py`：固定配置分别运行训练/验证窗口，
close/trigger × 0/1/2 tick，记录收益、回撤、Sharpe、费用、保证金峰值、
超限天数、换月覆盖与统一 manifest，不做网格寻优。

本次固定场景（2019-2020 train / 2021-2022 test，60M，slip=1）：

| 场景 | 收益 | 最大回撤 | Sharpe | 容量/覆盖说明 |
|---|---:|---:|---:|---|
| close test | -5.86% | 39.82% | 0.150 | 仅研究参考 |
| trigger test | +38.37% | 31.92% | 0.594 | 10 个品种仍缺 m5，roll 日历未覆盖 |

容量对照：

- trigger train、margin_usage=0.6：保证金峰值 48.03M，超限 2 天；
- 同场景 margin_usage=0.4：峰值降至 32.93M，但超阈值增至 31 天。
  说明降低阈值会同时收紧判超线，不能当成无成本解决容量问题。
- test 在 0.6 下无超限，0.4 下仍有 5 天，容量约束不能只看收益指标。

产出：`matrix_results/portfolio_oos_phase2.json` 与
`matrix_results/portfolio_oos_capacity40.json`。均非新生产基准。

## 4. 告警自检

- 新增 `scripts/notify_selftest.py` 并纳入部署：真实推送成功才返回 0，
  未启用/推送失败返回非零。
- 当前 notify 未配置，实测 `notify_selftest_rc=2`，符合预期；未伪造 webhook。

## 5. 验证与剩余边界

- **549 passed**（57 个测试文件）；Ruff 全部通过。
- 多 profile 的反向净单仍需订单级 userTag/归属证据才能自动确认；目前安全
  保持 UNKNOWN，不宣称已经解决所有反向成交归因。
- 完整历史换月日历仍需补齐，之后才能对真实组合做全窗口换月验证。
- 真实 webhook URL、生产故障注入、组合更长期 OOS 与实盘容量复核仍待完成。

## 6. 阻断项修复（2026-10-29）

- 修复多 profile 批次快照恢复后兼容补录单 profile 出场记录的问题；
  恢复后只保留 `__dual__` 批次，成交后 S2 剩余风险不再被误清。
- mixed enter/add 从“日志声称延后、实际取消”改为持久化延迟队列：
  保留引擎 pending 防重复触发，出场真实成交后重放；出场提交异常取消时
  下一无出场事件恢复；跨自然日延迟决策过期并清 pending。
- 多 profile 盘中止损、日损全平统一走异步 `TargetIntent` 批次，不再
  目标 0 即 confirm。
- 组合研究换月日 `arm_exclude_signal=true` 改用已平移新坐标的
  `chan[i]`；空仓 S1 模拟单同样随换月 re-anchor。
- 实盘 session_end 缺少当日 m5 时不再用 his 末根冒充当日评估推进
  `_d1_date`；保持锚点，待数据恢复后补评估。
- `TradeJournal` 业务游标升级为“文件身份+首笔成交身份+行号”；
  trades.csv 新建或同路径原地截断后旧游标均失效保持 UNKNOWN，不再跳过/
  误配新文件成交；半行写入不推进游标。
- trigger S1 模拟单通道出场按实际 tick/open 成交价判定输赢，跳空不再
  被理想通道位误判为盈利。

验证：**559 passed**；Ruff 全部通过。多 profile 生产激活仍待组合回测、
profile 分账和反向订单归因完成。

## 7. 第二优先级：统一执行账本与多 profile 组合模拟（2026-10-29）

- `RiskManager` 账本键升级为 `(code, profile)`：价格/风险不再跨子系统
  vwap 合并；`on_closed(code)` 仍清整 code，`on_closed(code, profile)`
  支持部分出场。单位数/相关性组由 code 级 `_code_meta` 去重，多
  profile 不双计。
- 单品种累计与保证金改为**本 profile share 预算、扣本 profile 占用**；
  组合总敞口保持全局，`post_risk_base` 与新增
  `post_profile_risk_base` 分别服务 code 全局与 profile 累计口径。
- `PortfolioSim` 支持 `S1+S2/speeds`：每 profile 独立引擎/通道/开仓日，
  共享组合 RiskManager；confirm 后立即预算预约，broker net 目标只在
  close 日批/m5 事件批末结算一次；费用按 profile gross（保守上界），
  summary 记录 `orders/book_mode/fee_mode/multi_profile_products`。
- 实盘 enter/add 接入 write-ahead outbox：目标意图先于
  `stra_set_position` 持久化，成交证据到达后才确认引擎；恢复时
  before 状态只重放绝对目标，目标状态也必须经逐笔/真值证据，UNKNOWN
  持续封锁且不假确认；跨日 intent 过期清理。
- 多 profile `execute_dual` 保持循环内 confirm 以维持批次内预算预约，
  同时先落 write-ahead 记录；批末一次净目标提交后关闭意图。

验证：**571 passed**；Ruff 全部通过。仍需 C++ `userTag` 才能解决反向
net/gross 的订单级归属；生产激活前还需故障注入与真实 webhook/OOS 复核。

## 8. 订单级归因链落地 + 激活阻断项清零（2026-10-30）

**归因链（零 C++ 改动，替代 userTag，设计见
docs/ORDER_ATTRIBUTION_NO_CPP.md）**：
- 新增 `core/OrderAttributor.py`（纯逻辑）：执行单元多空簿镜像 +
  净变化→订单签名预测；意图 FIFO 队列 × (侧车 TRD_ORDER/TRADE +
  trades.csv 权威兜底) 双源增量事件 → localid 绑定 → per-intent
  成交进度与精确 vwap。绑定不变锚 = 提交序==建单序（信号 FIFO +
  每策略独立执行器）；簿镜像 gross 拆分漂移由**成交超量时惰性反推**
  （不在绑定时猜，外部订单不触发簿重写）；守恒破坏（无法匹配成交）
  → `attribution_mismatch` 告警 + 保持 UNKNOWN。
- StraTurtle/fill_tracker 接线：enter/add/exit/dual 全意图注册进归因
  队列（与 outbox 同一持久化时点）；确认消费优先级 orders 证据 >
  真值/逐笔时序/fees；**跨零 mixed 批次由签名逐一认领自动消解**，
  不再一律 UNKNOWN（此前 `dual_exit_mixed` 封锁路径保留为兜底）；
  纯出场场景惰性构建并重放持久化意图。
- 幂等/恢复语义：`on_intent` 同 iid 同 (before,after) 重复登记安全；
  `complete()` 出队不回滚簿镜像（持仓确实到了 after），`discard()`
  撤销/过期回滚到 before。

**激活阻断项清零结论**：
- 反向 net/gross 归因：Python 层解决，C++ userTag 降级为"实证错误
  率不可接受时的加固项"（唯一独占价值 = 同 code 同向同手数同 tick
  双意图 R4，缓解 = 批内错开提交或 UNKNOWN）。
- 故障注入：16 例新测试覆盖场景 A/C/R1/R3/守恒破坏/簿漂移/双源
  去重/半行/轮转 + 进程内 enter/exit/mixed 集成；测试 571→**587**。
- S1+S2 vs S1 组合 A/B（12 品种 close/60M/1tick/2019-01~2026-08，
  产物 matrix_results/dual_ab.json）：**负结果** —— S1+S2 约束后
  +6.2%/DD55.6%/Sharpe0.215（rejected 1606 笔、保证金超限 69 天）
  vs 纯 S1 +75.3%/DD29.4%/Sharpe0.408；无约束同收益但回撤深 23pp。
  双系统双倍速消耗品种累计(8%)/保证金(10%)预算被约束勒死。
  **生产维持单 profile S1**；多 profile 能力保留，未来启用需先放宽
  累计/保证金预算并重测。
- 黄金基线刷新：10-26 武装口径修复（旧 prev_*+包络错一根）与
  10-29 引擎修复使 rb 基线 33.37%→9.73%（去掉陈旧锚的类前视优势，
  为诚实口径）；已显式 --update 并在此声明。归因链为实盘路径,
  回测行为不变由旧基线信号哈希佐证流程已执行。
- 剩余（非代码项）：notify webhook URL（待用户提供）；归因链生产
  观测窗（侧车已在产，实证门禁随部署自动开始累积）。
