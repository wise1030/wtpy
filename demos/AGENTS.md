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
- [ ] **profile 配置统一可行性分析（2026-10-30，未实施，
  docs/PROFILE_CONFIG_UNIFICATION.md）**：结论=可合并且方向是配置层
  向代码层看齐（代码层早已统一为 ProfileSpec 唯一中间态，分裂只在
  配置层 system/speeds 双入口 + "speeds 非空忽略 system" 优先级规则）。
  引擎核验: system 全部用途 ≡ filter 布尔语义 + 预设名（窗口早已
  参数化, enable_s1_filter 已是半独立开关）。目标模型 =
  `profiles: [{preset: S1|S2, 逐字段覆盖, share}]`；两步走:
  A 配置层统一零引擎漂移（推荐先做）, B 引擎去 system 化（可选,
  快照 v5）。边界: 与双系统 A/B 负结果无关, 不改变生产单 profile 结论。
