- [x] **S1/S2/speeds normalized profiles + 安全改造（2026-10-25）**：
  复核推翻“system 与 speeds 无条件等价”：S1/S2 是引擎入场语义及 base
  窗口预设，speeds 是共享执行/风控单元内的 profile 组合；legacy 双系统
  ceil/floor units 不能机械转 50/50（units=1 还有零份额 S2）。落地
  `core/profiles.py` 唯一 ProfileSpec 解析（key/system/entry/exit/fs/
  units/share），StraTurtle 三分支装配收敛为单一路径，ConfigLoader/
  runLive/run_portfolio_bt 共用。①配置安全：多档 speeds 强制显式
  name/system（55/20 或 name=S2 不决定语义），speeds 项未知键拒绝，
  fs 缺省跟随 base.s2_entry，按实际 profiles 校验 bar_cnt（250/125
  需 252，旧校验会永久 NaN 空转）。②快照安全：v4 增加 profiles
  语义指纹（system/windows/units/share），纯函数 validate_snapshot
  校验子系统集合+指纹+system，TurtleEngine.restore 也校验 system；
  改名/错语义 fail-loud，禁止漏恢复后静默塌缩到首 profile。③风控：
  execute_dual 每个 confirm 后立即 sync（同批预算预约，修 close
  第二子系统重复消费 total_risk）；单位闸改 open_units+新单位前置
  容量检查（5/6+2 不再放行到 7）。④研究：run_portfolio_bt 单 speed
  按真实 profile+required_bars，多 profile 明确拒绝（原静默按单 S1）；
  run_speed_study 增 --system S1|S2/open 跳空止损/完整 meta，55/20+S1
  不再误称生产 S2；独立审查另修出场 bar PnL（用实际出场价，剔除
  止损后的无持仓时段，test_speed_study 跳空回归），产物已重生成。
  测试 484→**498** 全绿、ruff、
  黄金逐位一致；close 组合基线新旧 CSV md5 相同（77.07%/29.17%/0.414，
  1927 笔），trigger 严格经典武装基线也一致（139.94%/37.84%/0.572，
  2389 笔）。**未生产启用**；多 profile net 成交/组合模拟、反向
  net/gross、share 保证金/累计风险分账仍是激活前阻断项。
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
- [x] **Phase3 阻断项修复第一批（2026-10-29）**：按审阅优先级落地并回归
  ——①多 profile `__dual__` 批次快照恢复不再兼容补录单 profile 出场
  记录（修复批次成交后误清仍持仓 S2 风险账，恢复→成交→S2 80手账本
  回归）；②mixed enter/add 改为**真实持久化延迟队列**（保留引擎
  pending、出场成交后重放、提交异常后下一无出场事件恢复、跨自然日过期
  清 pending），不再是"日志称延后实际 reject"；③多 profile 盘中止损/
  日损全平统一走异步 `TargetIntent` 批次，不再目标 0 即 confirm；
  ④组合研究换月日默认 `arm_exclude_signal=true` 使用已平移新坐标的
  `chan[i]`，空仓 S1 sim 单也随 `_apply_rolls` re-anchor；⑤实盘
  session_end 缺当日 m5 时不再用 his 末根冒充当日评估推进 `_d1_date`
  （保持锚点待 catch-up，修复数据事故后永久跳过当日窗口）；⑥
  `TradeJournal` 业务游标=文件身份(ino)+首笔成交身份+行号，新建或
  同路径原地截断后旧游标失效保持 UNKNOWN，半行写入不推进游标；⑦trigger S1 sim 单通道出场按实际
  tick/open 判定输赢，跳空不再被理想通道位误判盈利。测试 549→**559**
  全绿、ruff；README/Phase3 文档同步。**未做**：第二阶段统一
  enter/add/exit write-ahead intent、RiskManager profile 分账、组合
  回测多 profile——多 profile 生产激活仍阻断。
- [x] **第二优先级：profile 分账 + 组合多 profile + write-ahead outbox
  （2026-10-29）**：①`RiskManager` 账本键改 `(code, profile)`，价格/风险
  不再跨子系统 vwap 合并；单位/相关性组由 code 级 `_code_meta` 去重；
  累计风险与保证金改为"本 profile share 预算、扣本 profile 占用"
  （`post_risk_base` code 全局 + `post_profile_risk_base` profile 累计），
  首 profile 不能挤占后续份额，组合总敞口仍全局。②`run_portfolio_bt`
  支持 S1+S2/speeds 多 profile：独立引擎/通道/开仓日、共享账本逐
  profile 同步、close/trigger/h1 批次内预算预约、broker net 批末一次
  结算；费用按 profile gross（保守上界），summary 记
  `orders/book_mode/fee_mode/multi_profile_products`。③实盘 enter/add
  write-ahead：意图先于 set_position 入 v4 快照（`intents/outbox_seq`），
  恢复只允许绝对目标重放或真实成交证据确认，UNKNOWN 封锁不假确认，
  跨日过期；多 profile dual 循环内 confirm 保持批次预算预约、批末关闭
  意图。测试 559→**571** 全绿、ruff、diff-check。剩余硬边界：反向
  net/gross 需 C++ userTag；生产激活前仍需故障注入/真实 webhook/OOS。
