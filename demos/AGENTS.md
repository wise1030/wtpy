- [x] **剩余路线清零（2026-10-21）**：①**trigger/m5 组合回测**——
  `run_portfolio_bt --entry-mode trigger`：日线只武装（含当根包络,
  session_end(T-1) 口径），盘中 m5 走引擎 check_bar 触碰仿真（跳空按
  开盘价, 止损→出场→加仓→入场, 每 bar 至多一决策），无 m5 退化 d1
  触碰；过滤器武装时前置裁决/风控触发时复核/名额按 m5 first-come，与
  实盘 trigger 管线逐一对应；产出 `_trigger` 后缀不覆盖 close 工件。
  参考成绩（60M/1tick, 本地仅 rb/IF 有 m5）：约束 +85.97%/DD38.77%/
  Sharpe0.448 (2589笔) vs close +77.07%/29.17%/0.414。**实现 bug 实录**：
  `confirm is e.confirm_enter` 绑定方法判等在 CPython 下恒 False →
  参数错位仓位爆炸，改显式 kind 分派。②**执行层订单归因实验**——源码
  核验 CTA 层无订单/成交回调（订单状态机仅 HFT 层暴露），但 C++
  EventNotifier 广播 TRD_ORDER/TRADE（含 localid）；落地
  helpers/ExecJournal.py（OrderTracker 状态机+去重 / ExecAttributor
  滑点·成交率·耗时归因, 7 例单测）+ scripts/exec_journal.py 旁路侧车
  （订阅 notifier 总线落盘 JSONL, 零侵入）。③**h1 显式拒绝**——
  ConfigLoader 对 period:h1 直接报错（数据管道/session 合成/回测均未
  按 h1 验证），要支持须先补管道。④**删除陈旧 deploy/ 快照**——本地
  9-20 make_deploy 产物已删（生产在服务器独立目录, make_deploy 一键
  重建）。测试 426→**438** 全绿、ruff 通过。剩余：notify 启用
  （待 webhook URL）、执行归因侧车生产接入观察。
