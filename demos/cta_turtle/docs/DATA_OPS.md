# 历史K线数据运维方案 v2 — 基于 C++ 底层源码核验的完整方案

> 适用: demos/cta_turtle。v1 方案的"三层保障"仍有效，但经
> wondertrader/src 源码核验，修订/新增以下关键认知（标注源码位置）。

## 0. C++ 底层事实（源码核验结论）

| # | 事实 | 源码位置 |
|---|---|---|
| F1 | 实盘K线由**独立数据底盘(WtDt)**的 `WtDataWriter` 从tick合成，写入 mmap 文件 `rt/{min1,min5}/{exchg}/{code}.dmb`（仅当日，进程崩溃不丢，OS崩溃丢秒级）；**会话轮转时**才转存 his DSB | WtDataWriter.cpp:1340,1906,2037; WtDataReader.cpp:1948 |
| F2 | 交易引擎(WtRtRunner/WtDataReader)是**纯读者**：`get_kline_slice = 历史尾部(his/extloader) + rt当日块` 拼接 | WtDataReader.cpp:1541-1786 |
| F3 | **rt 只有 m1/m5，没有日线**：`getRTKilneBlock` 对 KP_DAY 返回 NULL；onMinuteEnd 跳过 KP_DAY（作者注释"实盘中日线是不会闭合的"）→ **实盘中 d1 既不触发 on_bar、get_bars 也看不到当日** | WtDataReader.cpp:1931,2003 |
| F4 | 交易引擎重启后 rt 游标归零，onMinuteEnd 会**从头重放当日全部 rt bar** → 策略必须幂等 | WtDataReader.cpp:2013-2040 |
| F5 | 会话轮转语义不对称：日线 DSB 按"末条日期"去重合并（同日不同值→替换，旧→跳过，新→追加，**自愈**）；min1/min5 DSB **盲目追加** rt 整块（若 his 已含当日会重复） | WtDataWriter.cpp:1893-1926,2030-2044 |
| F6 | ext loader（`register_ext_data_loader`）在历史加载中**只调用一次**/code#period（`_bars_cache` 缓存后不再重读）；HOT 连续合约先走 raw loader 组链 | WtDataReader.cpp:1552-1559,1478,1038 |
| F7 | 数据底盘重启：kline rt 块重新 mmap（当日bar保留）；tick cache 经 `loadCache()` 重建索引 | WtDataWriter.cpp:263 |
| F8 | 标准实盘拓扑 = **双进程**：datakit(WtDt, 直连CTP) + 交易引擎(ParserUDP/ParserShm 从底盘收tick)。**交易引擎进程内直挂 ParserCTP 时没有任何组件合成/落盘K线** → bar 事件永不发生 | demos/cta_fut vs datakit_fut; WtCore 全源码无 bar 合成器 |

## 1. 对本项目的四个判定

### Q1 盘前历史加载缺失如何补
- 加载路径：策略 on_init → `stra_prepare_bars` → WtDataReader 首次 `readKlineSlice` →
  ①ext fnl loader(我们=CsvDataLoader读storage/csv) → ②his DSB。二者皆无 → 空序列。
- 缺失分级与补法：
  - hots键缺失 → `prepare_data --fix-hots`（新增：可用 TqSdk `query_cont_quotes` 写真实主力码）；
  - 暖机深度不足 → `DataChecker.check_depth` 门禁 + TqSdk/第三方区间补数 → storage/csv；
  - 崩溃窗口尾部缺口 → BarJournal journal→csv 回补（v1 Layer2 不变）。

### Q2 第三方数据与既有数据补齐不重复
- **csv 层**：`BarJournal.merge_to_csv` 键值去重+只追加尾部（已实现，幂等）。
- **his DSB 层**：日线安全（F5 自愈）；**min 线必须遵守 his/rt 分区律——
  回补/转换写入 DSB 的K线必须截止于"当前交易日之前"**，当日K只属于 rt，
  否则轮转盲目追加导致重复（F5）。
- **rt 层**：第三方数据永远不写 rt（mmap 块由 WtDataWriter 独占）。
- **reader 层**（csv 暖机特有）：ext loader 结果与 rt 拼接（F2），
  csv 若含当日K → 与 rt 重复 → **CsvDataLoader 必须按交易日过滤掉 >= 当前交易日 的行**。

### Q3 盘中宕机重启的当天数据与无缝对接
- **数据底盘存活/重启**：当日K在 rt .dmb mmap 中持久（F1/F7），不丢。
- **交易引擎重启**：get_kline_slice 自动 = csv/DSB尾部 + rt当日块，连续；
  且 onMinuteEnd 从头重放当日（F4）→ 策略状态机靠快照(user_save_data)恢复 +
  m5 信号日期去重防重复触发（已实现，与 F4 语义正好匹配）。