- [x] **反向 net/gross 订单级归因分析：userTag 降级为备选项
  （2026-10-29，docs/ORDER_ATTRIBUTION_NO_CPP.md）**：论证 net 标量
  证据无法分解 profile 向量变化（同向部分出场/反向并存/跨零 mixed/
  共享账户串扰四场景），订单级归属必要；但 C++ userTag 非唯一解——
  现有 outbox 意图序 + EventNotifier 侧车（TRD_ORDER/TRADE 自带
  localid/islong/isopen/istoday）+ trades.csv 权威兜底三条零侵入链
  已拼成完整 "意图→localid→成交" 归因链，靠"信号 FIFO 消费 + 每策略
  独立执行器 → 提交序==建单序"即可绑定，无需改 C++。userTag 独占
  价值仅剩"同 code 同向同手数同 tick 双意图"（R4），缓解=批内错开提交
  或 UNKNOWN。结论：先落地 Python 侧 OrderAttributor + 2 周生产观测
  窗实证，错误率不达标再启动 A5-2 C++ 改造；mixed UNKNOWN 分支随
  意图侧拆签名自然消解。
- [x] **订单级归因链落地 + 多 profile 激活阻断项清零（2026-10-30）**：
  ①`core/OrderAttributor.py` 纯逻辑 localid 归因（簿镜像+签名预测+
  意图 FIFO×侧车/trades.csv 双源事件；提交序==建单序绑定；簿拆分漂移
  成交超量惰性反推；守恒破坏 UNKNOWN+attribution_mismatch 告警）接入
  enter/add/exit/dual 全路径，**跨零 mixed 不再一律 UNKNOWN**；
  ②故障注入回归 tests/test_order_attribution.py 16 例（外部订单穿插/
  撤单重挂/丢包 stub/簿漂移/双源去重/半行/进程内集成），测试 571→**587**
  全绿、ruff；③S1+S2 vs S1 组合 A/B（12 品种 close/60M/2019-2026,
  matrix_results/dual_ab.json）**负结果**：双系统双倍速消耗品种累计/
  保证金预算（rejected 1606、保证金超限 69 天）→ 约束 +6.2%/DD55.6%
  vs 纯 S1 +75.3%/DD29.4% → **生产维持单 profile S1**，多 profile 能力
  保留；④黄金基线显式刷新（10-26 武装口径修复去类前视优势，rb
  33.37%→9.73% 诚实口径，--update 声明）；⑤C++ userTag 正式降级为
  加固备选项。已部署生产（deploy/cta_turtle，rsync+md5 校验+SIGKILL
  重启，WatchDog 拉起）。剩余非代码项：notify webhook URL、归因链
  生产观测窗实证。
- [x] **远程巡检 + 归因链两项生产缺陷修复（2026-10-30 午后）**：巡检
  发现 ①共享账户上**其他策略**在做 INE.ec/ao/ag（订单回报 userTag
  `000001#...` 可见），其成交进入账户级侧车/trades.csv 后被全部 12
  策略的 OrderFeed 消费 → 成批 attribution unmatched 误告警
  （本品种 0 例，归因链本身正确）；②事件携带真实合约码
  (SHFE.rb.2610) 而归因队列以 HOT 键注册，绑定会整体失配（测试事件
  一直用 HOT 码故未暴露）。修复：ExecAttribution.poll 按品种前缀
  过滤 + 事件归一化到 HOT 键；各加 1 例回归（tests 18 例，
  全量 **589 passed**、ruff）。热修部署 + SIGKILL 重启复验：最新批次
  14:10:03 后 0 unmatched/0 error。运维记录：监控服务正确名
  wt-turtle-monitor（非 -mon，active 自 9/20）；今日 4 次启动批次
  08:47 盘前 / 10:18 / 10:21 / 14:10（后三次为部署）；premarket/
  contracts/hots/hotdaily 定时器全部正常；真值 balance 25.6M/avail
  13.5M，本策略持仓 rb 空 410/p 空 72/m 空 304/CF 空 159/IF 空 9
  与引擎一致。教训：账户级数据源的按品种过滤必须前置到 feed 层。
