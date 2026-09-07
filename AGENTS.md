# AGENTS.md — cta_turtle 项目分析与改进路线图

> 本文档是 `demos/cta_turtle`（海龟趋势跟踪 CTA 系统）的完整分析报告与实施路线图。
> 供后续开发/AI 代理接手时快速对齐上下文。实施状态用 ✅/🚧/⬜ 标记。

## 0. 项目定位

基于 WonderTrader/wtpy CTA 框架的经典海龟交易系统，交易国内商品期货主力连续合约。
设计目标：

1. 主逻辑基于海龟算法做趋势跟踪（S1: 20/10+盈利跳过过滤+55 fail-safe；S2: 55/20）；
2. **通过过滤器模块可配置不同辅助业务逻辑进行信号过滤**；
3. 支持**多品种、多周期**（d1/h1 每品种独立配置）追踪商品期货趋势。

## 1. 现状盘点

| 模块 | 文件 | 完成度 | 说明 |
|---|---|---|---|
| 策略壳 | `StraTurtle.py` | ★★★★★ | 薄壳模式：生命周期/图表/执行/持久化/换月re-anchor |
| 信号状态机 | `helpers/TurtleEngine.py` | ★★★★★ | 纯逻辑无框架依赖，决策-确认两段式 |
| 指标 | `helpers/indicators.py` | ★★★★★ | Wilder ATR(lfilter) + Donchian(sliding_window_view) 向量化 |
| 仓位 | `helpers/PositionSizer.py` | ★★★☆☆ | VaR反算；缺资金占比/保证金约束 |
| 配置 | `schemas.py` + `ConfigLoader.py` | ★★★★★ | dataclass 校验 |
| 回测 | `runBT.py` / `run_matrix.py` | ★★★★☆ | 单品种+矩阵(serial/subprocess)，绕开 WtBtEngine 单上下文限制 |
| 实盘 | `runLive.py` + 全套 yaml 模板 | ★★★★☆ | 多品种多实例，SimNow 占位凭据 |
| 部署链 | `make_deploy.py` / `deploy_tpl/run.py` / `scripts/init_monitor_db.py` | ★★★★★ | WatchDog 托管 + 独立 WtMonSvr，生产级 |
| 数据工具 | `CsvDataLoader.py` / `scripts/convert_csv_to_dsb.py` | ★★★☆☆ | DSB segfault 两个绕行方案并存，未自动化 |
| 测试 | `tests/` | ★★★★★ | 全部通过，覆盖引擎全逻辑 |

### 已验证的架构亮点（勿破坏）

1. **纯逻辑引擎与框架壳分离**——`TurtleEngine.on_bar()` 只吃 OHLC+N+通道字典，可脱离 wtpy 单测；
2. **决策-确认两段式**——被否决的入场不提交引擎、不污染 S1 过滤器状态；
3. 边沿触发 + `_fs_armed` 带状态的 fail-safe；
4. HOT 换月 avgpx 漂移 re-anchor；
5. 无未来函数（通道不含当根、收盘确认语义有明确口径与测试）。

### 回测现状

`SHFE.rb.HOT` 2018-01~2023-04 双基线（同窗对比）：

| 基线 | 首仓 | 总收益(5.3y) | 最大回撤 | 胜率 |
|---|---|---|---|---|
| 风控改造前 (无组合预算) | 477 手 | 175.4% | **71.7%** | 27.5% |
| 风控后 (risk: 段默认值) | **59 手** | 6.9% | **9.9%** | 27.8% |

风控闸门精确生效（59 = floor(500万×2%/(2N×volscale))）；收益压缩属预期
（风险预算为约束项），实操可通过调大 `max_risk_per_mkt`/units 平衡。
滑点敏感性：0→2 tick 总收益 6.91%→6.46%（相对损耗约 6.4%，稳健）。
~~`common/hots.json` 仅注册 rb/hc/l/pp~~ **2026-09-02 已扩至 12 品种**
（rb/cu/al/sc/TA/MA/CF/pp/l/m/p/IF），数据经 `scripts/fetch_data_sina.py`
新浪主力连续拉取（注意与 WT HOT 换月规则不同源，换月日可能小幅跳空）；
12 品种矩阵（2019-01~2026-09, S1 trigger units=1, 500万/品种独立）：
组合等权 +58.5%/回撤 28.5%/sharpe 0.34，明细见 matrix_results/。