- 残余窗口：OS 级崩溃丢 mmap 脏页秒级 → journal 兜底（策略已处理过的部分）。
- **致命缺口（本次核验新发现）**：当前 runLive 配置为单进程 ParserCTP 直连（F8），
  实盘根本不会有 bar 事件。必须改造为 datakit 双进程拓扑。

### Q4 d1 主周期实盘闭合（F3）——设计级缺口【已消解】
- 现设计：d1 主K on_bar 驱动状态机 + m5 盘中止损。回测可用（回测引擎合成日线），
  **实盘 d1 on_bar 永不触发**。
- **消解（2026-08-24, entry_mode=trigger 方案C落地）**：信号触发改由 tick
  驱动（引擎武装-触发状态机），日线评估由 session_end 用当日 m5 聚合合成
  日K完成——d1 on_bar 不再是信号链路的依赖项，d1 his 仅用于指标历史。

## 2. 目标数据拓扑（v2）

```
┌─ datakit 进程 (WtDtEngine, runDt) ────────────┐
│ ParserCTP(直连) → WtDataWriter                  │
│   → rt/{min1,min5}/{exchg}/{code}.dmb (当日mmap)│
│   → 会话轮转: 转存 his/{day,min5}/{exchg}/*.dsb │
│   → his day 自愈合并 / min 盲追加(分区律保障)    │
└──────────────┬──────────────────────────────┘
               | UDP/Shm tick 广播
┌─ trader 进程 (WtEngine, runLive) ─────────────┐
│ ParserUDP/Shm(tick) → 策略 on_tick/条件单       │
│ WtDataReader: ext loader(storage/csv, 过滤当日) │
│   + rt 当日块 → get_bars; onMinuteEnd 驱动 on_bar│
│ BarJournal: 每根消费K线旁路落盘 (崩溃兜底)       │
└──────────────┬──────────────────────────────┘
               | WatchDog 拉起前
┌─ 预检/回补 (prepare_data) ────────────────────┐
│ --fix-hots / --gate 深度门禁 / --repair-journal │
│ 第三方(TqSdk)区间补数 → storage/csv (幂等合并)   │
└──────────────────────────────────────────────┘
```

回测仍为单进程 WtBtEngine，读 his DSB（csv→DSB 经 convert_csv_to_dsb，
同样遵守分区律以免回放端与生成端口径错位）。

## 3. 实施清单（按优先级）

1. ~~**P0 拓扑改造**~~ ✅ (2026-08-31)：deploy_tpl 新增 runDt.py + dtcfg.yaml
   (broadcaster UDP 3997/9001 + WtDataStorage writer) + mdparsers.yaml
   (CTP 凭据移至数据侧) + logcfgdt.yaml；交易侧 tdparsers.yaml 改
   ParserUDP (CTP 留注释回退)；make_deploy 同步+dtcfg 路径绝对化+
   mdparsers 入 keep-secrets；init_monitor_db 注册双进程调度
   (cta_turtle_dt + cta_turtle, 均 guard)。
2. ~~**P0 d1 实盘闭合**~~ ✅ 已由 trigger 模式消解 (见 Q4)：session_end
   m5 聚合合成日K评估 + tick 触发，信号链路不依赖 d1 on_bar。
3. ~~**P1 当日过滤**~~ ✅ (2026-08-31)：CsvDataLoader(csv_dir, stop_tdate)
   丢弃 >= 当日行，deploy run.py 传自然日；convert_csv_to_dsb
   --stop-tdate 同规则；顺带修复 m5 time 列解析 (原硬编码 1500) 与
   WTSBarStruct 字段名 bug (volume/turnover/open_interest 被 ctypes
   静默吞掉 → vol/money/hold 恒 0)。
   ⚠️ 简化口径: 用自然日而非 calcTradingDate — 夜盘启动属保守过滤,
   当日日盘K由会话轮转后的 his 提供。
4. ~~**P1 重放幂等回归**~~ ✅ (2026-08-31)：tests/test_replay_idempotent.py
   3 例 (重启+信号bar重放/session_end重放去重/m5段整体重放跳过)，
   锚点: armed/pending + _d1_date + _m5_key 均入快照 v4。
5. ~~**P2 hots 增强**~~ ✅ (2026-09-01)：`scripts/fetch_data_tqsdk.py`
   --fix-hots 用 query_cont_quotes 写当前主力映射（默认仅空键，
   --force-hots 强制覆盖会丢弃历史换月表）；同脚本 K线获取走
   dmpBars 回调 → BarJournal.merge_rows 幂等合并进 storage/csv
   （与运行时 journal 回补同源版式/键逻辑）。

## 4. Runbook（v1 §4 仍然有效，增量：）