- [x] **profile 配置统一（2026-10-30 分析与实施同日完成，bfa81a7 A 方案 + f34eb20 B 方案）**：
  docs/PROFILE_CONFIG_UNIFICATION.md）**：结论=可合并且方向是配置层
  向代码层看齐（代码层早已统一为 ProfileSpec 唯一中间态，分裂只在
  配置层 system/speeds 双入口 + "speeds 非空忽略 system" 优先级规则）。
  引擎核验: system 全部用途 ≡ filter 布尔语义 + 预设名（窗口早已
  参数化, enable_s1_filter 已是半独立开关）。目标模型 =
  `profiles: [{preset: S1|S2, 逐字段覆盖, share}]`；两步走:
  A 配置层统一零引擎漂移（推荐先做）, B 引擎去 system 化（可选,
  快照 v5）。边界: 与双系统 A/B 负结果无关, 不改变生产单 profile 结论。
- [x] **profile 配置统一 A 方案落地（2026-10-30 傍晚，分析见
  docs/PROFILE_CONFIG_UNIFICATION.md）**：配置层向代码层看齐——
  `profiles[]` 成为唯一入口（`preset: S1|S2` = 窗口+filter 预设简写,
  显式 entry/exit/fs/filter 逐字段覆盖, 打开 "S2 语义+自定义窗口"/
  "S1 窗口+关过滤器" 组合空间）；`system`/`speeds` 转 deprecated 别名
  （展开结果与旧实现逐字等价：ProfileSpec 序列/name 顺序/units/
  share 全保留），显式使用打 WARN, 与 profiles 叠加 fail-loud 互斥
  （消灭 "speeds 非空忽略 system" 优先级规则）。引擎/快照零改动:
  ProfileSpec 增 filter 字段(默认 True, 不入快照指纹), 引擎构造
  enable_s1_filter 改传 profile.filter (legacy 路径推导值与原
  base 开关等价, S2 引擎门短路 ⇒ 行为不变)。生产 turtle_config
  删 12 处 system:S1 行 (缺省单 S1 profile, 启动零警告)。
  黄金基线第三次刷新: signals/funds/trades 哈希逐位一致
  (9.7264464 不变), 仅 config_sha 更新——零语义漂移实锤。
  测试 589→**606** (+17: preset 等价/窗口覆盖/filter 语义/双档
  等价 legacy/互斥/弃用告警/快照指纹不变), ruff, 已部署生产
  (64 文件 md5 全一致, 24 inited, 恢复正确, 0 error)。
  剩余 B 方案(引擎去 system 化, 快照 v5)为可选后续。
- [x] **profile 统一 B 方案落地（2026-10-30 夜，A 方案同日下午已落）**：
  引擎去 system 化——`TurtleEngine(system, enable_s1_filter)` 二元组
  → 单一 `filter: bool`（全部 10 处 system 门控改走 filter; "S1"/"S2"
  降级为 filter 派生只读标签, 日志/快照回滚兼容）; ProfileSpec 同步
  (filter 字段+system 属性); 快照 v4→v5（filter 指纹, 恢复路径
  v3/v4/v5 全兼容, v4 system 自动迁移 filter=system=="S1", 新快照
  双写 system 派生值支持代码回滚）。已知 fail-loud 边界记录: v4 快照
  无法区分"S1+裸20/10"与"S2"(v4 不持久化 enable_s1_filter), 迁移按
  system 判 — 该组合且改配置时恢复显式报错要求迁移, 生产(filter=T)
  不受影响。零漂移验证: 黄金哈希逐位一致 + 生产 v4 快照自动迁移恢复
  (rb 410 手/风险账一致, 0 error)。测试 606→**607** (+v4迁移/v5指纹/
  派生标签 3 例, 另机械迁移 ~40 处构造点), ruff, 已部署生产。
  system/speeds 配置别名保留可用 (deprecated WARN)。