- [x] **目录治理 + h1 小时级一期（2026-10-21）**：①**改名 helpers/→core/**
（被 import 的策略核心库；scripts/=独立可执行运维工具语义不变），全仓
import/部署模板(make_deploy CODE_DIRS)/CI(cov)/ruff/文档同步；②**research/
归拢**——run_walkforward/run_slippage_sens/run_speed_study/run_portfolio/
run_portfolio_bt + wf_report/slippage_report 七件研究工具入 research/
（根目录只留核心工作流入口），跨目录路径全部改 ROOT 锚定；③**h1 一期**
——WtDt 不写 h1 且 his 重建对运行进程不可见，故策略层 m5→h1 聚合
（core/aggregate.py 纯函数：起始钟点桶+夜盘交易日历映射+尾部桶
session_end flush；norm_m5_time 三态归一 WT 打包时间 (年-1990)*1e8+
月*1e6+日*1e4+HHMM——bt DSB 实证 2904082125）；StraTurtle 主K=m5 逐根
推进、桶闭合走 _eval_core 统一骨架、trigger bt 用 check_bar 与 live tick
时序等价、锚点 _h1_key+_h1_m5_key 持久化防重放；ConfigLoader 放开 h1
（原显式拒绝移除）、runBT 预检路由 m1→m5。**调试实录**：bt CTA mocker
按主K bar 驱动 on_calculate（d1 每日一次），初版"每 call 只评最新桶"
在 bt 退化为日频——改"bt 依序全评+逐 bar 触碰仿真 / live 只评最新+
sweep 兜底"双模；闭合检测用桶末根 bar 映射而非 bar 级 closed_by
（基线 bar 闭合桶的边界）。rb 实证（2019-06~12，5M，trigger，1tick）：
h1 +2.95%/DD8.84%/Sharpe0.23/126笔 vs d1 +11.29%/5.82%/1.59（换手4倍、
单笔期望低，合速度研究）。测试 438→**449**（+test_h1 11：桶口径/夜盘
映射/午休归并/OHLCV/键单调/时间归一/配置放行/策略级 cadence/快照往返/
close 突破入场/trigger 武装）。剩余：live h1 部署前 rt m5 日历口径生产
验证、run_portfolio_bt --period h1（二期）、聚合增量 feed 优化。
- [x] **生产部署 10-20/10-21 批次（2026-10-21 15:33）**：服务器自 10-09 版
  跳级到最新（含 core/ 改名/research 归拢/h1 一期/trigger 组合回测/
  累计风险闸/真值隔离/日损闸/fallback summary/P2×5/执行归因）。
  部署链：make_deploy 本地构建（排除 config.yaml/dtcfg.yaml——含本地
  绝对路径，服务器保留自有版本）→ rsync --delete（排除凭据 mdparsers/
  tdtraders + 运行时 generated/Logs/DtLogs/logs/Strategy/Trader/
  CTPMDFlow/CTPTDFlow）→ 服务器 helpers/ 由 --delete 自动清除 →
  逐文件 md5 校验 7/7 一致 → config.yaml sed 补丁 riskmon
  base_amount 5M→15M、risk_scale 1.0→0.5（C++ SimpleRiskMon 日志
  实测 "Capital: 15000000, Risk Scale: 0.50" 生效）→ kill -9 run.py
  WatchDog 15s 拉起。验收：12 策略 inited、真值 age≤1s、四持仓账本
  逐字恢复（rb 408@3057.07 / p 72@9734.26 / CF 159@16047.45 /
  IF 9@4149.11）、执行单元预热 target 与持仓一致、**零重启成交**、
  0 Traceback、ConfigLoader 口径 WARN 正常打印（75% 名义单位）。
  备份 ~/wt_turtle_backup_20261021_1538.tgz（38MB，回滚 =
  解压+kill -9 run.py）。夜盘（21:00 起）起新代码全路径生效，
  首夜观察点：session_end 合成评估日志格式、hotdaily 20:40 重启、
  定时器脚本（update_hots/update_hot_daily 已同步新 core 路径）。
- [x] **优化方案执行第一批（2026-10-22，依据 OPTIMIZATION_PLAN_2026-10-22）**：
  复核后执行，修正方案三处（unit_var 标定随 portfolio_capital 联动、
  exec_journal 漏部署清单、现货品种无夜盘）。①**S1 资金分配机制落地**：
  `risk.portfolio_capital`（默认 0=关闭，生产行为不变）+ `alloc_weights`
  （{code:权重}，缺省等权，未知 code 拒绝）；`ConfigLoader.allocated_capital()`
  纯函数；`RiskManager.set_sid_capital/capital_base`——runLive 共享账本下
  **单位/总敞口闸共享、风险/保证金比例闸按 sid 各自分配额**（from_config
  分配解析 → replace(fallback_capital=alloc)，一致性 WARN 同口径；--capital
  覆盖语义：分配开=组合总资金口径，关=旧 fallback 语义）；调试实录：
  误插第二个 `__init__` 被 Python 后者覆盖前者，发现并合并。②**R1 黄金
  回测**：`research/golden_bt.py`——rb d1 trigger 2018-01~2023-04/5M/0tick
  固定窗口，对 sha256(signals+funds+trades)+summary 关键指标精确比对，
  两次运行逐位一致（35.58%/DD20.66%/0.258/1275 天）已写基线
  `golden/rb_d1_trigger_baseline.json`；漂移即 exit 1，口径变更需
  --update+PR 声明；4 例纯逻辑单测。③**A5-1 执行侧车生产接入**：
  make_deploy 补 exec_journal.py（原 TPL_FILES 白名单漏掉）+ systemd_tpl
  整目录同步；新增 `wt-turtle-execj.service`（Restart=always，ExecStartPre
  sleep5）；生产已部署启动（23:13 active，订阅 ipc:///tmp/wt_turtle.ipc，
  首批事件待订单流；ExecStartPre 误放 [Unit] 段已修正重装）。
  测试 449→**462**（+test_allocation 9 / test_golden_bt 4）全绿、ruff 通过。
  剩余：A1 拆分（黄金网已就位，可开始）/S2 过滤器 A/B/S3 h1 组合二期/
  S4 口径 A/B/A5-2 C++ 改造（先积累侧车基线）。
- [x] **A1 StraTurtle 四步拆分完成（2026-10-22 夜）**：2730→**1742 行**
  （-36%）。新增 `execution/` 包（注意: `exec` 是 Python 关键字不能作
  包名）：router.py（门控链+单/双系统执行路由+武装裁决, 382 行）/
  fill_tracker.py（成交证据链+均价回写+幽灵 trim+执行单元自愈, 384）/
  truth_monitor.py（真值源+对账+资金闸, 252）/ telemetry.py（告警落盘+
  否决冷却+fund_live+journal, 168）/ paths.py（GROUP_DIR 单源）。
  **手法**：ast 提取方法体 → self.X→self.s.X 机械重写 → 类持策略回指，
  StraTurtle 保留同名薄转发（测试/调用方接口零变化）；每步过后
  462 测试+ruff+黄金回测端到端哈希三重验证。**四次都是"OK: 端到端一致"
  逐位不变**。调试实录（给后续代理的坑位）：①`git checkout <file>` 把
  未提交的 h1/S1 改动整文件回滚——靠 deploy/cta_turtle 快照+补丁重放
  恢复，**教训：大改动前先 cp 备份或 commit**；②模块级全局 `_GROUP_DIR`
  随方法迁移失联（NameError 被 try/except 吞掉 → trade_journal_source
  静默返回 None —— 正是复核报告"异常静默"条目的活证）→ 收敛到
  execution/paths.py 单源；③`import os` 漏迁移同样被静默吞掉
  （fill 证据链降级）→ 迁移后对模块做 import×usage 审计；
  ④monkeypatch 打 `StraTurtle._GROUP_DIR` 的测试要同步补打
  `execution.telemetry._GROUP_DIR`。make_deploy CODE_DIRS 已含
  execution/（部署包 58 文件）。剩余 A1 尾项：回指逐步收敛为显式依赖
  注入（当前 self.s 过渡态）；A2 ExecutionModel/A3 引擎镜像合并未动。
- [x] **A1 尾项 + S3 h1 组合二期 + S4 口径 A/B（2026-10-24）**：①**A1 尾项**
  `execution/host.py` StrategyHost——四模块不再持策略回指, 改持白名单代理
  (类级 DEPS 显式声明全部读写依赖, 未声明访问立即 AttributeError loud-
  failure, 读写落同一策略属性零漂移; test_caliber 的 DEPS×usage 审计测试
  是回归网); ②**S3** `run_portfolio_bt --period h1`（m5→h1 聚合复用
  core/aggregate.py + 日界保证金标记 + trigger 逐 bar 武装 catch-up）+
  `--codes` 子集 + `run_h1_mix.py`（半资金混合+日收益相关+门槛判定）+
  本地 IF m5 MetaTrader 格式头修复（.bak_20261024 备份）; ③**S4** 四口径
  配置化（base 段 trigger_envelope/allow_same_bar_reversal/gap_stop/
  arm_exclude_signal, 默认全旧行为）+ `run_caliber_ab.py` 聚合。实证
  （12 品种/60M/strength, caliber_ab.json）：**envelope 恒等证伪**（通道
  窗口已含武装 bar, 与 channel_only 逐位相同）; **arm_exclude_signal
  （严格经典武装=突破前 N 日通道）约束 +139.94%/Sharpe0.572（+53.9pp/
  +0.124, 无约束同向 +206.3%/0.637）——**用户已拍板启用, 默认翻 True**
  (黄金基线声明式刷新: rb 5M 35.58%→33.37%/0.258→0.239, 单品种略降但
  组合截面显著改善, 组合口径为决策依据）; 同 bar 反转贡献 +11.6pp（默认保持 True）; close 引擎
  跳空止损诚实口径使基线 -11.8pp（"close 诚实差于 trigger"量化）。
  S3 实证门槛**未通过**：d1/h1 日收益相关≈0（分散化理论成立）但 h1 速度
  独立期望深负（半资金 -99.9%, 磨损/假突破）, 混合被拖死 → **h1 不生产
  化**（h1_mix{,_trigger}.json 留档）。测试 468→**484** 全绿、ruff、
  黄金回测逐位一致（S4 默认零漂移组合基线逐位复核）。坑位记录：沙箱
  nohup 子进程随调用退出被灭（长任务一律 harness 后台 job）; /tmp 跨
  调用清空（产出落项目内）; 组合基线复跑必须带 --priority strength。
- [x] **S2 过滤器 A/B + A2 模式面收敛（2026-10-22 夜）**：①**S2 实证**
  （`research/run_filter_ab.py`, 组合约束 60M/1tick/全窗口 close+strength）:
  ADX(14,≥25) **−2.48%**/DD44%/0.133 vs 基线 +77.07%/0.414；VolBand
  (250,10-90%) **−21.24%**/0.024 —— 先验"ADX 最可能过审"被证伪（海龟
  突破入场恰在趋势起点 ADX 尚低时被拦）；LiquidityFloor 绝对地板横截面
  不可比全放行（如需应写相对流动性变体）。**负结果已留档
  matrix_results/filter_ab.json + OPTIMIZATION_PLAN §S2**, 过滤器继续
  生产零启用（证据驱动）。②**A2-v1 `execution/models.py` StrategyMode**:
  is_backtest/period/entry_mode/intrabar/bt_no_simtick 五元组 on_init
  单点算定, 派生 main_period/calc_path/session_flush/wants_ticks/
  wants_catchup；接线 8 处分支点（on_init 主K注册+tick订阅、on_calculate
  分派、session_end 分派、catchup 三段闸门、on_tick 触发闸）。
  **口径修正实录**: is_backtest 改为 on_init 单点采样后, 旧测试
  （init 后翻转 ctx.is_backtest 依赖 live 重读）失败 → 测试改为 init
  前置位（与 WtBtEngine 真实行为一致）。test_mode.py 6 例真值表钉死
  派生规则。v2（撮合/证据/时钟完整 ExecutionModel 接口）留待 h1 组合
  二期后定义边界。测试 462→**468** 全绿、ruff 通过、黄金回测逐位一致。