## 2. 差距分析（按严重度）

### P0-1 可插拔过滤器模块缺失（与设计初衷最大差距）

- S1 盈利跳过硬编码在 `TurtleEngine._eval_entry/_s1_filter_skip`；
- 方向闸硬编码在 `StraTurtle._execute` + `InstrumentConfig.allows_direction`；
- `filters.yaml` 是框架执行层开关（strategy/code/executer bool 开关），与信号过滤无关。

**方案**：新增 `filters/` 包。`SignalFilterBase` 协议 + 注册表；方向闸迁移为
`DirectionGateFilter`；新增 AdxTrend/VolatilityBand/LiquidityFloor 内置过滤器；
S1 盈利跳过保留在引擎内（sim 单推进与其深度耦合，外移风险大），但加
`enable_s1_filter` 开关可关闭。过滤器在引擎决策之后、执行之前链式调用，
只读 bars/指标/引擎快照，禁止修改引擎状态；配置驱动、逐级记录否决原因。

### P0-2 风险控制只有单层 VaR，无组合层约束

现状：rb 首仓 477 手（≈3.8 倍杠杆），4 层满仓一次 2N 止损 ≈ 本金 64%；无保证金检查
（实盘拒单→`implied_fill` 退 fallback→引擎状态脱管直到重启）；无经典海龟跨市场单位限制。

**方案**：新增 `helpers/RiskManager.py`，在下单前做最后闸门：
单品种风险预算（该笔 2N 潜亏 ≤ equity×max_risk_per_mkt）、组合总敞口上限、
相关性组单位数限制、保证金占用预估；权益 = `fallback_capital` + `stra_get_fund_data(0)`
净盈亏（bt/实盘该接口均不含本金，2026-09-02 修正），
无效时退回配置的 base_capital；缩量优先于一刀切否决。

### P0-3 回测/实盘一致性与状态对账缺口

- 日线止损 bar-close 成交 vs 实盘下一时段落单，系统性偏差未量化；滑点默认 0；
- 无运行时对账：部分成交/拒单/手工干预只在 on_init 收养一次。

**方案**：(a) 运行时 reconcile——持仓与引擎层数据连续 N 根 bar 不符则告警并按实际持仓重建；
(b) confirm_* 依据 `_implied_fill` 反推的实际增量提交；(c) P1 阶段再做 m5 主K盘中止损。

### P1-4 多品种多周期只有骨架

数据准备全手工（hots.json 键 + storage K 线两项前提）；矩阵回测每品种独立按满额资金算，
无组合合成权益曲线；h1 小时线管线未验证。
**方案**：`scripts/prepare_data.py` 一键管道 + 数据健康报告；run_matrix 组合层汇总。

### P1-5 策略功能相对经典海龟缺失

每品种 S1/S2 二选一（经典为双系统各半单位并行）；无相关性分组限制落地；
无涨跌停无法成交处理。
**方案**：`system: "S1+S2"` 双引擎各半量共享风控预算（P2）；涨跌停顺延逻辑挂入过滤器链。

### P2-6 工程细节

`_stop_price` 私有越界访问（StraTurtle.py 图表段）；fail-safe 双口径无注释；
README 测试数/日期区间漂移；加仓阈值用当根 N 无平滑选项；serial 模式复用单例自认风险；
无 lint/typecheck/CI。

## 3. 实施路线图

### 阶段一（P0）：过滤器架构 + 风控闭环 —— 对齐设计初衷