```bash
# 双进程启动 (生产: deploy_tpl/, WatchDog 托管; 本地手动:)
python3 runDt.py &                # 数据底盘 (ParserCTP→K线合成→UDP广播)
WT_PREFLIGHT=strict python3 run.py   # 交易引擎 (ParserUDP 收行情)

# 崩溃恢复判定: rt .dmb 文件保留当日K, 无需回补当日;
# journal --repair-journal 只补"OS级丢失+策略已消费"的交叉缺口, 天然幂等
```

## 5. 首次实盘部署实测记录 (2026-09-01, 129.211.5.54)

部署拓扑：远端 `/home/ubuntu/projects/cta_turtle/`（独立 venv: wtpy 0.9.9.3
+ numpy/pandas/scipy/pyyaml/fastapi/uvicorn/psutil/itsdangerous/pytz），
与服务器上既有 futu_mm 生产部署（dist/, 监控:8080）完全隔离；
监控 8081（systemd: wt-turtle-monitor.service, token 鉴权）+ WatchDog
双进程托管（cta_turtle_dt + cta_turtle, 均 guard）。

**四个实测坑（按发现顺序）**：

1. **hots.json 空规则 → 实盘 segfault**（致死）：HOT 合约数据装配走
   `cacheIntegratedBars`，空规则 → `splitCustomSections` 返回 false →
   空容器 → libWtDataStorage 内段错误 (segfault at 0x70, 异常穿过
   extern"C" 边界)。回测直读 `*_HOT.dsb` 不受影响——**此坑只在实盘出现**。
   对策：hots.json 必须写入当前主力映射；DataChecker.check_hots(live=True)
   已升级为空规则 error；主力判定可用 fetch_data_tqsdk --fix-hots 或
   解析 rt/min5 dmb 持仓量实测（本文 §2）。
2. **fd 上限**：datakit 每合约持有 ticks/min1/min5 三个 mmap fd，
   全量 922 合约 ≈ 2766 fd > systemd 默认 1024 → EMFILE 异常穿边界
   → abort。对策：systemd unit `LimitNOFILE=1048576`（子进程继承）。
3. **C++ 配置加载器对注释敏感**：tdparsers.yaml 带中文行内注释时
   WTSCfgLoader 解析失败 ("Loading parser config failed")。对策：
   C++ 侧读的 yaml 保持纯配置。
4. **僵尸合约 tick 时间戳非法**：全量合约中大量远月无交易合约推送
   非法时间戳 tick（如 20260901.73028700），writer 合成路径写越界
   (libWtDataStorage error 7)。对策：contracts.json 裁剪为活跃合约集
   （远端已裁至 11 个: rb/hc/l/pp/i 主力次主力）；顺带关闭
   disabletick/disablemin1 减负。
5. **WTSBarStruct 字段名**（P1 已修）：ctypes 赋值 volume/turnover/
   open_interest 会被静默吞掉（真实字段 vol/money/hold），数据全零。

**部署操作要点**：pip 装 wtpy 需 `--no-deps`（其 metadata 死锁
pandas==1.3.5 在 py3.11 无法源码编译）；statemonitor.yaml 非 allday
模式必需（否则 tick 不落盘不广播）；SimNow 7x24 行情前置用
182.254.243.31:30011（180.168.146.187 组从该服务器不可达）。

## 6. 设计取舍备忘（v1 仍有效，增量：）

6. **为何不能单进程直挂 CTP**：WtCore 全源码中 bar 合成只存在于 WtDtCore/
   WtDataWriter（数据底盘侧），交易进程无此组件（F8）。
7. **为何当日K不回补进 his**：min DSB 轮转盲追加（F5）会重复；分区律
   "his=昨日及以前, rt=当前交易日"是唯一的去重保证。

## 6. 合约信息每日更新 (2026-09-02, scripts/update_contracts.py)

CTPLoader (wtpy ContractLoader → libCTPLoader.so → CTP TraderAPI
QueryInstrument) 拉取全市场合约/品种，更新 common/contracts.json 与
commodities.json。链路设计：

1. **子进程隔离**: C++ DumpToJson 后 `exit(0)` (TraderSpi.cpp:376)，
   宿主进程会被带走 → 必须 subprocess 调用；
2. **种子合并**: 临时目录先放入现有 json (C++ LoadFromJson 先读后写)，
   手工维护字段保留；session 由 mapfiles (scripts/ctp_loader/map_future.ini
   的 [Session] 段，覆盖 INE.sc=FN0230/SHFE.cu=FN0100 等) 提供，
   新品种未命中时回填旧文件值；
3. **SAVEPATH 必须绝对路径**: C++ 以子进程 cwd 解析相对路径，
   相对 common 会写入嵌套目录 `./ctp_loader_xx/ctp_loader_xx/`，
   读到旧种子文件导致"合约数异常(11)拒绝替换"（实测坑⑤）；