- [x] **生产审计 + 两项修复（2026-11-02 白盘）**：例行巡检发现 ①**fund_live 旁路失效 3 天**——A1 迁移 (224997b) 把 `_get_fees` 改为薄转发 `(self, context)` 但遗留 `@staticmethod`，调用时 context 绑到 self 形参 → TypeError，自 10-30 部署起 fund_live.csv 停写且 Runner 每秒刷警告（"盘中资金不可见"事故检测器失明）；修=去装饰器 + 3 例回归（tests/test_fund_live.py）。②**l/pp 引擎层账脱管潜伏一周**——引擎 267/246 手 vs 信号/券商 137/89：10-23 夜出场证据链断裂使引擎残留陈旧层（l [130@9151]、pp [112+45]），10-30 新周期入场被引擎误记为加仓（"add layer2 121"实为新开仓），风险账虚高 ~30 万导致 10-29 已见 total_risk(cap=0) 误否决；**对账盲周根因**：生产每日双重启（08:47 盘前+20:40 hotdaily）使 `_recon_miss` 归零，而 reconcile 只在日评估路径运行、每窗口仅 1 次 → 失配永远无法累计到 patience=3（RECONCILE 历史零触发）；修=①计数随快照持久（v5 键 recon_miss）②on_init 恢复后引擎净仓≠信号仓即播种到 patience-1（差异已跨重启存续，非在途单）+ 4 例回归（tests/test_reconcile_restart.py）。测试 607→**614**，ruff，黄金零漂移；已部署并 kill -9 重启（10:57 激活），l/pp 播种告警按预期出现，15:15 日评估将对账重建为信号仓单层。账户侧：上午商品拉升使共享账户权益自峰值回撤（riskmon IDD 137% 系 (峰值-现值)/(峰值-昨结) 口径，30 分钟窗外不触发熔断，无强制减仓，账户当日仍 +54 万）；l 真值差 -11 系周五 layer3 加仓 16 手仅成交 5 手、周一 MiniImpact 1 手切片补满（byvol@1.00 策略），已自然收敛。
  - **15:15 随访确认**：l/pp 按预期 `RECONCILE: mismatch persisted ... rebuilt into S1 as single layer [137@9621.77]/[89@10571.04]`，引擎/风险账/真值三方收敛，recon_miss 归零；11:02 CTP 8193 断线 1 秒内重登；对账链 alerts（reconcile_seed×2/reconcile×2）完整落盘。残留 s1_last.entry_price=9151（陈旧层价）经源码复核**无害**：S1 过滤器跳过判定在入场评估前必经出场结算（_settle_s1_exit 用重建后层均价判输赢，B2 同 bar 结清已覆盖 close 同根反转），陈旧值不影响任何未来决策，无需快照手术。
