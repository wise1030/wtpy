# P0 第一批实施记录（2026-10-26）

> 范围：按项目审阅路线图实施的首批安全改造。本文只声明本批完成情况，
> 不代表 trigger/close 研究语义、组合回测、资金分配等后续阶段已完成。
> 未部署生产、未启用新收益参数、未主动刷新绩效基线。

## 1. 实盘出场：目标仓不等于成交仓

### 已实现

- `TurtleEngine` 新增可持久化 `_exit_pending`：出场等待期间保留 direction、
  layers、S1 与止损管理状态，on_bar/on_price/check_bar 不重复产出决策。
- close 的决策阶段 S1 预结清可在异步出场开始时恢复，真实成交后按实际价结清；
  同 bar `[exit,skip]` 的即时确认路径不再误清新 sim 的 fail-safe。
- 实盘单 profile 路由、盘中止损、日损全平统一先登记出场意图/成交游标，
  再提交目标仓；回测仍走即时成交假设，不改变既有填充口径。
- `FillTracker` 按平仓方向匹配本通道成交：平多为 CL/CNL 卖出，平空为
  CS/CNS 买入。全部成交后才清引擎与风险；部分成交只更新进度；超时只告警。
- `exit_orders` 与引擎状态共同进入 v4 快照，重启后继续等待，不提前重入。

### 明确边界

- 多 profile 的净额出场和逐 profile 成交归因仍未统一，保留为激活前阻断项；
  本批不为该路径宣称完成。
- 部分成交期间保守保留全部风险，不根据不完整证据猜测剩余层价。
- 尚未接入券商/柜台原生条件单。

## 2. 共享账户成交与 ghost 安全

- `trades.csv` 动作口径修正：CNL/CNS 是平今真实成交，不是撤单。
- enter/add 的证据增加 open + 买卖方向过滤，不再混入同品种平仓成交。
- `truth_rebuild_mode=rebuild` 才允许账户净增量确认、真值零仓出场确认和
  ghost 自动裁剪，语义仅限显式独占账户。
- 默认共享 `alert` 模式不使用账户净增量/绝对净仓做本策略自动确认或裁剪；
  `ghost_auto_trim=true` 在 alert 下也只告警并转人工。
- 真值存在时，不再用信号口径 fees 或墙钟超时替代真实成交；无真值环境才保留
  原有 fees/超时降级。

## 3. 主力与 HOT 数据 fail-closed

- 主力选择只接受有限且严格为正的 OI；缺行情、NaN、全零不再回退到首候选。
- `update_hots` 全部无有效结果时非零退出、告警、不写盘、不重启；部分有效时
  仅更新有效品种，其余保留旧映射。手工 `pick_hot_sina` 同样拒绝全无效结果。
- `update_hot_daily` 对缺映射、缺文件、空数据、读取/合并/DSB 重建异常逐项汇总，
  notify 并让 CLI 非零退出；成功读取且已最新的零增量不误报失败。

## 4. strict 预检 fail-closed

- 手工 `runLive` 与部署 `deploy_tpl/run.py` 统一 off/warn/strict 解析和失败策略。
- 非法 `WT_PREFLIGHT` 值直接拒绝；strict 下导入、回补、check_config、
  check_staleness 等任一异常都阻断；SystemExit 不再被吞掉。
- warn 是显式降级并输出醒目警告，off 才是跳过检查。

## 5. 验证证据

- 全量测试：**518 passed**（52 个测试文件）。
- Ruff：`All checks passed`。
- 黄金回测：`OK: 端到端一致`，rb 指标保持
  `33.3729652% / DD 18.5822549% / Sharpe 0.2386128 / 1275 days`。
- 新增/更新覆盖：
  - exit 未成交、部分成交、超时、重入封锁、盘中/日损路径、快照恢复；
  - close `[exit,skip]` S1/fail-safe；
  - CNL/CNS、open/close 与买卖方向、共享 alert 禁用自动确认/裁剪；
  - 主力全空/全零/部分有效、日更失败与正常零增量；
  - 手工和部署两个入口的 strict/warn/非法模式。

## 6. 后续阶段（不在本批完成声明内）

1. 统一 trigger 通道索引、exclude/envelope、事件 mark 与 close/h1 时序；
2. 组合研究的换月、费用、共同窗口、日期对齐与可复现 manifest；
3. 加仓 post-risk、组合权益分母、部署/手工统一资金装配；
4. 多 profile 净额执行与分账、h1 生产前评估；
5. 配置真实 webhook 后启用外部告警并做故障注入（本批未填伪地址）。