- [x] `filters/base.py`: SignalFilterBase 协议（init/allow_entry/on_bar/snapshot）+ FilterRegistry
- [x] 内置过滤器：DirectionGate（迁移）、AdxTrend、VolatilityBand、LiquidityFloor
- [x] TurtleEngine 增加 `enable_s1_filter` 开关（默认 True 保持行为不变）
- [x] StraTurtle 装配过滤链：DEC_ENTER 先过过滤链再过风控，否决原因分级日志
- [x] `helpers/RiskManager.py`: 单品种/组合风险预算 + 相关性分组 + 保证金预估
- [x] schemas/ConfigLoader: instruments[].filters + risk 配置段校验
- [x] 运行时对账循环（连续失配检测 → 告警 → 按实际持仓重建）
- [x] 新增单测：filters/risk/reconcile/config，全部通过

### 阶段二（P1）：一致性工程 + 多品种数据管道

- [x] m5 主K盘中止损适配（d1 副K取通道，信号日期去重防重复触发；快照v2含去重键，v1兼容）（2026-08-23）
- [x] `run_slippage_sens.py` 滑点敏感性矩阵报告（2026-08-23）
- [x] `scripts/prepare_data.py` 数据一键管道 + helpers/DataChecker 预检（runBT/runLive warn-only 接入）（2026-08-23）
- [x] run_matrix 组合层权益合成与贡献分解（helpers/portfolio.py 纯函数；dynbalance 为相对初始资金盈亏口径）（2026-08-23）

### 阶段三（P2）：策略完备性 + 平台化