- [x] **复核报告 + Phase 0 运维安全网落地（2026-11-09 夜盘）**：产出 `docs/ARCH_REVIEW_2026-11-09.md`（业务/架构/代码三维复核，含自我复核修订 4 处：加仓层数 4 层算术、pricemode=1 对手价、fill_timeout 硬编码、快照语义不匹配 fail-loud），随后实施 Phase 0 全部 6 项——①**fill_timeout 按紧急度分档**（schemas 增 `risk.fill_timeout_secs=600`/`exit_fill_timeout_secs=120`，fill_tracker 4 处硬编码 600/1800 改走配置，免费品种 3x 窗口保持 1800 等价；出场告警 600s→120s 仅影响告警时机）；②**快照损坏 → DEGRADED**（on_init except 扩到 ValueError/KeyError，恢复失败禁新开/加仓（router 三处门），出场/收养保留，首次 _persist 解除，alert state_degraded）；③**策略心跳**（实盘每 5s 写 `generated/heartbeat/<name>.json`，on_init/session_end 强制帧）；④**周期快照 300s 节流 + snapshots 3 深轮换备份**（内容 hash 去重）；⑤**`_on_arm` 价格不可用门**（stra_get_price 0/None/NaN → 解除入场武装+每日一次告警 no_price_arm_block，防 9-28 类断流后 stale 武装位假突破）；⑥**外部 watchdog `scripts/heartbeat_watch.py`**（进程存活 180s/tick 断流 360s 交易时段判定 sessions.json 跨零点节段+holidays，状态转换告警+30min 重复提醒，自崩走 timer_notify）+ systemd `wt-turtle-heartbeat.timer` 每分钟 + **`scripts/doctor.py`** 只读体检 CLI（配置/hots/数据时效/真值/每策略引擎-信号-券商三方对账/心跳/备份, exit 0/1）。测试 614→**642**（+test_phase0_safety 14 +test_heartbeat_watch 14），ruff，**黄金基线逐位一致**；已部署（rsync+manifest 校验，64/64 一致），install_units 渲染 heartbeat 双 unit，22:19 kill -9 重启：12 策略零异常恢复（rb 4 层/风险账 412 手/预创建一致）、12 心跳文件即刻落盘、watchdog 首轮即报 no_heartbeat→recover（重启窗口预期行为）、doctor 0 error。**notify webhook 仍待用户提供 URL**（watchdog/doctor 告警暂只落 alerts.log）。**部署期间发现既有现场（非本次改动引入）**：rb/cu/TA/m/p/CF 信号仓 vs 券商仓 2 倍差（-412 vs -206 等），21:07 起 attribution unmatched fills 持续刷屏 + truth_mismatch_persist——判读为共享账户上反向策略（同 repo cta_spread_arb 腿）对净仓的对冲 + 归因串扰，alert 模式正确拒绝重建，truth_mismatch 链完整告警，待人工核对。
- [x] **riskmon 静默砍仓事故完整归因（2026-11-09 夜盘，两次误诊后定案）**：21:06:44 夜盘开盘 7 分钟商品拉升、6 品种空头浮亏扩大 → 账户权益自日内高点回撤 IDD 21.66% ≥ 20% 阈值 → **我们自己的 C++ SimpleRiskMon 触发**（9-03 配置 riskmon 段 `risk_scale 0.5` + `inner_day_fd 20%`，当时作为"risk_scale=1.0 时空转"的修复引入，从未实战触发过）→ `setVolScale(0.5)` → `WtCtaEngine` 在所有目标提交点（`WtCtaEngine.cpp:367/457` `abs(pos)×_risk_volscale`）缩放 → **我们自己的 MiniImpact 执行单元忠实执行了 593 手平仓**（rb 206/m 199/CF 79/TA 66/p 36/cu 11，21:07-21:4x 1 手切片，usertag `otp.simnow.*` = `TraderAdapter.cpp:105` 对无 tag 订单的自动生成格式）。信号层完全不知情 → truth_mismatch/归因 unmatched 告警风暴。**关键机制：scale 持久化**——`setVolScale` 经 `save_datas()` 写 `generated/portfolio/datas.json`（`riskmon.{scale,date}`），重启经 `WtEngine.cpp:583` 恢复，**配置改回 1.0 不能覆盖运行态**；且 `_risk_date=20261110`（夜盘归属次日交易日），0.5 会一直管到整个 11/10 交易日——23:48 重启后 cu 立即被二次砍半（目标 -12→-6→实平 -3）实锤。**两轮误诊记录**：第一轮判"ec/ag/ao 外部策略平仓"（被 2 倍精确比例与 SimNow 账户级订单广播误导）；第二轮判"otcpricer"（被 `otp.` 前缀误导，实为 WT 自有格式）——用户两次纠正。**排除证据**：futu_mm（dist/WtUftRunner，配置只有 ao/ec 两个策略，其订单 usertag `wtp.trader_ctp.*`/`000001#*`，因 SimNow 同账户多 session 私有流互播出现在我们日志）。**处置**：①config `risk_scale 0.5→1.0`（riskmon 退化为纯监控，缩量归策略层 RiskManager/daily_loss——它们本来就在做且不撕裂账本）；②`portfolio/datas.json` scale→1.0；③手术裁剪 6 品种双账到 broker 真值并工具化 `scripts/trim_to_truth.py`（dry-run/备份/审计）；④23:58 重启验证：risk scale 1.00 生效、无 0.50 日志、cu 自动回补到 -6、doctor 0 error。**教训**：①C++ 执行层 governor 静默改仓位 = 三方账本撕裂，任何"自动减仓"必须走策略层并留告警；②riskmon 触发只有 risk 日志没有告警通道（notify 未配的代价连环放大）；③诊断纪律：怀疑外部行为者之前，先排除自身系统——查执行单元目标日志（`Executer/exec_*.log` 的 `Target position changed`）比查成交流水更接近真相；④`otp.`/`wtp.` 这类前缀先查 `TraderAdapter` 的 pattern 生成逻辑再下归属结论；⑤SimNow 同账户多 session 订单流互播，我们的 Trader 日志会混入 futu_mm 的订单（按 usertag 过滤才可靠）。
- [x] **Phase 1 落地（2026-11-10）：旧回测作废 + 25M 资本模型(shadow) + 逐合约信号口径**：用户三决策全部落地——①**旧回测作废**：`matrix_results/` 38 件全部移 `_void_20261110/`（README 标注 VOID_raw_hot_unadjusted，禁止引用）；②**资本模型 v2**：`risk.unit_risk_frac=0.01` + `shadow_capital=25M` 观察期（生产预算零变化，`shadow_size` 否决日志旁路对比，手数差异首见告警；翻转 = portfolio_capital=25M + base.unit_var=0 + shadow=0）；`PositionSizer.layer_lots` 支持 per-call unit_var、`StraTurtle._unit_var()` 派生（显式>0 优先，黄金口径不变）、`alloc_total` 全链路透传、`unit_var=0` 无资金源时 ConfigLoader 拒绝；③**换月不采用复权，移仓后新合约自身序列重算信号**：`base/instrument.signal_source`（hot|contract，品种级覆盖优先，默认 hot 零漂移）；**live=hots.json 当前主力单合约**（配 20:35/20:40 运维节奏，盘中换月 `signal_source_roll` 告警）；**bt=common/roll_calendar.json 逐日主力**（每历史日指标在当日主力自身真实序列，数据经 storage/csv/contracts/*.json 抓取缓存**直读**——24 个 C++ 副K 注册触发 CtaMocker::stra_get_bars 段错误，实证绕开）；三处日评估路径（on_calc_daily/session_end 合成 m5 用合约码/catchup）统一 `_signal_arrays` 替换 + `align_window` 尾部对齐/上市前截断/缺失沿前收。**数据管道**：`scripts/build_contract_signals.py`（sina 逐合约全历史+每日 max-hold 日历+自校验，~830 合约 12 品种 10 分钟；CZCE 三品种 sina 日线下架/EM 无已摘牌 → **保持 hot 待 tqsdk 源**，instrument 级覆盖）、`merge_historical_contracts.py`（contracts.json 回填 269 历史合约键=品种+年月 rb1905 口径，缺条目 C++ 段错误实证）、`seed_live_contracts.py`（实盘 his/day/<product><yymm>.dsb 回填）。**A/B 实锤（matrix_results/phase1_contract_ab.json）**：rb 2018-01~2023-04/5M/0tick trigger 同窗口 **逐合约 +4.43%/DD26.8%/345笔 vs HOT拼接 +9.73%/DD25.7% — 旧口径虚高 5.3pp（+54%）**，坐实 B2 研究污染定性；黄金(hot) 声明式更新 config_sha（新 risk 字段）signals/funds/trades 逐位不变。**Phase 1 剩余**：全 9 品种组合重跑与参数重验（arm_exclude 等）、CZCE tqsdk 源、production 切 contract 模式前的 live 种子部署（seed_live_contracts + 服务器 contracts.json 合并）、shadow 观察 1~2 周后翻转。测试 642→**668**，ruff，黄金零漂移；已部署生产（72 文件校验一致，12 策略零异常恢复，signal_source=hot 默认无行为变化，shadow 日志待下次决策触发）。
- [x] **CZCE 数据源补齐：ks2wtpy/KS S3 接入（2026-11-10，用户指示的 Phase 1 数据补充方案）**：`tools/ks2wtpy`（KS→WT 转换管道，S3/MinIO 源，credentials.json 本地凭据不外泄）全量转换 CZCE TA/MA/CF 日线（1152 合约 16s，endpoint 须用 hostname:6900 S3 API 端口而非 url 字段的 6901 console 端口）。`build_contract_signals.py --ks-csv-dir` 适配器摄入（4位年月→WT CZCE 3位码，SPD/IPS/HOT/main 跳过，open_interest=hold，幂等）→ **12 品种全部具备 contract 信号源**；CZCE 日历 max-hold 自校验 mismatch 仅 **2.2~2.5%**（vs SHFE 系 5~30% 的定义性偏差，KS 数据质量显著优于 sina 拼接）。逐十年同码（TA601=2016+2026）合并进同一缓存（日期 disjoint 不影响按日判定，merge 的 opendate/expiredate 跨十年已记文档）。**TA 合约模式全窗口回测验证通过**（31 段换月正确，无 m5 自动退化口径）。全 12 品种 contract 矩阵后台重跑中（2019-01~2026-08/5M）。测试 668→**671**，ruff。
- [x] **Phase 1 十二品种矩阵重跑完成（2026-11-10 深夜，contract vs hot 同口径对拍）**：`matrix_results/phase1_matrix_12p.json`（2019-01~2026-08/5M/品种，CZCE 无 m5 双口径同退化）。**结果无系统性方向、个体重洗牌巨大**（rb +2.6→+10.0 / p +11.7→+41.8 / l -3.7→-36.0 / m +6.3→-21.5 / sc +21.5→-3.4），证实旧口径换月跳空污染是**双向噪声而非单边偏差**，旧 12 品种矩阵 +58.5% 被显著美化；简单加总 hot ≈ +89pp vs contract ≈ -64pp。**剩余**：组合级 run_portfolio_bt contract 接线、参数重验（arm_exclude 等）、shadow 观察期后翻转、生产切 contract 模式（live 种子 + 服务器 contracts.json 合并）。
- [x] **contract 口径组合级参数重验（2026-11-10，Phase 1 决定性结果）**：`matrix_results/phase1_portfolio_revote.json`（12 品种独立 5M 等权/2019-01~2026-08，三配置对拍）。**结论与旧污染口径全面反转**：①**entry_mode close 碾压 trigger**：组合 Σ +74.7pp vs -14.0pp（差 88.7pp），12 品种 10 个改善（m -21.5→+14.9 翻转 36pp / p →55.9 / CF →48.9），且多数 DD 同步下降（p 37.9→25.2 / CF 30.3→13.8）——旧口径"trigger 优于 close"系拼接序列假跳空上的盘中触发捡便宜，随污染清除而反转；②**arm_exclude 方向翻转但幅度温和**：armF Σ -1.5pp 优于 armT -14.0pp（+12.5pp，rb/p 主导）；③**生产当前配置 (armT+trigger) 是三种中最差**——两个参数都是污染数据时代的错误决策。**方法论教训**：单品种 A/B 噪声主导（arm 单品种 2:1 分裂 vs 组合方向不同），参数决策必须组合级；另实锤背景批随 git 命令同壳启动被会话回收（setsid 独立拉起解决），以及日历被子集构建清空导致 AB 批静默退化为 hot 的完整事故链（合并写已修）。**待用户拍板**：生产切 `entry_mode=close + arm_exclude_signal=false`（需声明式重建黄金基线 close 口径 + run_portfolio_bt contract 接线配套）。
- [x] **参数切换两口径裁决 + armF 落地生产（2026-11-10）**：双口径组合级重验矩阵（hot/contract × armT/armF × trigger/close）——**arm_exclude=false 口径无关一致赢**（hot +73.9pp / contract +12.5pp，hot 下 rb 单品种 golden 9.73%→**38.06%/DD20.9**），**已切换生产**（黄金声明式重建）；**entry_mode 口径依赖**（hot trigger 胜 +87.3 vs close +49.3；contract close 胜 +74.7 vs -14.0）——**维持 trigger**，与 contract 信号源绑定为整体升级包（live 种子+服务器 contracts.json 合并后一起切）。部署验证：12 策略零异常。新增 golden 口径=trigger+armF+hot。待办：run_portfolio_bt contract 接线（正式组合级成绩）、contract live 激活包、shadow 观察后翻转 25M。
- [x] **Phase 1 收官：run_portfolio_bt contract 接线 + close 实盘拓扑适配 + 生产激活包（2026-11-11）**：①组合回测 contract 接线——`build_contract_series` 逐日主力拼接（信号+执行统一新合约自身序列，上市前截断/缺口前收/volume+hold 带上），`_no_roll_shift` 禁执行层价差平移，manifest 纳入 roll_calendar+缓存哈希。**正式组合级成绩（6000万/close+armF/2019-01~2026-08）：contract 约束 +21.4%/dd40.9/sharpe0.236 vs hot +62.1%/dd30.7**——真口径组合期望（年化≈2.5%）显著低于旧认知，与单品种矩阵方向一致；②**close 模式实盘拓扑适配**（关键发现：close 原 session_flush=none，实盘 d1 永不闭合 F3 → 空跑！）——session_flush/wants_catchup 扩展到 close+live（session_end m5 合成日K 驱动收盘决策，_eval_core 本就模式无关），close+live 订阅 m5 副K，contract live 增备主力合约 m5 副K（session_end 合成原料），close+intrabar_stop 双驱动互补（m5 主K盘中 2N 止损扫描，补 tick 止损缺失）；③**生产配置三件套切换**：entry_mode=close + signal_source=contract + 12 品种 intrabar_stop=true（收盘确认次日开盘成交口径+盘中止损保全）；黄金基线声明式重建 rb close+contract+armF=**+10.89%/DD22.71%**。测试 675→**677** 全绿，ruff，golden 对拍一致。**遗留**：close 实盘成交滑差（收盘确认→次日开盘）待观察；contract+intrabar 组合实盘首活，20:40 窗口重启验证； Phase1 仅剩 shadow 观察期（1~2 周）后翻转 25M 正式口径。
- [x] **contract live 激活包落地 + 两个实盘首活 bug 修复（2026-11-12 凌晨，Phase1 收官）**：生产三件套切换（close+contract+intrabar_stop×12）部署重启，12 策略 inited、风控账本恢复（rb -411 手等）、signal source 全部解析为当前主力真实合约码。**激活链两个实盘首活 bug**：①**CZCE 合约码双重前缀**——hots.json 'to' 对 CZCE 是 'TA701'（contracts.json 键口径），`_full_contract_from_contract` 只认 SHFE 'rb2701' 4 位格式 → 输出 'CZCE.TA701.A701' 框架查无此合约（信号副K将静默无数据）；修：非 4 位数字后缀直接 ex+原码（CZCE.TA701）。②**close 模式 tick 心跳永久误报**——close 不订 tick → on_tick 永不回调 → `_last_tick_ts` 恒 0 → heartbeat_watch tick_silent 刷屏；修：on_tick 心跳基准移到模式闸前（tick 断流检测模式无关）+ StrategyMode.wants_tick_feed（close 实盘也订 tick，on_tick 对 close 只更新基准不做触发/意图）。**验证**：watch 零告警、tick_age 实时（<5s）、黄金 rb close+contract+armF +10.89%/DD22.71% 对拍一致。测试 677→**680** 全绿，ruff。**待验证（明交易时段首活）**：09:00 close 模式 catchup、15:15 session_end 合成日K 收盘决策、盘中 m5 intrabar 止损扫描。**Phase1 至此全部落地，仅剩 shadow 观察期（~1-2 周）后翻转 25M 正式资本口径**。
- [x] **激活包首活三连修 + 换月盲区根治（2026-11-12 盘中）**：生产 intrabar 首活暴露三个连环缺口，全部修复并部署验证：①**intrabar d1 副K漏订**——`calc_path=="intrabar"` 时主K=m5 但信号周期 d1 从未订阅副K（原注释"信号周期作副K"实现漏写），实盘 C++ 未订阅返回 None → `_on_calc_intrabar` 首行静默 return，m5 驱动正常但**零评估零止损扫描**（11:00 前 rb 全日无评估痕迹实锤）；②**intrabar 路径缺 contract 信号替换**——`_on_calc_intrabar` 直接 `_calc_indicators(HOT 序列)`，与 `_on_calc_daily` 的 `_signal_arrays` 不同步，contract 品种静默退回 HOT 口径（修复后 golden 重建 rb +10.89%→**+25.74%/DD19.94%**，提升 14.85pp = 真口径信号增益）；③**live 新主力换月盲区**——live contract=hots 当前主力单合约，新主力上市初期自身历史 < need_bars(57) → 每次换月后数周信号盲区；新增 `align_window_hotfill`（hotsutil 纯函数）：HOT 为底+主力重叠段覆盖（与 bt 逐日主力同构），used=0 返回 None 防静默退纯 HOT。**验证**：12 inited、watch 全 ok、rb window insufficient 告警消除、golden 对拍一致。测试 680→**682** 全绿，ruff。
- [x] **历史遗留推进 D1 勾选修正 + S2/过滤器 A/B + D5 归因链实证工具（2026-11-12）**：①D1（profile 配置统一）查证**已于 10-30 完成**（bfa81a7 A 方案+f34eb20 B 方案），AGENTS 复选框补勾；②**辅助过滤器 A/B**（contract 真口径/6000万约束/4 变体，matrix_results/phase2_filter_ab.json）——**负结论：内置过滤器无一改善组合**（ADX +19.8%/dd35.3/0.218 ≈ 平价换回撤 / VolBand -1.9% / LiqFloor +8.4% / ADX+Vol -24.9% vs 无过滤基线 +21.4%/dd40.9/0.236），生产维持不启用（唯一候选 ADX 待更长窗口复验）；③**D5 归因链实证工具** `scripts/audit_attribution.py`（策略日志证据分布+失败模式告警计数+trades.csv 品种白名单/localid 归因率交叉验证，CZCE 2 段码兼容），首轮生产实证 7 天窗：**本策略 6835 手成交 localid 归因率 100%**、失败模式有界（fill_timeout 1 次有完整告警链、signal_window 6 次系换月盲区修复前旧告警）。测试 682→**685** 全绿，ruff。
- [x] **15:15 session_end 首活验证 + C++ reader 缓存冻结实锤修复（2026-11-12 收盘后）**：close 模式核心路径首活——**9 品种正常**（eval window 121 根 synth=yes m5 聚合合成当日日K），**CZCE 三品种"当日 m5 缺失"跳过**。追查数据面齐全（rt/min5/CZCE/TA701.dmb 15:00 更新/his DSB 俱在），定位**交易侧 C++ reader 拼装缓存冻结**：新订阅的主力合约 m5 副K在 00:00:31 重启时装载 rt 块并缓存——SHFE 夜盘品种缓存停在重启时刻（白盘 09:00-15:00 段缺失，当日合成残缺），无夜盘的 CZCE/IF 当日完全空。**当日所有品种 15:15 合成均用了重启时刻残缺的 m5**（一次性污染，决策已执行无法回退，影响=一次收盘评估口径微差，记入观察）。**修复**：`_session_eval_daily` 合成原料统一读 **HOT 的 m5**（主K级成熟订阅；当日 HOT=hots.json 主力映射=当日主力自身，contract 真实价语义不变），删除主力合约 m5 副K订阅（消除同类缓存坑）。golden 零漂移（bt session_end 首行 _d1_date 去重直接 return 不受影响）。**已知尾巴**：CZCE 11/12 收盘决策丢失（锚点未动，11/13 盘中将按 11/12 收盘信号延迟执行，状态有序不错乱）；夜盘 23:00 session_end（rb 等）+ 明日 15:15（CZCE）验证修复效果。测试 685 全绿，ruff。