4. **过期合约剔除**: expiredate < 当日的合约不写入 — 根治实盘坑④
   (僵尸合约非法时间戳 tick → writer 写越界)，替代手工裁剪；
5. **当日幂等**: contracts.json mtime=今日则跳过，WatchDog 重启不重复请求；
6. **原子替换 + .bak.YYYYMMDD 备份**；合约数 <100 拒绝替换（防半截数据）。

触达路径（双保险）：
- deploy run.py 启动链 step0（WT_CONTRACT_SYNC=0 可关，默认开），
  凭据自动取同目录 tdtraders.yaml (traders[0])；
- systemd timer `wt-turtle-contracts.timer` 每日 20:30 (夜盘前) 兜底。

凭据口径：CTP 交易前置 (SimNow 7x24 = tcp://182.254.243.31:30001，
**不是**行情前置 30011；30201 不通——实测坑⑥)。

远端实测 (2026-09-02): 848 合约 / 87 品种, session 回填 17, 剔过期 0
(SimNow 只返回活跃合约); datakit 按 contracts.json 全量订阅
(dtcfg.yaml 注释), fd 上限已由 LimitNOFILE=1048576 解决。

## 7. 实盘 session_end 不触发坑与自愈评估 (2026-09-03)

**现象**: 部署后策略整日未武装——交易侧 config.yaml 无 statemonitor
(WtCore 不支持, StateMonitor 只在 WtDtCore), session 事件由
WtCtaRtTicker 驱动: session_end 在"收盘分钟 bar 闭合"或兜底分支触发,
两者都要求**引擎时间(=最后行情时间)推进过收盘点**; 白盘 15:00 后无
tick, 引擎时间停在 15:0x < TRADING 收盘 1515 → 兜底死锁,
session_end 永不触发 (实测 Runner 日志无任何 session_end 记录)。

**修复**: StraTurtle.on_tick 增加新交易日自愈评估 (_catchup_eval):
首个 tick 检查交易日翻篇 + his 末根未评估 → 立即纯 his 评估武装
(与 session_end synth=None 兜底同口径); 每日至多一次 (_catchup_date),
bt 不启用 (is_backtest 闸, 保住回测口径)。session_end 若偶发触发
(如夜盘品种 tick 推进过收盘点), _d1_date 去重防重复评估。

**顺带收益**: 冷启动/盘后启动不再空跑次日——开盘首 tick 即武装。

实测 (2026-09-03 09:58 重启后): `catchup eval: his last=20260902
armed L=3203.0 S=2968.0` 与本地独立复算逐位一致。

注意: set_index_value/add_chart_mark 仅 schedule 时段可用
(CtaStraBaseCtx.cpp:2036), 非 schedule 路径 (on_tick) 须传
_post_calculate(mark=False) 跳过图表段。

## 8. 多品种上线实录 (2026-09-03)

12 品种 (rb/cu/al/sc/TA/MA/CF/pp/l/m/p/IF) 全量上线, 当日完成:

1. **数据**: 本地 akshare 日线 csv rsync → 远端 convert_csv_to_dsb
   重建 (--stop-tdate 当日); hots.json 13 品种主力映射由
   scripts/pick_hot_sina.py 生成 (新浪 futures_zh_spot 实时持仓量最大,
   CZCE 3→4 位码转换用 expiredate 补年份, 合约代码按 WT 原生格式
   写回——SHFE/DCE/INE 小写、CZCE 3 位、CFFEX 大写, 与 contracts.json
   键严格一致, 否则 C++ HOT 映射失败);
2. **datakit 重启加载 848 合约全订阅** (contracts.json 为 CTPLoader
   日更全量版; fd 上限/僵尸合约问题已由 LimitNOFILE+过期剔除根治),
   实测 SHFE/DCE/CZCE/GFEX 四所 tick 流正常;
3. **catchup 自愈首演**: 12 品种重启后首 tick 全部立即武装
   (his last=20260901/0902), 通道值=含当根包络口径与本地复算逐位一致;
4. **实盘首单**: INE.sc 多 2 手 @678.4 (风控 8→2 缩量),
   CZCE.MA 多 53 手 @3160 (风控 212→53) — catchup 场景"补票"特征:
   价格已穿越昨日通道时首 tick 立即触发, 成交价比武装位高约 0.5N,
   与 trigger 语义自洽但滑差大于常规盘中触发, 评估绩效时应知悉;
5. 图表 API 非 schedule 时段防护: add_chart_mark/set_index_value
   仅 schedule 可用 (CtaStraBaseCtx.cpp:1978/2036), tick 路径全部走
   _safe_mark (_in_schedule 闸) / _post_calculate(mark=False)。