- [x] `system: "S1+S2"` 双系统并行（各半 units、净仓批量执行、品种预算按份额切分、快照v3）（2026-08-23）
- [x] `run_walkforward.py` 滚动窗寻优（训练/验证月度切分+网格+OOS汇总；纯函数可单测）（2026-08-23）
- [x] notifier 推送适配器（企业微信/钉钉/generic webhook，冷却去重；接入 reconcile/risk/session 事件）（2026-08-23）
- [x] walk-forward 分析面板（2026-09-01）：`run_walkforward` 全网格 IS 成绩落盘 wf_grid.csv + `scripts/wf_report.py` 单文件 HTML（汇总卡片/参数稳定性频次/双参数维热力图红绿色阶/逐窗 IS-OOS 对照；纯 stdlib 纯函数）
- [x] TqSdk 数据适配器 `scripts/fetch_data_tqsdk.py`（2026-09-01）：dmpBars 回调→BarJournal.merge_rows 幂等合并进 storage/csv（与运行时回补同源键逻辑，分钟线 time 列 G2 口径）；--fix-hots 用 query_cont_quotes 写当前主力映射（默认仅空键）；凭据 TQ_USER/TQ_PASS；tqsdk 未装时优雅降级；集成路径需真实 tqsdk 环境，纯逻辑 10 例单测
- [x] 公开 engine.stop_price() 替代私有访问；fail-safe 双口径注释统一（宽松跳空口径 vs 严格边沿互补，已复核并写入源码注释）；CI(pytest+ruff, 无wtpy环境桩降级)（2026-08-23）
- [x] 历史K线运维体系（docs/DATA_OPS.md）：暖机深度门禁(DataChecker.check_depth) + 运行时旁路日志(helpers/BarJournal, d1/m5双版式幂等回补storage/csv) + 启动顺序编排(deploy run.py: WT_AUTO_REPAIR→WT_PREFLIGHT strict自愈闭环)；顺带修复部署入口漏共享风控/notify实例（2026-08-24）
- [x] C++ 底层数据链路源码核验（docs/DATA_OPS.md v2 §0/§1）：确认实盘K线仅由 WtDt 数据底盘的 WtDataWriter 合成落盘（rt/{min1,min5} mmap 当日块，会话轮转转存 his DSB；日线 DSB 轮转自愈去重、min DSB 盲追加）；核验 WtDataReader 拼装语义（his尾部+rt当日块、ext loader 单次缓存、重启后 rt 游标归零会重放当日bar）；**发现三个设计级缺口**：①现 runLive 单进程直挂 ParserCTP 无 bar 合成器→实盘无K线事件，必须改 datakit 双进程拓扑；②实盘 d1 无 rt 块永不闭合（on_bar 不触发、get_bars 无当日），日常评估须改由会话末根 m5 驱动；③csv 暖机含当日K会与 rt 重复、min DSB 回补须遵守 his/rt 交易日分区律（2026-08-24）
- [x] **方案C落地：entry_mode=trigger 盘中触发**（2026-08-24）：引擎武装-触发状态机（on_bar仅武装含当根包络通道→DEC_ARM；on_price(tick)/check_bar(bar仿真,跳空按开盘价)触发；pending抑制+confirm重武装+reject_pending当日撤武装）；过滤器武装时前置裁决、风控触发时复核、S1 skip在触发时判定（sim单由价格推进）；快照v4（armed/pending/N/通道持久化，v3兼容）；StraTurtle接线（tick订阅+bt日线check_bar+session_end当日m5聚合合成日K评估，F3缺口消解——信号不再依赖d1闭合）；**未用框架条件单**（CtaStraBaseCtx.cpp:981 每次on_calculate清空条件单+自动执行无法承载触发时过滤语义）；rb双基线对照（同填充模型纯信号语义A/B）：close 6.9%/回撤9.9%/胜率27.8% vs trigger 6.2%/回撤11.3%/胜率30.0%（293笔）；测试 165→188
- [x] P0 拓扑改造（2026-08-31）：deploy_tpl 新增 runDt.py(datakit: ParserCTP→K线合成→UDP广播3997/9001)+dtcfg.yaml+mdparsers.yaml(CTP凭据移至数据侧,入keep-secrets)+logcfgdt.yaml；交易侧 tdparsers.yaml 改 ParserUDP(CTP留注释回退块)；make_deploy 同步+dtcfg路径绝对化；init_monitor_db 双进程调度(cta_turtle_dt+cta_turtle均guard)；测试 188→200
- [x] ~~P0 d1 实盘闭合~~（已由 trigger 模式消解：session_end m5聚合评估 + tick触发，无需 d1 on_bar）
- [x] P1 当日分区过滤（2026-08-31）：CsvDataLoader(csv_dir, stop_tdate) 丢弃>=当日行(deploy run.py 传自然日,夜盘保守口径)；convert_csv_to_dsb --stop-tdate；顺带修复两个潜伏bug——m5 time列原硬编码1500、WTSBarStruct字段名 volume/turnover/open_interest 被ctypes静默吞掉(vol/money/hold恒0,影响LiquidityFloor)；tests/test_partition_filter.py 8例
- [x] P1 F4 重放幂等回归（2026-08-31）：tests/test_replay_idempotent.py 3例（重启+信号bar重放无重复单/session_end重放快照逐字节一致/m5段重放_m5_key跳过）；锚点 armed/pending+_d1_date+_m5_key 均入快照v4
- [x] **首次实盘部署**（2026-09-01, 129.211.5.54 ubuntu@）：独立目录 `/home/ubuntu/projects/cta_turtle/`（与 futu_mm 生产部署完全隔离），venv=wtpy 0.9.9.3+全依赖；监控 8081 systemd 托管(token鉴权)+WatchDog 双进程(cta_turtle_dt/runDt.py + cta_turtle/run.py, guard)；SimNow 模拟账户(broker 9999)凭据服务器本地复用；**四个实测坑全记录于 DATA_OPS.md §5**：①hots.json 空规则→实盘 cacheIntegratedBars 空容器 segfault(用户定位方向正确; DataChecker live=True 已升级空规则为 error) ②fd 上限 1024→EMFILE abort(922合约×3mmap, systemd LimitNOFILE=1048576) ③C++ yaml 加载器对注释敏感(tdparsers 纯配置化) ④僵尸合约非法时间戳 tick→writer 写越界(contracts.json 裁至 11 活跃合约+disabletick/disablemin1)；主力实测(rt dmb 持仓量): rb2701/hc2610/l2701/pp2701 已写 hots.json；夜盘 21:00 后有待实盘 tick 流下复验
- [x] **合约信息每日更新管道**（2026-09-02）：`scripts/update_contracts.py` 包装 CTPLoader（子进程隔离——C++ dump 后 exit(0) 会带走宿主；种子合并保留手工字段；**SAVEPATH 必须绝对路径**否则写入嵌套目录，实测坑⑤；过期合约剔除=根治坑④僵尸合约；当日幂等+原子替换+.bak 备份；凭据 --cred tdtraders.yaml 或 CTP_* env）。触达双保险：deploy run.py 启动链 step0（WT_CONTRACT_SYNC=0 关，默认开）+ systemd timer `wt-turtle-contracts.timer` 每日 20:30。远端实测 848 合约/87 品种（SimNow 交易前置=**30001** 非 30201，坑⑥）；`scripts/ctp_loader/map_future.ini` 随包（[Session] 段覆盖 INE/SHFE 夜盘）；make_deploy 同步。测试 6 例
- [x] **12 品种多品种组合落地**（2026-09-02）：`scripts/fetch_data_sina.py`（akshare futures_main_sina 主力连续→BarJournal.merge_rows 幂等合并，SINA_SYMBOL 内置 27 品种映射）拉取 cu/al/sc/TA/MA/CF/pp/l/m/p/IF 日线（2005~2026 全历史）；commodities.json +10 品种（官方快照权威值）、fees.json +INE.SC（估算 20元/手平今免）、hots.json 12 键；turtle_config.yaml 12 品种 6 相关性组（black/metal/energy/cheme/oils/soft/equity）units=1，risk 调 max_total_risk_frac=0.10/max_units_total=6；run_matrix 2019-01~2026-09：**组合等权 +58.5%/回撤 28.5%/sharpe 0.34**，cu(+367%)/CF(+275%)/al(+150%) 主力，MA/pp/TA 拖累
- [x] **两个多品种实测 bug 修复**（2026-09-02）：①**equity 净盈亏口径**——`stra_get_fund_data(0)` 在 bt(CtaMocker.cpp:1883)与实盘(CtaStraBaseCtx.cpp:1711)**都是净盈亏(不含本金)**，RiskManager.equity 改为 fallback_capital+净盈亏（旧口径下首笔盈利后 equity 塌缩→全场否决，rb 基线 27% 胜率下盈亏常≤0 走 fallback 才未暴露）；②**bt 无 m5 品种 trigger 撮合链断裂**——C++ simTicks 只用非日K bar 模拟 tick（HisDataReplayer.cpp:1222 跳过 KP_DAY），日K 品种无 m5 时信号登记后永不成交；StraTurtle 加 `bt_no_simtick` 退化（check_bar 触发+收盘撮合），runBT/run_matrix 按 `{code}_m5.csv` 存在性自动注入。测试 221→227
- [x] **实盘 session_end 死锁修复**（2026-09-03）：交易侧无 statemonitor，session 事件由 WtCtaRtTicker 驱动且要求行情时间推进过收盘点——白盘 15:00 后无 tick → session_end 永不触发 → 策略整日未武装空跑。修复：`on_tick` 新交易日自愈评估 `_catchup_eval`（首 tick 检测 his 末根未评估→纯 his 评估武装，每日一次 `_catchup_date` 闸，bt 用 is_backtest 闸保护口径；顺带消解冷启动空跑）。远端重启实测 `his last=20260902 armed L=3203/S=2968` 与独立复算逐位一致。测试 227→231
- [x] **12 品种实盘上线**（2026-09-03）：数据 rsync+DSB 重建；`scripts/pick_hot_sina.py`（新浪 futures_zh_spot 持仓量判定主力，CZCE 3→4 位码 expiredate 补年、合约码按 WT 原生格式写回 contracts.json 键口径）生成 13 品种 hots 映射；fees 补 INE.SC；datakit 重启加载 848 合约全订阅（四所 tick 正常）；12 品种 catchup 武装与复算逐位一致（含当根包络口径）；**实盘首单**：sc 多 2 手@678.4、MA 多 53 手@3160（风控缩量精确生效；catchup"补票"滑差特征已录 DATA_OPS §8）；图表 API 非 schedule 防护（_safe_mark/_in_schedule 闸+mark=False）。测试 231→236
- [x] **上线日志审计修复两项**（2026-09-03）：①**C++ SimpleRiskMon 参数空转**——config.yaml riskmon 段误用 params 嵌套层（C++ getDouble 在 riskmon 直接子级读，官方 demo 为平铺+数字不带%+inner_day_active 开关），日志 "Capital: 0.0 OFF(0.00%)" 实锤，改平铺后 "MaxIDD: ON(20%) Capital: 5000000" 生效；②**监控字段空白**——venv 缺 websockets 库，前端 WS 实时通道握手失败（"Unsupported upgrade request" 刷屏），pip install websockets+重启监控即恢复；REST 数据链（generated/outputs/*.csv→DataMgr）验证正常。另：session_end 当日 15:15 正常触发（848 全订阅后国债 tick 覆盖 15:15 收盘点，与 catchup 双保险互补）；sc/MA 入场 implied_fill 记账价 vs 实际成交均价差 ~0.3N（分批成交窗口 fallback 触发价，add/stop 参考点偏差可接受，已录）
- [x] **P1 成交均价回写**（2026-09-04）：TurtleEngine.reanchor_last_price（修最后一层价+首层同步 s1_last.entry_price+trigger 模式 _rearm 跟随）+ StraTurtle._reanchor_actual_fill（confirm 时挂 _fill_pending 标记，持仓量达目标后从 avgpx 增量反推最后一层实际价，偏差>0.5tick 才修正；双系统反向净额化无法归因则跳过；不持久化，换月 re-anchor 兜底崩溃窗口）；调用点=on_tick（秒级）+_post_calculate（bar 级）；bt 实测 132 次回写——**证实 bt 撮合也是延迟的**（confirm 时 fallback 触发价，下根 bar 修正为收盘价），rb 基线 32.8%→26.9% 属记账口径修正漂移；与 _reanchor_on_roll 协同（先精确修末层，换月检测不误报）。exit 价不回写（输赢判定边界情形可接受，文档已注）。测试 236→240
- [x] **docs/GUIDE.md 使用指南**（2026-09-04）：监控页面使用（数据链路/排障/重启影响）+ 策略业务逻辑（信号系统/武装触发状态机/过滤链/双层风控/执行记账/持久化自愈）+ 全配置参数说明（turtle_config 三段+config.yaml riskmon+调参速查）
- [x] **夜盘审计修复三项**（2026-09-05 凌晨）：①**_last_n 重启后 NaN 静默丢触发**——策略层 N 缓存不持久化，重启~下次日评估窗口内 `_exec_trigger_decs` 的 isfinite 闸静默丢弃全部触发决策（实测 sc 穿越 add 位 688.32 无响应）；修复=on_init 从引擎快照 `_n` 同步 `_last_n` + NaN 丢弃改告警日志；②**修复①后暴露 bt 重放重复单**——test_replay_idempotent 原靠 _last_n=NaN 意外通过，真问题是 `_run_trigger_bar` bt 日线触碰仿真无 bar 日期去重，重启重放同根 bar 时恢复的 add 武装位被二次消费；修复=触碰仿真加 `d_cur > _d1_date` 去重（bt 基线 26.9% 不变）；③**组合风控账本重启丢失**——RiskManager._book 进程内存态，重启后持仓期间总敞口/单位数约束被低估；修复=on_init 恢复/收养持仓后 `on_rebuilt` 重建（远端实测 "risk book restored: sc dir=1 lots=2 avg=674.70 / MA lots=53 avg=3148.00"）。另修正路径盲区：`_reanchor_on_roll` 原仅挂 on_calculate（实盘 d1 主K不闭合=死代码），已补挂 `_session_eval_daily`/`_catchup_eval` 日评估路径（sc 记账 674.7 vs 实际 674.9、MA 3148 vs 3150 的偏差待次日 session_end 自动平移）。测试 240→244
- [x] **P0-1 hots 主力映射每日自动更新**（2026-09-05）：`scripts/update_hots.py`——复用 pick_hot_sina 纯逻辑，**query_holds 重构为 stdlib 直连 hq.sinajs.cn**（去 akshare 重依赖，生产 venv 无 akshare；parse_hq_text 纯函数：CF hold=第13列/FF=第6列，实测字段数 44/50 对照 akshare 列定义，无效代码空 payload 静默跳过）；变更检测（before/after diff）→ merge_hots 原子替换+.bak → kill datakit/run.py（WatchDog 拉起，hots 变更须重启生效）→ webhook 告警；systemd `wt-turtle-hots.timer` Mon-Fri 20:35（在 contracts 20:30 后、夜盘 21:00 前安全窗口）；远端实测 dry-run "13 品种无变更" ✓；make_deploy 同步两个脚本。测试 8 例
- [x] **P0-2 关键事件告警接入**（2026-09-05）：`_veto_log` 首见同步 `_alert("veto_"+key)`（风控否决/缩量不再静默）；trigger 决策丢弃（N 缺失窗口）告警；**fill_timeout**——`_fill_pending` 挂 ts，分批窗口 >600s 未达目标仓位告警一次（执行单元放弃追单/涨跌停无法成交的可观测代理），不阻断继续等待。**生产 notify.enabled=false 待用户配 webhook URL**。测试 244→254（含 veto 告警去重/fill 超时只报一次 2 例）
- [x] **孤儿 pending 事故修复（P0 级，2026-09-07 白盘审计发现）**：9/4 旧代码 `_last_n`=NaN 静默丢弃触发决策时，引擎 `_emit_*` 已先置 pending → 决策丢失后 pending 无人 confirm/reject → 经快照 v4 持久化/恢复永久挂起 → `on_price`/`check_bar` 的 pending 闸吞掉后续**全部触发含止损**（持仓裸奔）。实测 4 品种受害：sc/MA add 决策丢失（9/7 sc 盘中 709 穿越 add 位 688.32 无响应）、TA/m enter 决策丢失（错过入场）。**三层修复**：①`TurtleEngine.clear_pending()` + on_init 恢复时清除孤儿 pending（武装位保留→价格仍在位上方下 tick 立即补票；已成交未 confirm 的边界由持仓对账/reconcile 兜底）；②DEC_ADD lots=0 否决补 `_reject_trigger`（与 enter 分支对齐，dual 路径原本无漏）；③日评估三路（`_on_calc_daily`/`_session_eval_daily`/`_catchup_eval`）评估前 `_drop_stale_pending` 跨日自愈。重启实测 4 条 "drop orphan pending" 告警。测试 254→257。**教训：pending 语义="决策已发出待确认"，任何决策丢弃路径都必须同步清 pending；日评估/重启是天然的自愈闸点**

## 4. 开发约定（给后续代理）

1. 修改信号逻辑必须先读 `tests/test_turtle_engine.py` 的口径注释，改完跑
   `python3 -m pytest tests/ -q`（在 demos/cta_turtle 下）；
2. 引擎保持纯逻辑零框架依赖；框架交互只进 StraTurtle/filters 的 init 阶段；
3. 过滤器禁止修改引擎状态，只能返回放行与否决原因；
4. 所有新配置字段必须在 `schemas.py` 定义并在 `ConfigLoader.load` 校验（未知字段报错）；
5. 回测口径变更（成交价/滑点/撮合时机）需同步更新 README「与经典海龟的偏离」小节。
