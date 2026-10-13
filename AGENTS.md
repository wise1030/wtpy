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
- [x] **P1 清单四项落地（2026-09-15）**：①**同 bar 出场结清 S1 过滤器（B2）**
  ——close 模式 `on_bar` 在同一次调用里既产出出场又评估入场, 而 `s1_last`
  要等 `confirm_exit` 才更新; 两层多仓均价 115/通道出场 112 属 **loss**, 却被
  读成"上一笔盈利" → 误 skip, 且 `_record_sim` 抢先覆盖 taken 记录 → 真实输赢
  永久丢失 (复现: decisions 由正确的 `[exit,enter]` 变成 `[exit,skip]`)。修:
  出场决策产出即 `_settle_s1_exit()` 结清 (两段式第一阶段), `confirm_exit` 幂等。
  ②**双系统 re-anchor / 账本口径（B5/B6）**——`_reanchor_on_roll` 原按首子系统
  层数比对净仓, 两子系统同持时恒不等 → 换月漂移永不修正 (B5); 改为全持仓子系统
  按净成本口径汇总后统一平移 (反向并存也成立)。`_sync_risk_book` 原用当根 N
  覆盖账本, 与单系统"逐层按加仓时点 N 记账"不一致 (B6); 新增
  `TurtleEngine.booked_risk()` 逐层按冻结 N 汇总, `RiskManager.on_rebuilt/
  sync_position` 支持显式 `risk=`, 启动重建/对账重建/批量同步三处统一。
  ③**`run_matrix --weights` 静默清零（B1）**——`parse_weights` 产出短名键, 直接
  传给 `merge_curves` 全部落空 → 组合曲线恒 0 (看起来像"策略不赚钱")。修:
  新增 `resolve_weights()` 短名→全码解析 (fail-fast: 未匹配/歧义/非数值直接报错),
  main 在跑回测前先校验, `merge_curves` 对权重全落空改为抛错。
  ④**共享账户可用资金闸门 + 执行单元卡死自愈**——真值源 `funds.available`
  (实测 4.37M vs 策略侧 fallback 5M, 账户被多策略共用) 接入风控:
  `risk.account_gate_mode` off/warn(默认, 只告警)/veto + `account_reserve_frac`
  安全垫, 缩量优先 → 对齐 9/9「资金耗尽 p 追单 3357 次被拒」事故防线。
  执行单元卡死源码级定位 (DATA_OPS §9): 单元懒创建 + `WtLocalExecuter::on_tick`
  对未创建单元直接丢 tick (`:444-449`) + 单元只接受严格同码 tick
  (`WtMinImpactExeUnit.cpp:197-199`) → `_last_tick` 恒 NULL → `:315` "execute
  later" 永久静默; 新增 `WT_EXEC_RECOVER=restart` (opt-in, 真值源新鲜才触发,
  每自然日一次 marker 闸) 自愈重启, 默认仍只告警。测试 294→316。
- [x] **P0 三项修复：N 口径 / 脏状态守卫 / 交易通道真值源（2026-09-15）**：
  ①**止损-加仓 N 口径（P0-2）**——旧实现每 bar 用当根 N 重算止损，N 放大
  时止损被动放宽，实际止损亏损可超 RiskManager 记账的 2N（rb 日线 4105 根
  实测：持有 20 根 N 比值 p95=1.40、极值 2.14 → 单层实际亏损达预算 2.1 倍），
  N 缩小时加仓位又下移造成"价格未推进也加仓"。新增 `base.stop_n_mode`
  三口径：`entry`（冻结入/加仓时点 N，纯经典）/`current`（当根 N，旧行为，
  仅 A/B 回退）/**`ratchet`（默认：当根 N 但只收紧不放宽，两次加仓之间单调、
  加仓时按新单位重锚）**；引擎侧 `layer_ns` 逐层冻结 N + `_stop_level/_add_level`
  棘轮位（入快照/随换月平移）+ `update_n()` 显式刷新最近评估 N（close/intrabar
  路径不刷 → 止损静默 NaN）。**组合矩阵实测（12 品种 2019-01~2026-08 等权，
  2026-09-15 重测 — 首测系边改代码边跑，数据失真已作废）**：entry
  49.9%/回撤29.5%/sharpe0.29、current **56.9%/27.3%/0.33**、ratchet
  54.9%/28.5%/0.32（rb 单品种 ratchet 32.9%/35.3%）。三者相差 ≤7pp 且排序
  不稳定，默认取 ratchet **不是为了收益，而是风险口径唯一自洽**（current 的
  止损随 N 放大被放宽，实际亏损可超记账 2N）。
  ②**脏状态守卫 + 收养符号修复（P0-1）**——`direction≠0 且 layers 空`（脏/旧快照）
  时 on_bar/`_stop_price`/`confirm_exit` 不再 IndexError/ZeroDivision（止损返回
  NaN 不误触发）；**on_init 收养孤儿空头持仓时 avgpx 未取绝对值** → 层价写成
  −3460 → 止损落到价格另一侧、下一根 bar 即被"止损"平掉真实持仓，改为 `abs()`
  + 无价时不硬收养；引擎新增 `adopt_position()/flatten_book()` 统一入口（避免
  layers 与 layer_ns 两表脱节）。③**交易通道真值源（P0-3）**——源码复核确认
  策略侧 `stra_get_position/avgpx/fund_data(3)` 全是**信号口径**
  （`stra_set_position`→`append_signal` :1354，下一 tick `on_tick` 消费信号并
  直接改写持仓簿+累计手续费 :811/:1469），fees 代理在源码口径下"信号落地即计费"
  （fail-open），费率 0 品种（实测 INE.SC）又永久疑似幽灵（fail-closed）；
  新增 `helpers/LiveTruth.py` 读交易通道落盘的 `generated/traders/<id>/rtdata.json`
  （真实持仓+账户资金，每根K收盘与每笔成交后刷新；远端 2026-09-14 实测在更新），
  接入 on_init 收养 / 运行时对账 / 成交确认三处：真值仓未达目标 → 不回写 avgpx
  且 `ghost_fill` 告警，真值缺失回退 fees 代理并超时放行（`fill_timeout`）；
  配置 `base.truth_json`（空=自动探测，off=关闭，回测自动降级）。测试 260→294。
  **顺带发现（待办）**：共享账户真实资金可见（rtdata.json funds: balance 23.36M/
  available 4.37M/margin 18.29M，含其他策略持仓）→ 可用它做"账户可用资金"闸门，
  对齐 AGENTS P0-3「共享账户资金协议」
- [x] **生产三日审计与执行链路修复（2026-09-08~10）**：①**RiskManager 旧版未部署事故**——远端 equity 口径停在 8/24 版（fd>0 时权益塌缩为净盈亏几万），sc/MA/cu/al 的 add 全部被 cap=0 误否决 3 次，9/10 凌晨部署修复版后放量恢复正常（al 37 手入场）；②**执行单元非确定性卡死确诊**——盘中新建 MiniImpactExecUnit 大概率 tick 断供（"No lastest tick data" 后永久静默，cu/al 卡死一天、MA 幽灵 add 4 手），启动早期（channel ready 事件前后）创建的单元正常；**框架信号仓全面屏蔽真实成交**（stra_get_position/avgpx/组合层 positions 全是信号口径，Trades 日志按交易日切分加剧误判）；③**F1 执行单元预热**——on_init 同值 set_position(tag="PreWarm") 强制预建 12 品种执行单元（diff=0 不下单，bt 跳过保持基线）；④**F2 幽灵仓位检测**——fees（stra_get_fund_data(3)）作为真实成交代理
（**勘误 2026-09-23**：此处"只随真实成交回报累积"记错——9/15 源码复核
证实 fees 在 do_set_position 信号落地时累加（CtaStraBaseCtx.cpp:1469），
是**信号口径**；该错误口径正是 9/21 CF/p 秒确认幽灵事故的成因之一，
证据链已由 4be7512 重构接管）：fill_pending 挂起时记 fees0，信号仓达标但 fees 未增 → 禁止假 avgpx 回写 + 600s 超时 ghost_fill 告警；⑤**F3 veto 全量入参**（equity/want/rpl/vs）；⑥**运维教训**：手工清状态必须同时清 stradata 的 signals 段（残留信号 57 重启后被消费导致 MA 计划外加仓 4 手@3465，已追认进引擎 layers）；MA 幽灵层曾把止损位抬高 345 点（3285 vs 正确 2940）。测试 257→260。**待办**：执行单元卡死根因（wtpy 0.9.9.3 libWtCore.so 与源码差异排查/升级）、共享账户资金协议（P0-3，9/9 上午资金耗尽 p 追单 3357 次拒单实锤）、p 记账价偏差 71.5 点校准

## 4. 开发约定（给后续代理）

1. 修改信号逻辑必须先读 `tests/test_turtle_engine.py` 的口径注释，改完跑
   `python3 -m pytest tests/ -q`（在 demos/cta_turtle 下）；
2. 引擎保持纯逻辑零框架依赖；框架交互只进 StraTurtle/filters 的 init 阶段；
3. 过滤器禁止修改引擎状态，只能返回放行与否决原因；
4. 所有新配置字段必须在 `schemas.py` 定义并在 `ConfigLoader.load` 校验（未知字段报错）；
5. 回测口径变更（成交价/滑点/撮合时机）需同步更新 README「与经典海龟的偏离」小节；
6. trigger 决策的 confirm/reject 必须与决策产出在**同一 on_tick 内同步完成**——
   `_drop_stale_pending`（日评估）与 on_init 孤儿清理都**无条件** clear 引擎
   pending，依赖"pending 不跨 tick"不变量；引入异步确认必须同步给这两处加
   时效判断（详见 docs/DATA_OPS.md §11.2）；
7. fees 是**信号口径**（`CtaStraBaseCtx.cpp:1469`，信号落地即计费）——任何
   把 fees 当真实成交代理的新代码必须先过 `_truth_stale_now()` 陈旧闸，
   否则复现 2026-09-21 秒确认幽灵事故。
- [x] **实盘部署 + 真值用途收窄为 alert（2026-09-15/16 夜盘）**：把 P0/P1/P2 全部
  改动部署到 129.211.5.54（`deploy/cta_turtle/`，rsync 后逐文件 md5 + helpers 全目录
  清单哈希校验一致；备份 `~/wt_turtle_backup_20260915_2332.tgz`），重启 runDt/run.py
  （WatchDog span=10 自动拉起），验收：12 策略 inited、引擎状态/风控账本恢复、
  真值源生效（登录后 age=0.0s）、0 traceback、重启未产生任何成交。**期间发现并
  修复两件事**：①`truth_max_age` 生产取值——rtdata.json 只在登录/每笔成交/
  on_calculate 末尾刷新，d1 主K不闭合时无成交就长时间不更新（实测 8h），但持仓
  只随成交变化、trader 每次成交后都重查账户 → **内容仍准**，故生产设 86400s
  （1800s 会把真值功能整段禁用）；②**真值不可按绝对持仓量用于重建**——券商持仓是
  **账户级**，共享账户下含其他策略仓位（实测 CZCE.TA 账户 112 vs 本策略 64；
  CF 则是 20:59 那笔 `add layer2 20 lots` 只在引擎确认、券商未成交 = 执行单元卡死
  幽灵层），故新增 `truth_rebuild_mode`：**alert（默认，只观测告警，差额发
  `truth_mismatch`）/ rebuild（仅独占账户时用于重建/收养）**；同时把成交确认从
  绝对量改为**增量口径**（下单窗口内账户持仓增量 == 本层手数，对共享账户免疫，
  比值口径在共享账户下会永久误判"未成交"）。测试 326→327。
- [x] **实盘运行复盘 + P0/P1 两项修复（2026-09-16）**：复盘确认此前修复全部正确
  （持仓收敛 CF -65=券商 -65、TA 64=64；alert 模式生效且避免了按瞬时值 112 误重建；
  CF 触发 `avgpx drift re-anchor: shift -69.72`；全天无新 `No lastest tick data`/
  ghost_fill/orphan pending；15:15 日评估正常、20:59 夜盘正确拦截加仓；数据服务不中断）。
  **复盘中发现两个更严重的问题并已修**：
  ①**P0 数据管道：主力连续日线自 2026-09-01 冻结** —— 数据底盘 (WtDt) 每日只写
  **真实合约**日线（`his/day/<EXCHG>/<contract>.dsb`，16:00 会话轮转后落当日 bar），
  而策略代码是 `XXX.HOT`（读 `XXX_HOT.dsb` / `XXX_HOT_d.csv`），两者无自动衔接
  → 策略的 N(ATR20)/20/55 日通道一直用陈旧窗口（硬证据：`INE.sc` armed exit=581.8
  恰为 HOT 文件 2026-08-28 最低价，干净最新 10 日窗口最低是 665.4；另发现真实合约
  DSB 因数据底盘重启重复追加同日 bar，直接进窗口会双计）。修复：新增
  `scripts/update_hot_daily.py`（按 hots 当前主力 + 按日期去重 → 增量并入
  `storage/csv/<code>_d.csv` → 重建 HOT DSB；幂等、异常不阻断交易）+ 一次性回填
  + `wt-turtle-hotdaily.timer` 每日 20:40（晚于 20:35 hots 更新、早于 21:00 夜盘）。
  ②**P1 风控账本口径把加仓全锁死** —— 全天 6 次入场/加仓全部 `total_risk(cap=0)`；
  根因是在途风险按"每层 2N（恢复时用当根 N）"记账 ≈52.7 万，而按**当前实际止损位**
  的真实最坏亏损仅 ≈20.4 万（4.0% 权益）→ 虚高 2.5 倍占满 10% 预算。修复：
  `TurtleEngine.booked_risk(stop=...)` 按止损位算最坏净亏损（锁盈层贡献为负、
  组合下限 0），策略三处（单系统 confirm、双系统同步、启动/对账重建）统一改走该
  口径；顺带修 legacy 层 `layer_ns=None` 在首次评估用当根 N 回填，并新增
  `eval window: N 根 [首~末] synth=?` 日志（正是这次无法即时判定数据是否陈旧的原因）。
  测试 327→336。
- [x] **删除 `_implied_fill`（信号账口径记账价）2026-09-15**：confirm 时不再从
  框架 `stra_get_position_avgpx` 反推成交价 —— 该接口是**信号账面**（`set_position`
  即改写信号仓，真实成交要等下一个 tick 落地），反推得到的是"新信号仓 × 旧成交
  均价"的混合值；加仓场景数学上直接退化为**加仓前的旧均价**（例：持 100 手
  @3400 再加 100 手，反推值 = 3400 而非真实 3500）。改为记账价 = 决策价
  （触发位/收盘价/tick 价），真实成交价统一由 `_reanchor_actual_fill` 回写
  （逐笔成交优先 → 净均价兜底）。**量化影响：rb 基线 32.9228%/314 笔与三口径
  组合矩阵（entry 49.9% / current 56.9% / ratchet 54.9%）逐位不变** —— 纯简化，
  但消除了实盘加仓记账价的系统性偏差来源。测试仍 326。
- [x] **P2 清单落地（2026-09-15，不含 StraTurtle 四模块拆分）**：①**成交价回写
  改走逐笔成交（`generated/traders/<id>/trades.csv`）**——原口径只有"净均价"一个
  方程，多层待回写时欠定（N 个未知层价），且 `_fill_pending` 单槽会被后确认的
  子系统覆盖；现按子系统分槽 + 记录**逐笔成交游标/本层手数**，每层取自己的真实
  成交均价（同码多笔按确认顺序分配成交量，总量与总均价因此精确；撤单行 CNL
  剔除）；净均价反推降级为兜底（仅在单层未知时用）。②**同 bar 出场结清 S1
  过滤器（B2）**、③**双系统 re-anchor/账本口径（B5/B6）**——详见下条 P1 记录；
  ④**`--weights` fail-fast（B1）**；⑤**否决日志逐 key 冷却 + 累计次数**（原全局
  单槽在不同原因交替时刷屏、成功入场还重置）；⑥**`_calc_indicators` 去重复计算**
  （实测单次 512µs、12 品种×288 根 m5≈1.8s/日，收益低于缓存键错配风险故不加
  记忆化）；⑦**journal 旁路补 turnover**（原回补把 money 列写 0）；⑧`runLive
  --config` 未传给回补子进程、`run_matrix` serial 模式提醒、GUIDE 里"当前配置
  不加仓"错误（实测 rb 基线 111 次加仓）等小项。测试 316→326。
- [x] **P0 级新发现：陈旧真值源污染决策（2026-09-15，随 P2 修复）**：策略对账/
  成交确认原先只判"真值可读"，未判时效；环境中残留的过期 `rtdata.json`
  会被当作真实持仓 → 触发错误的重建/平仓（实测：回测目录里 22 小时前的样本
  文件把 cu 基线从 **335% 拉到 76%**，并导致一次三模式矩阵对比全部失真）。
  修复：`_truth_position` 加时效闸门（`base.truth_max_age`，默认 1800s）→ 陈旧
  即回退框架口径 + `truth_stale` 告警一次；`ChannelTruth` 文档明确"net_position
  不做时效判断，决策前必须查 stale()"。**教训：任何外部真值在用于破坏性动作
  （重建/平仓/资金闸门）前必须先过时效闸门。**
- [x] **全面评估 + 全量修复落地（2026-09-20，评估报告驱动的 P0/P1/P2 清单）**：
  对架构/业务/代码/配置/运维五维评估后执行全部修复，**rb 双基线 A/B 对拍
  逐字节一致**（重构零漂移验证）。清单：
  ①**P0-3 ghost_fill 自愈**（CF 34 手幽灵仓事故处置动作化）：新增
  `TurtleEngine.trim_to_lots()`（尾部裁剪/只缩量/不动 S1 过滤器）+
  `StraTurtle._maybe_ghost_trim()`（ghost_fill 触发后自动把信号层裁到与
  券商同向且不超过它的手数；跨子系统从尾部裁；`base.ghost_auto_trim`
  开关默认开）；②**P1-8 StraTurtle 收敛**：提取 `_eval_core()`（on_calc/
  session_end/catchup 三评估路径此前近重复 — 历史上 reanchor 曾漏挂一条）
  + 合并 ENTER/ADD 门控链为 `_size_and_cap()`（否决 key/日志口径逐字保持），
  净减 ~150 行；③**P1-6 配置加固**：顶层段/instruments 项未知键拒绝
  （`untis` 之类 typo 不再静默回默认）、units 拒绝非整数值、
  `config_version` 字段、加载时打印敏感行为口径（entry_mode/stop_n_mode/
  ghost_auto_trim/gate/notify）；④**P1-4 数据时效断言 + 定时器失败告警**：
  `DataChecker.check_staleness()`（按 holidays.json 日历推导"数据应到哪天"，
  滞后 warn — 2026-09 HOT 冻结 16 天预检无感事故的检测项）接入 deploy
  run.py preflight；新增 `scripts/timer_notify.py`，三个定时器
  （contracts/hots/hotdaily）全部失败路径推 webhook；⑤**P1-7 hots 双源**：
  update_hots 增加 tqsdk 兜底（sina 不可达自动切换 TQ_USER/TQ_PASS 按
  open_interest 判定）；⑥**P2-10 涨跌停锁死检测**（有界版）：tick 价钉住
  >`limit_lock_secs`(默认120s) 且有未闭合成交窗口 → 告警 + 暂停加仓
  （出场/止损不受影响，`_detect_limit_lock`）；⑦**P2-11 fund_live.csv
  旁路**：`_post_calculate` 每 ≥60s 增量落盘日内资金（框架 fund.csv 仅
  session_end 写入的盲区）；⑧**P2-9 部署链加固**：make_deploy 生成
  `deploy.manifest.json`（sha256 清单，凭据文件只记名不记哈希）+ `--verify`
  漂移校验；9 个 systemd unit 文件入库 deploy_tpl/systemd/（含
  install_units.sh 渲染安装，@PREFIX@/@MON_TOKEN@ 占位）；⑨**P2-12 CI
  强化**：pytest-cov 门槛 70%（helpers+filters+scripts）、ConfigLoader
  覆盖率 70%→90%（22 条例校验分支正反测试）。测试 326→**375**。
- [x] **生产三日审计: 真值链死亡/幽灵仓/评估闸门三重根因修复（2026-09-21~23）**：
  对 9/21 周一盘中"商品策略零活动"的追查推翻了初始假设（tick 断流），源码级
  定位出三个叠加的真实根因并全部修复（commit 92fef8d/4be7512）：
  ①**P0 真值链永久死亡 + fees 代理 fail-open 误确认**——`rtdata.json` 只在
  `TraderAdapter::onRspAccount`（账户查询响应）时写入；CTP 每日 07:0x 例行
  断线(4097)重连后初始化查询链遇"查询未初始化"错误(错误90) → 状态机卡死在
  `AS_ALLREADY` 之下 → `queryFund()` 永久 no-op → 真值自 9/17 起冻结。叠加
  **fees 代理是信号口径**（`CtaStraBaseCtx` 信号落地即计费）→ `_fill_confirmed`
  的 fees fail-open 把未成交**瞬间误确认**：9/21 20:59 CF 加仓 47+47 手、
  p 入场 29 手全部未达柜台且**零告警**（truth_mismatch 因真值陈旧也不触发）。
  修复：`_fill_confirmed` 证据链重构为 **真值增量 > 逐笔成交 journal（trades.csv
  只随真实成交回报增长，独立于真值链路）> fees 代理**，且真值已配置但陈旧时
  fees 增长视为未验证（600s fill_timeout 告警 / 2x 超时放行，决策价兜底）。
  ②**P1 catchup 每日闸被首 tick 数据竞态消费**——`on_tick` 原先在调用
  `_catchup_eval` **之前**就落 `_catchup_date`：9/21 09:00:00 整 11 个商品策略
  首 tick 读 bars 未就绪（bars<need 静默 return），闸已消费 → 当日不再自愈；
  IF 因首 tick 在 09:29（reader 就绪）侥幸成功。修复：闸门消费权内移到
  `_catchup_eval` 内部，只有结论性出口（已是最新/评估成功/模式禁用）才落闸，
  暂时性失败下一 tick 自动重试。
  ③**P1 WtDataReader 缓存使 hotdaily 重建不可见**——C++ reader 按
  (code,period) 缓存 his 块、无失效 API：9/18(五) 20:40 重建的 DSB 对运行中
  进程不可见 → 周五夜盘 catchup 静默跳过（his 末根冻结在周四）→ 商品通道缺
  一根 bar。修复：`update_hot_daily.py` 重建且增量>0 后 kill run.py
  （`WT_HOTDAILY_RESTART=0` 可关）——每日 20:40 重启一石三鸟：reader 刷新、
  执行单元重置（已知卡死 bug 的日线级自愈）、CTP 重登复活真值查询链。
  ④**谜之老合约执行单元破案（无需修复）**——9/21 09:01:20 创建的
  sc.2610/MA.1610/al.2610/IF.2609 四个单元来自 `WtCtaEngine::on_schedule`
  （每根分钟线收盘）枚举组合持仓簿时对**残留的过期合约代码**提交目标 0
  （源码注释原话承认"组合持仓里会有过期的合约代码存在"），`getUnit`
  自动建单元、diff=0 无订单、过期合约永无 tick 自然饿死——设计上无害。
  **教训**：①真值/证据的降级路径必须与降级原因匹配——fees fail-open 只在
  "环境无真值"时成立，"真值已配置但链路死亡"时它从兜底变成谎言；
  ②一次性闸门（once-per-day）不得在暂时性失败路径上消费；③外部数据重建
  后必须考虑读取方缓存（C++ reader 无失效 API 就用进程重启）。
  **后续验证**：9/22 20:35 hots 换月重启后新单元按柜台同步内部仓位，周一
  挂起的 CF -94/p -29 在途增量**自然收敛**（truth_mismatch 告警链 -93→
  -140→-159 完整记录成交到达）；9/23 早 rtdata 恢复分钟级刷新；修正后的
  确认链与 catchup 重试闸当晚 20:40 重启后生效。测试 375→**383**。
- [x] **文档体系成型（2026-09-23）**：全面检查后产出 `docs/OPS_MANUAL.md`
  （策略配置与运维手册：系统拓扑/配置管理 SOP/盘前盘后巡检/告警处置表/
  应急手册/变更检查清单）+ 重写 `README.md`（系统架构图、模块职责、三层
  防御、文档索引；修正过时数据：测试 326→383、"220 单测"→38 文件、
  文件结构补 6 个新脚本与 LiveTruth.py）；`GUIDE.md` 告警表补
  `hotdaily_restart`。配置权威源=schemas.py 口径已在手册固化。
- [x] **周一晨柜台连接失败事故处置 + 盘前定时重启（2026-09-28）**：
  ①**事故**：中秋休市(09-25)~周末后周一 08:41，交易通道早晨重连撞上 SimNow
  交易前置启动窗口（"用户未初始化"→"前置不活跃"），自动重试两次放弃 → 通道
  永久挂起，人工 08:50:58 重启后 08:51:10 登录成功，开盘前恢复零交易影响；
  **根治**：新增 `wt-turtle-premarket.timer`（Mon-Fri 08:47 pkill run.py，
  WatchDog 拉起重登，pkill 用锚定正则防 self-match），入库 deploy_tpl +
  install_units；②**监控"获取不到日志"排查为误报**：链路全程健康（WS 7 接入、
  IPC 08:50:58 已重订阅），空白系 08:51:14→09:00 开盘前无 tick 的策略日志
  静默窗；③**附带修复**：`CFFEX.IF.HOT_m5.csv`/`SSE.ETF.510300_m5.csv` 为
  2 月遗留 MetaTrader 格式头（`<Date>`），每次启动 IF m5 预载 KeyError
  （走 stderr 故 Runner 日志不可见，09-22 起 4 次）；IF m5 实际走 DSB/rt
  正常路径（09-23 synth=yes 实证），坏文件移 .bak 根治噪音。
- [x] **商品行情全天中断事故 + 盈亏审计（2026-09-28）**：中秋后周一，datakit 行情
  通道在 08:41/08:42 前置启动窗口断线重连 → **商品所 session 状态错乱**（K线管道
  判 invalid timestamp 丢弃）——datakit ingest 计数正常（SHFE 300万 tick）但
  his/rt/UDP广播只落 CFFEX，**11 个商品策略全天零 tick**（fund_live 无盘中行 +
  `stra_get_price`=0 双实证；IF/CFFEX 免疫），止损/加仓全天失效（当日侥幸未触及
  armed 位）；dynprofit 冻结 → funds.csv 09-28 行=09-24 行 → **监控"今日盈亏"
  显示不准的直接原因**（盈亏公式本身正确：dynbalance=closeprofit+dynprofit−fees；
  次要口径差：信号 vs 真实成交 ≤0.5N 回写窗、共享账户柜台数含别家策略）。
  对照实证：9/23、9/24（晚间启动次日）商品 fund_live 全天 39~310 行正常。
  **处置**：盘后重启 datakit（18:07，MD ESTAB 30011 恢复）+ 20:40 hotdaily
  例行重启 run.py → 夜盘 21:00 商品 tick 全恢复（p 21:02 触发加仓 -60→-72 实证）；
  **premarket timer 扩展为双进程重启**（datakit+run.py，根治假期后早晨双故障）。
  数据缺口：周一商品 K 线未落盘（his/day 停 09-25）→ 明日通道窗口缺周一，
  周二 20:40 自愈；遗留建议：策略侧 tick 静默检测（session 内 N 分钟无
  on_tick → alert），当前全靠人工从盈亏异常发现。
- [x] **周一商品K线第三方回补（2026-09-28 夜）**：datakit 事故致 09-28 商品日线
  缺失（his/day 停 09-25），确认可用 `scripts/fetch_data_sina.py`（akshare
  futures_main_sina 主力连续 → BarJournal.merge_rows 幂等并入 HOT csv——与
  hotdaily 同源同格式管道）回补。执行链：服务器 12 份 csv 备份(.bak_20260928)
  → 拉本地合并（11 商品各 +1 行 09-28，IF 已有自动跳过）→ 三重校验（跳空
  ≤2.2%、量健康 6.7万~116万、与当晚实盘价交叉吻合；diff 证明纯追加）→
  回传 → convert_csv_to_dsb 重建（rb 4119→4120 根）→ 重启 run.py（4120
  载入确认，sc 夜盘 tick 即时恢复）。**换月口径风险**：新浪主力连续与
  hots.json 换月规则不同源，换月窗口日可能小幅跳空——本次经 09-24 收盘
  连续性+现价交叉验证无此问题；通用 SOP：事故日若紧邻换月须先比对
  sina 与本地末日收盘再合并。该路径已固化为数据缺口的官方回补方案
  （优于 tqsdk：免额外凭据/依赖，生产 venv 无 akshare 在本地拉取后回传）。
- [x] **保证金闸口径 bug 修复（方案B落地，2026-10-08）**——
  `RiskManager.cap_entry` 第 3 项保证金预算 = `单策略 equity×share×max_margin_usage`
  （≈3M） 减去 **组合全局账本 cur_margin**（CF+p+IF 三仓即 ≈3M 预估保证金）→
  CF 159 手满仓后（约 9/23 起）全体 12 策略的 margin cap 恒 ≈0-2 手，组合事实冻结：
  10-08 国庆后首日 al/MA/TA 三笔触发入场全被 margin(cap=0) 否决、rb 661 手申请
  被压到 2 手（数值回放复现：同参数空账本=165 手；带账本=2 手，与实盘逐字一致）。
  **修复（用户拍板方案B）**：①账本条目新增 `sid` 分组键（on_filled/on_rebuilt/
  sync_position 全链路透传，StraTurtle 6 处调用点传 `sid=self.name()`），
  `cur_margin` 只扣减本策略自身持仓——他策略重仓不再占满本策略保证金预算，
  组合总敞口/单位限制保持全局口径不变；②`fallback_capital` 5M→**15M**；
  ③`max_margin_usage` 0.60→**0.10**（每品种保证金≤自身权益10%；CF 159 手
  自身恰好顶满 150 万额度，自身加仓被正确拦截，他品种不受影响）。
  新增单测 `test_margin_scoped_per_strategy_planB`（含 10-08 事故场景回放：
  A 满仓 margin=0、B 空仓 cap=407 不再被压 0、total_risk 仍全局）；
  测试 383→**384** 全绿+ruff 通过；20:40 hotdaily 重启无缝加载（rb -2 手/
  risk=1209 正确恢复，0 error），夜盘起新口径生效。
  注：total_risk 仍是"单策略 equity×10% − 组合全局 used"口径（与 margin 旧 bug
  同构但 15M 基数下余量充足：used≈45 万 vs 预算 150 万），留作后续观察项。
- [x] **10-09 执行链路事故处置完成（2026-10-09 夜盘前）**——新风控口径首日
  验证通过（MA 194→145 / pp 155→116 per_market 缩量、IF margin cap=5 按策略
  口径精确生效），但执行层连环故障：①09:10:30 run.py 静默崩溃（无 error 日志，
  疑似 C++ 段错误，WatchDog 6s 拉起）消耗当日 exec recover 额度；②执行单元
  tick 断供（24 次 No lastest tick）→ pp 116 手入场单零派发（Trader 日志零
  记录，pp 当时快速拉涨午后封涨停加剧未成交）、IF 加仓零派发、MA 平仓零派发；
  ③ghost trim 对 broker=0 全幽灵不生效（pp 信号悬空至 233 手）；④trim 后
  同秒 re-add 死循环（IF 每 ~10min 一轮）；⑤MA 柜台裸多 95 手（vwap 3298.89）
  引擎已归零 = 无止损真实持仓。**修复落地**（测试 384→386 全绿）：`_maybe_
  ghost_trim` 支持 broker=0 全幽灵 → 整体 flatten；trim 当日经 `_ghost_blocked`
  封锁 enter/add 再触发（`_size_and_cap`+双系统两 gate 四出口，含
  `_reject_trigger` 拒武装）；旧测试"broker=0 不动作防误平"口径反转并注释
  理由。**快照手术**：MA 收养 95 手@3298.89（ud_ 引擎态 direction/layers/
  layer_ns/stop 3083.58/add 3406.55/armed + stradata 信号账双向一致）；
  pp/IF 交由新代码自愈（IF armed.long 持仓期=null 无再触发风险）。19:54:43
  kill -9 重启加载（MA risk book 95/204549 正确恢复，12 单元 init 期预创建
  执行单元 F1 生效，0 error）。**三个操作教训**：⑴pkill 模式必须匹配真实
  cmdline——WatchDog 拉起的是 `venv/bin/python run.py`（cwd=deploy 目录，
  cmdline 无 cta_turtle 路径），`pkill -f "cta_turtle/[r]un.py"` 静默不匹配
  → 本次用 `kill -9 <pid>`；⑵run.py 运行时**不会周期性回写** stradata/ud_ 快照
  （内存态为准，落盘在退出/特定事件时）——改完快照必须重启才生效，且重启要用
  SIGKILL 防旧进程退出时覆写；⑶订单零派发的第一判定=Trader 日志有无记录。
  待观察：pp 全幽灵 trim + 当日封锁将在 21:00 夜盘首 tick 后对账触发（届时
  fund_live position 应 233→0）；exec 单元断供根因（wtpy C++ 层）未修，
  F1 预创建+自愈重启仍是对策。
