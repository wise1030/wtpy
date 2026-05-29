# ks2wtpy：KS/MinIO/S3 数据到 WonderTrader/wtpy 标准数据仓转换工具

`ks2wtpy` 用于把 KS 服务商提供的期货历史 K 线数据转换为 WonderTrader / wtpy 可直接消费的本地离线数据仓。目标不是让回测、实盘、投研各自读取不同格式，而是建立统一的：

```text
KS 原始数据（本地目录 / SSH / MinIO S3）
  -> 标准化解析
  -> WT Storage（CSV + DSB + LMDB + basefiles）
  -> Validation（manifest + quality_report + readback）
  -> Access（回测 / 实盘历史预检 / 投研派生视图）
```

当前项目已经覆盖 `docs/ks_data_integration_analysis.md` 中的 Phase 0、Phase 1 以及三类访问场景的基础闭环：历史回测、实盘启动前历史数据预检、投研只读派生视图。它仍不是完整的生产级数据服务平台；生产级 basefiles 元数据和 Phase 3.5 盘中宕机恢复本轮明确不在 `ks2wtpy` 内继续处理，由其他模块/方案替代或后续独立建设。

## 1. 主要功能

### 1.1 数据源

- 本地 KS 数据目录：`--local /path/to/ks-data`
- 远程 SSH 目录：`--remote HOST --remote-user USER --remote-path PATH`
- MinIO/S3 数据源：`--s3 --s3-endpoint ... --s3-bucket ks-data`

在当前项目语境下，"KS接口"优先指 MinIO/S3 API。SSH 路径仍保留，但更适合诊断或兼容旧部署。

### 1.2 输出格式

- CSV：wtpy CSV 调试/轻量回测格式，输出到 `{output}/csv/`
- DSB：普通 `WtDataStorage` 历史文件，输出到 `{output}/his/day|min1|min5/{EXCHG}/`
- LMDB：`WtDataStorageAD` 兼容存储，输出到 `{output}/lmdb/day|min1|min5/{EXCHG}/`
- basefiles：`commodities.json`、`contracts.json`、`sessions.json`、`hots.json`、`fees.json`、`holidays.json`，输出到 `{output}/common/`
- 回测配置模板：`configbt_csv.yaml`、`configbt_dsb.yaml`、`configbt_lmdb.yaml`
- 审计文件：`conversion_manifest.json`、`quality_report.json`

### 1.3 转换模式

- `--full`：全量重建，关闭增量语义
- `--incremental`：增量更新，支持 `--dates YYYYMMDD,...` 限定分钟线日期
- `--config-only`：只生成/更新 basefiles
- `--verify`：只验证已生成输出
- `--live-preflight`：实盘启动前历史数据只读预检，不连接 KS/S3，不写实时 `.dmb`，同时输出机器可消费 startup gate 与 JSONL 告警摘要
- `--research-export`：从已转换 WT 离线仓导出投研 CSV 视图和 lineage metadata；`pyarrow` 可用时同步生成 parquet sidecar，不可用时安全降级为 CSV-only
- `--include-cffex-options`：启用 CFFEX 股指期权（IO/HO/MO）独立流水线，与普通 `--include-option`（合约级期权过滤）正交。详见 §6.6.1 与 §3.8。

### 1.4 数据质量与审计

每次转换会生成：

- `conversion_manifest.json`
  - 转换模式、源摘要、输出路径、过滤条件、启用输出类型、统计信息；
  - `data_version.version_id` 与 stats fingerprint；
  - `access_contract`：把回测、实盘历史预检、投研派生视图绑定到同一份 manifest/quality_report；
  - `data_semantics`：记录 stdCode、KS date、分钟 timestamp、夜盘日期不平移等语义；
  - S3/MinIO 模式下只记录 raw/cache artifact 摘要和 sha256，不记录 access key 或 secret key。

- `quality_report.json`
  - CSV 文件数、行数、首尾时间；
  - 重复、乱序、OHLC 错误、负 volume/turnover/open_interest、解析错误；
  - 分钟线缺口、持仓跳变；
  - 日线和分钟线日期集合一致性；
  - hots 主力切换、回退、日期缺口审计；
  - LMDB/DSB Python readback 统计；
  - 可用时探测官方 `WtDataHelper.read_dsb_bars()` DSB 读回。

## 2. 与 `docs/ks_data_integration_analysis.md` 的对齐状态

| 阶段/场景 | 当前状态 | 说明 |
|---|---|---|
| Phase 0 安全与一致性 | 基本对齐 | 凭据外置；`--output` 子目录重算；`--full` 全量语义；CLI/README 对齐；WTSBarStruct=88 bytes；LMDB key=52 bytes。 |
| Phase 1 基础数据正确生成 | 基本对齐 | 支持 tar.gz 日期发现、CSV/DSB/LMDB 输出、增量排序、完整 LMDB key 去重、DSB/LMDB readback、S3 小范围真实转换验证。 |
| Phase 2 回测可用 | 可用底座已完成；生产级 basefiles 元数据本轮排除 | 已能生成 CSV/DSB/LMDB/basefiles/config 模板；最小 wtpy `stra_get_bars()` smoke 已验证 CSV/basefiles 可读。真实手续费、合约乘数、节假日、复杂 sessions 等生产级元数据由其他模块/方案替代或后续接入权威来源。 |
| Phase 3 实盘历史加载 | 预检层和机器 gate/告警摘要已完成；自动补齐仍不负责 | `--live-preflight` 可检查离线仓、缺数据时 `decision=block`、明确不写实时 `.dmb`，并写出 `live_gate.json` 与 `live_alerts.jsonl` 供启动脚本/告警系统消费。自动补齐和告警投递通道不在本工具内实现。 |
| Phase 3.5 盘中宕机恢复 | 本轮明确排除 | safe mode、checkpoint、GapDetector、BackfillProvider、StorageRepair、RecoveryGate、recovery manifest 不在 `ks2wtpy` 本轮处理，由其他模块/方案替代或后续独立项目建设。 |
| Phase 4 投研服务化 | 派生视图与 parquet sidecar 已完成；服务 API 待增强 | `--research-export` 可从转换后的 WT CSV 派生投研 CSV 并记录 lineage；`pyarrow` 可用时写 parquet sidecar 和 `formats/parquet` metadata，不可用时安全降级。WtDtServo->pandas wrapper、研究服务 API 仍属后续。 |

结论：当前 `ks2wtpy` 已经从"雏形转换脚本"升级为"最小可验收的 KS -> WT 离线数据仓转换链路"。它已满足历史回测、实盘历史预检 startup gate、投研 CSV/parquet 派生视图三类场景的底座要求；生产级 basefiles 元数据和 Phase 3.5 盘中恢复不作为本轮 `ks2wtpy` 任务继续推进。

## 3. 目录结构与核心模块

```text
ks2wtpy/
├── config.py                    # 全局配置、交易所/品种/周期映射、WTSBarStruct/LMDB key 常量
├── ks_reader.py                 # 本地/SSH KS CSV/tar.gz 读取与 KSBarRecord 解析
├── s3_reader.py                 # MinIO/S3 KS 数据读取、本地 cache、内存 cache、prefetch
├── standardizer.py              # stdCode、bar time、审计摘要等 WT 语义标准化
├── csv_writer.py                # wtpy CSV 生成、增量 append、HOT logret、CSV readback
├── dsb_writer.py                # WonderTrader .dsb 生成、zstd 压缩、Python readback
├── lmdb_writer.py               # WtDataStorageAD LMDB 生成、完整 key 去重、readback
├── config_generator.py          # basefiles 生成:commodities/contracts/sessions/hots/fees/holidays
├── main.py                      # CLI 主入口、转换流程、manifest、quality_report、preflight、research export
├── remote_update.py             # 远程/每日增量包装,复用 main.py parser/config builder
├── requirements.txt             # 可选依赖:lmdb、zstandard、minio、pytest、(pyarrow) 等
├── configbt_csv.yaml            # 回测配置模板:CSV 模式 (replayer.mode=csv)
├── configbt_lmdb.yaml           # 回测配置模板:LMDB 模式 (WtDataStorageAD)
│
├── acceptance/                  # P0 验收门禁子包:统一收口验收/实盘/CI 入口
│   ├── __init__.py              # 包入口, 再导出 build_acceptance_report / write_acceptance_artifacts
│   ├── __main__.py              # 允许 `python -m acceptance --output ...` 调用
│   ├── cli.py                   # 子包 CLI:全量验收 (manifest+quality+readback+pytest+pycompile+freshness+completeness+basefiles_loadback+s3_matrix)
│   ├── gates.py                 # 5 个核心 gate:ManifestGate / QualityGate / ReadbackGate / PytestGate / PyCompileGate + GateResult
│   ├── report.py                # build_acceptance_report() + write_acceptance_artifacts() — 聚合 _rollup 总账
│   ├── freshness.py             # FreshnessGate (P0):manifest.generated_at + DSB mtime + 存在性三源最短板
│   ├── completeness.py          # CompletenessGate (P0):当日 _main+_index DSB 缺失率, 假日 skip
│   ├── basefiles_loadback.py    # BasefilesLoadbackGate (O2/P0):wtpy 原生 ContractMgr/SessionMgr/ProductMgr 真 load common/ 三件套 + 交叉引用
│   ├── live_gate.py             # 实盘启动轻量 gate:`python -m acceptance.live_gate --output ... --strict`
│   ├── s3_matrix.py             # 跨格式 (CSV/LMDB/DSB) 一致性 + S3 端到端 gate (BarTriple)
│   ├── wt_readback.py           # WtDtServo C++ loader DSB 读回 gate (WtReadbackTask + WtReadbackGate)
│   └── _wt_readback_worker.py   # WtDtServo singleton 隔离 subprocess (避免一进程多次 setStorage)
│
├── research/                    # 投研服务化:只读派生视图加载器
│   ├── __init__.py              # 再导出 ResearchView / open_research_view
│   └── adapter.py               # ResearchView (list_datasets/load/describe/lineage) + open_research_view()
│
├── references/                  # 非代码权威快照, 用于 basefiles 生产化
│   ├── README.md                # 各快照的来源/升级流程/字段语义
│   ├── wt_commodities_snapshot.json     # WT 官方 commodities.json 快照
│   ├── wt_contracts_snapshot.json       # WT 官方 contracts.json (922 合约)
│   ├── wt_fees_snapshot.json            # WT 官方 fees.json (36 品种)
│   ├── wt_holidays_snapshot.json        # WT 原版 2008-2022 节假日 (UTF-8 + 排序去重)
│   ├── wt_sessions_snapshot.json        # WT 官方 sessions.json (GBK→UTF-8, 8 个 session ID)
│   └── extra_holidays_2023_2026.json    # 国务院发文人工维护的 2023-2026 节假日扩展
│
├── docs/                        # ks2wtpy 子模块深度文档
│   └── stdcode_and_cpp_pitfalls.md      # stdCode 三段规范 + WT C++ loader 完整陷阱清单 (12 章节)
│
└── tests/                       # 全量回归测试 (178 个 test_ 函数)
    ├── test_ks2wtpy_regression.py       # 主回归 (96 个), 覆盖 config/reader/writer/generator/main/standardizer + CFFEX 期权独立流水线 7 项
    ├── test_acceptance.py               # acceptance 子包回归 (22 个)
    ├── test_basefiles_loadback.py       # basefiles loadback 回归 (10 个)
    ├── test_research_adapter.py         # research adapter 只读契约锁定 (13 个)
    ├── test_wt_readback.py              # WtReadbackGate 行为锁定 (18 个)
    └── test_s3_matrix.py                # s3_matrix gate 跨格式一致性 (19 个)
```

### 3.1 `config.py`

职责：

- 定义交易所、期货/期权品种、KS period 到 WT period 的映射；
- 定义 `ConvertConfig` dataclass；
- 统一输出路径：`csv/`、`lmdb/`、`his/`、`common/`、`convert_state.json`；
- 固化二进制结构事实：
  - `WTSBAR_STRUCT_SIZE = 88`
  - `LMDB_BAR_KEY_SIZE = 52`
  - `LMDB_KLINE_MAPSIZE = 512MB`（**已知限制**：全品种 m1 场景不够，详见 §14）
  - `WTPY_SUPPORTED_PERIODS = {'kline_1d', 'kline_1m', 'kline_5m'}`（15m/30m/1h 不支持）

### 3.2 `ks_reader.py`

职责：

- 解析 KS CSV 字段：`date,time,open,high,low,close,volume,amount,openinterest`；
- 支持本地目录、tar.gz、SSH 读取；
- 输出标准 `KSBarRecord`；
- 支持期权过滤与 tar.gz-only 日期发现。

### 3.3 `s3_reader.py`

职责：

- 使用 MinIO/S3 API 读取 KS 对象；
- 三级缓存：S3 → 磁盘 tar.gz (`s3_cache/`) → 内存解析后 `{code: [KSBarRecord]}`；
- 支持 prefetch 预热（多线程并行下载 + 内存 pin）；
- 不在 manifest 中写入 endpoint/access key/secret key 等敏感值。

**性能调优环境变量**（详见 §14）：

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `KS2WTPY_MEM_CACHE_ITEMS` | 4000 | 内存缓存最大条目数 |
| `KS2WTPY_MEM_CACHE_MODE` | auto | `auto`=FIFO淘汰 / `full`=永不淘汰(注意OOM) |
| `KS2WTPY_PREFETCH_WORKERS` | 16 | 预热下载线程数 |
| `KS2WTPY_PREFETCH_BATCH` | 200 | 每批预热日期数 |

### 3.4 `standardizer.py`

职责：

- 统一 WT 语义工具（48 行,被 reader/writer/generator 共用）；
- 公开函数：
  - `std_code(exchg, code) -> str`：标准化 stdCode（`EXCHG.CODE`，**CZCE 不去首位**——与 WT C++ CodeHelper 最新版行为一致）
  - `minute_bar_time(date, time) -> int`：分钟 bar key `YYYYMMDDHHMM`
  - `wt_day_time() -> int`：日线占位时间 `0`
  - `bar_time_key(period, date, time) -> int`：周期感知封装
  - `normalize_bar(period, bar) -> dict`：统一字段名 + 时间 key + 缺失字段补 0

> 注意：WT C++ 历史版本有 CZCE 去首位的逻辑（SR109 → CZCE.R109），但后来修正为不去首位。当前 `std_code("CZCE", "SR109")` 返回 `CZCE.SR109`，与 WT C++ 最新版一致。如果回测用的是旧版 WT C++，可能存在 stdCode 不匹配。

### 3.5 `csv_writer.py`

职责：

- 写 wtpy CSV；
- 增量 append 时做全局时间排序和去重；
- 支持日线、min1、min5；
- HOT 主力合约 CSV 生成 + 对数收益率 (logret)；
- CSV 也作为 quality report 和 research export 的可读源。

> 已知限制：logret 仅输出 CSV 格式，DSB/LMDB 无 logret 支持。HOT CSV 不做价格复权——换月时直接拼接原始价格，产生虚假跳空；WT HotMgr 可处理复权因子，但 CSV 模式回测不受保护。

### 3.6 `dsb_writer.py`

职责：

- 生成普通 `WtDataStorage` 可用 `.dsb` 文件；
- 支持 DSB V2 压缩格式（zstd/zstandard）；
- WTSBarStruct value 为 88 bytes；
- 支持 DSB header/payload/bar_count readback；
- quality report 中会统计 DSB 文件数和 bar 数；可用时探测官方 `WtDataHelper.read_dsb_bars()`。

> 已知限制：**DSB 不支持增量写入**——增量模式下只写入新日期的 bars 会覆盖整个 DSB 文件，丢失历史数据。详见 §14 已知问题 #1。

### 3.7 `lmdb_writer.py`

职责：

- 生成 `WtDataStorageAD` 兼容 LMDB；
- key 为 `exchange[16] + code[32] + bartime(uint32 BE)`，共 52 bytes；
- value 为 88 bytes WTSBarStruct；
- 增量去重使用完整 `(exchange, code, bartime)`，避免跨合约误去重。

> 已知限制：LMDB mapsize 固定 512MB，全品种 m1 场景不够（详见 §14）。LMDB 没有 HOT 数据写入——只有 csv_writer 和 dsb_writer 有 HOT 产物。

### 3.8 `config_generator.py`

职责：

- 生成 wtpy basefiles（commodities/contracts/sessions/hots/fees/holidays）；
- 尊重 `--products`/`products_filter`，避免小范围转换时扫描全市场；
- `contracts.json` 可从日线样本填充 `opendate`/`expiredate`；
- HOT 主力合约用纯 OI 最大启发式（`determine_hot_contract`）确定；
- 生成基础 `hots.json` 并由 quality report 审计主力切换；
- **CFFEX 股指期权独立流水线**：`generate_commodities` / `generate_all` 接受 `option_products` 形参（如 `{'CFFEX':['IO','HO','MO']}`），把这些纯期权品种以 `category=6`（`CC_SpotOption`，**非 2=CC_FutOption**）写入 `commodities.json`；`_iter_products` 默认按 `OPTION_ONLY_PRODUCTS` 过滤纯期权目录，避免被当作期货误入 contracts/fees/hots。

注意：当前 basefiles 属于"可用底座/半真实元数据"，生产级手续费、保证金、节假日、复杂 session、真实合约乘数等仍建议后续接入权威来源。CFFEX 期权品种的 `pricetick`/`volscale` 走 fallback 占位，待补 `references/wt_commodities_snapshot.json` 中权威条目。

> 已知限制：hots.json 的 `oldclose` 在切换前一日为假日时可能为 0.0，导致 WT 复权因子除零（详见 §14 #2）。

### 3.9 `main.py`

职责：

- CLI 参数解析（30+ 参数）；
- 根据参数创建 local/SSH/S3 reader；
- 执行 full/incremental/config-only/verify/live-preflight/research-export 六种模式；
- 写 `conversion_manifest.json`、`quality_report.json`；
- 生成回测配置模板；
- 执行 `--live-preflight`，生成 `live_preflight_report.json`、`live_gate.json`、`live_alerts.jsonl`；
- 执行 `--research-export`，生成 research CSV、可选 parquet sidecar 和 `research_view_metadata.json`。

### 3.10 `remote_update.py`

职责：

- 作为每日远程增量包装入口；
- 复用 `main.py` 的 CLI builder，避免参数漂移；
- 支持 legacy 参数归一化（`--remote-host` → `--remote`）。

### 3.11 `acceptance/` —— P0 验收门禁子包

设计目标：把分散的产物（`conversion_manifest.json` / `quality_report.json` / 回归测试 / 编译 / WT loader 读回 / DSB 新鲜度 / 当日完整性 / 跨格式一致性 / S3 端到端）汇成 *单一* `acceptance_report.{json,md}`，给出 `overall_status`，便于 CI 与人工双轨判定。

入口：

- `python -m acceptance --output <dir> [...]`：完整验收（默认全开 manifest/quality/readback/pytest/pycompile/freshness/completeness/basefiles_loadback/s3_matrix）
- `python -m acceptance.live_gate --output <dir> [...]`：实盘启动轻量 gate（只跑 freshness + completeness，stdout 单行 JSON）

门禁矩阵：

| 门禁 | 模块 | 内容 |
|------|------|------|
| `ManifestGate` | gates.py | 校验 manifest 完整性、stats.errors=0 |
| `QualityGate` | gates.py | CSV/LMDB/DSB 质量扫描(重复/异常/OHLC/白名单) |
| `ReadbackGate` | gates.py | CSV/LMDB/DSB 读回抽样校验 |
| `PytestGate` | gates.py | 运行 pytest 回归测试 |
| `PyCompileGate` | gates.py | 编译检查所有 .py 文件 |
| `S3MatrixGate` | s3_matrix.py | CSV/LMDB/DSB 三格式跨格式一致性 + S3 端到端 |
| `FreshnessGate` | freshness.py | 数据新鲜度检查(距上次转换的小时数) |
| `CompletenessGate` | completeness.py | 数据完整性(应存在品种的缺失比例) |
| `BasefilesLoadbackGate` | basefiles_loadback.py | wtpy 原生 loader 读回校验 |
| `WtReadbackGate` | wt_readback.py | WT WtDtServo 加载 DSB 读回 |

### 3.12 `research/` —— 投研服务化只读派生视图

设计目标：投研脚本/notebook 用一个简短 API 加载 `--research-export` 派生出来的 CSV/parquet 视图，自动选优、自动透传 lineage，**绝不写盘**。

公开 API：

- `open_research_view(path: str) -> ResearchView` — 工厂函数
- `ResearchView.list_datasets() -> List[Tuple[std_code, period, format]]`
- `ResearchView.load(std_code, period, *, format='auto', as_dataframe=False)` — 支持 CSV 和 Parquet 两种格式
- `ResearchView.describe(std_code, period) -> Dict`

只读契约被 `tests/test_research_adapter.py`（13 项）锁定。

### 3.13 `references/` —— 非代码权威快照

| 文件 | 来源 | 用途 |
|------|------|------|
| `wt_commodities_snapshot.json` | WT 官方 | `commodities.json` 按品种拷贝 category/pricetick/volscale/precision/session/holiday |
| `wt_contracts_snapshot.json` | WT 官方 (922 合约) | `contracts.json` 派生 maxlimitqty/maxmarketqty/min*qty/longmarginratio/shortmarginratio |
| `wt_fees_snapshot.json` | WT 官方 (36 品种) | `fees.json` 拷贝 {open, close, closetoday, byvolume} 4 字段 |
| `wt_sessions_snapshot.json` | WT 官方 (GBK→UTF-8) | 8 个官方 session ID:FD0900/FD0915/SD0930/FN0100/FN0230/FN2300/FN2330/TRADING |
| `wt_holidays_snapshot.json` | WT 原版 (2008-2022, 265 天) | 顶层 {"CHINA": [YYYYMMDD,...]} |
| `extra_holidays_2023_2026.json` | 国务院发文人工维护 | 2023-2026 共 77 天扩展，按年组织。2026 年数据暂用 2025 同期估值 |

升级原则：每年 11-12 月国务院发布次年放假安排后，编辑 `extra_holidays_2023_2026.json` 即可；其他快照随 WT 上游同步覆盖再 `sort_keys=True` 规范化。

### 3.14 `docs/` —— 子模块深度文档

| 文件 | 内容 |
|------|------|
| `stdcode_and_cpp_pitfalls.md` | stdCode 三段规范使用指南 + WT C++ loader 完整陷阱清单（12 章节,含 extractStdCode 四步法、CZCE 去首位、HOT 展开机制、basefiles 加载顺序、10 项高频坑点自检表） |

### 3.15 `tests/` —— 全量回归测试

178 个 `test_*` 函数，覆盖 9 个 P0 域：

| 测试文件 | 用例数 | 覆盖 |
|----------|--------|------|
| `test_ks2wtpy_regression.py` | 96 | 主回归:config/reader/csv_writer/lmdb_writer/dsb_writer/config_generator/main/standardizer/preflight/research/manifest/quality + **CFFEX 期权独立流水线 7 项** |
| `test_acceptance.py` | 22 | acceptance 子包:GateResult/rollup/freshness/completeness/live_gate/白名单 |
| `test_basefiles_loadback.py` | 10 | basefiles loadback 回归 |
| `test_research_adapter.py` | 13 | ResearchView 只读契约:open/list/load/describe/lineage/readonly/as_dataframe |
| `test_wt_readback.py` | 18 | WtReadbackGate 任务构造、stdCode 三段、CZCE 去首位、scan/autodiscover/gate |
| `test_s3_matrix.py` | 19 | BarTriple 三态一致性、S3 端到端凭据缺失 skip |

**未覆盖/弱覆盖模块**：`main.py`（1878行，核心转换管线靠集成测试）、`s3_reader.py`（540行，仅 s3_matrix 中 mock 测试）、`remote_update.py`（零直接测试）。

## 4. 安装依赖

```bash
cd /mnt/d/gf_pc/WonderTrader/wtpy/tools/ks2wtpy
python3 -m pip install -r requirements.txt
```

最小可选依赖：

```bash
python3 -m pip install lmdb zstandard minio pytest
```

说明：CSV-only/local 转换主要依赖 Python 标准库；LMDB、DSB、S3、测试分别需要 `lmdb`、`zstandard`、`minio`、`pytest`。

可选软依赖（按需启用）：

- `pyarrow`：`--research-export` 时同步输出 parquet sidecar；缺失则自动降级为 CSV-only
- `pandas`：`ResearchView.load(..., as_dataframe=True)` 时返回 DataFrame；缺失则抛 `ImportError`
- WonderTrader C++ Python 扩展（`wtpy.WtDataHelper` / `wtpy.WtDtServo`）：`wt_readback` gate 与 `--live-preflight` 的真实读回时启用；缺失则相关 gate `skip`

## 5. 数据源配置

### 5.1 S3/MinIO（推荐）

推荐用环境变量或本地未提交配置注入凭据，不要把密钥写入源码或命令日志：

```bash
export KS2WTPY_S3_ENDPOINT='[REDACTED]'
export KS2WTPY_S3_ACCESS_KEY='[REDACTED]'
export KS2WTPY_S3_SECRET_KEY='[REDACTED]'
```

转换命令示例：

```bash
python3 main.py --full --s3 \
  --s3-bucket ks-data \
  --output ./storage/ks_converted \
  --products SHFE.rb \
  --periods m1,m5,d
```

KS S3 真实布局（端口 6900, bucket=ks-data）：

- 周期目录：`kline_1d/kline_1m/kline_15m/kline_1h`（**无 5m**）
- 日线：整品种一个总 tar 含 `_main` + `_index` + 全合约
- 分钟线：按日切 tar，无 `_main`，只有 `_index` + 具体合约 + 期权 + SP 价差
- CZCE 月号 3 位（AP610）
- `future_code_list.json` GBK 编码是 Phase 2 basefiles 权威源

常见注意点：

- MinIO Python client 的 endpoint 不能带 scheme/path；
- 如果本地有凭据文件，字段值视为敏感信息，不应打印；
- 大范围 S3 转换前建议先用单品种/小日期窗口 smoke；
- **内存敏感场景**建议调低 `KS2WTPY_MEM_CACHE_ITEMS` 和 `KS2WTPY_PREFETCH_WORKERS`（详见 §14）。

### 5.2 本地目录

```bash
python3 main.py --full --local /path/to/ks-data \
  --output ./storage/ks_converted \
  --products SHFE.rb \
  --periods m1,m5,d
```

### 5.3 SSH 远程目录

```bash
python3 main.py --incremental --remote KS_HOST \
  --remote-user wondertrader \
  --remote-path /path/to/ks-data \
  --output ./storage/ks_converted
```

SSH 路径保留用于兼容旧流程；新验证优先走 S3/MinIO。

## 6. 常用命令示例

### 6.0 CLI 参数全表

`main.py` 共 30+ 参数，按功能分组速查：

| 分组 | 参数 | 说明 |
|------|------|------|
| **运行模式**（互斥） | `--full` | 全量转换（关闭增量语义） |
| | `--incremental` | 增量更新（默认；配合 `--dates` 控分钟线日期） |
| | `--config-only` | 只生成/更新 basefiles |
| | `--verify` | 只验证已生成输出 |
| | `--live-preflight` | 实盘启动前历史只读预检 |
| | `--research-export` | 从 WT 离线仓派生 CSV/parquet 投研视图 + lineage |
| **数据源**（互斥） | `--remote HOST` | 远程 SSH 主机 |
| | `--local PATH` | 本地 KS 目录 |
| | `--s3` | 启用 MinIO/S3（推荐） |
| **输出选择** | `--csv-only` / `--lmdb-only` / `--dsb-only` | 互斥；只生成某一种 |
| | `--no-csv` / `--no-lmdb` / `--no-dsb` / `--no-config` | 关闭对应输出 |
| **过滤** | `--exchanges EX1,EX2` | 指定交易所子集 |
| | `--products EXCHG.P1,EXCHG.P2` | 指定品种子集（强烈推荐 smoke 用） |
| | `--periods m1,m5,d` | 指定周期（默认 `m5,d`；KS 支持 m1/m5/m15/m30/h1/d，但 wtpy 仅 m1/m5/d 原生支持） |
| | `--dates YYYYMMDD,...` | 增量模式下指定分钟线日期 |
| | `--include-option` | 默认过滤期权；加此开关在期货品种目录内包含期权合约（合约级过滤） |
| | `--include-cffex-options` | 启用 CFFEX 股指期权（IO/HO/MO）独立流水线（品种级，K线-only，category=6） |
| **输出路径** | `--output PATH` | 输出根目录（必传；子目录会自动重算） |
| | `--live-requirements PATH` | 实盘预检需求 JSON |
| | `--research-codes EXCHG.code,...` | 投研导出 stdCode 列表 |
| | `--research-periods min1,min5,day` | 投研导出周期 |
| | `--research-output PATH` | 投研派生视图输出目录 |
| **SSH** | `--remote-user USER` / `--remote-path PATH` | SSH 用户名/远端路径 |
| **S3/MinIO** | `--s3-endpoint HOST:PORT` | endpoint（或环境变量），不能带 scheme/path |
| | `--s3-access-key KEY` / `--s3-secret-key SECRET` | 凭据（或环境变量） |
| | `--s3-bucket NAME` | 默认 `ks-data` |
| | `--s3-secure` | HTTPS（默认 HTTP） |
| | `--s3-cache-dir PATH` | 本地缓存目录（默认 `--output/s3_cache`） |
| **其他** | `--log-level DEBUG/INFO/WARNING/ERROR` | 默认 `INFO` |
| | `--force` | 强制全量 |

### 6.1 查看帮助

```bash
python3 main.py --help
```

### 6.2 小范围 S3 全量转换（建议 smoke 模式）

```bash
python3 main.py --full --s3 \
  --s3-bucket ks-data \
  --output /tmp/ks2wtpy_smoke \
  --products SHFE.ag \
  --periods m1,d \
  --no-config \
  --log-level INFO
```

说明：首次验证建议加 `--no-config`，避免 config 生成阶段扩大扫描范围。转换后检查：

```bash
python3 -m json.tool /tmp/ks2wtpy_smoke/conversion_manifest.json
python3 -m json.tool /tmp/ks2wtpy_smoke/quality_report.json
```

### 6.3 全量生成 CSV + DSB + LMDB + basefiles

```bash
python3 main.py --full --s3 \
  --s3-bucket ks-data \
  --output ./storage/ks_converted \
  --products SHFE.rb,DCE.i \
  --periods m1,m5,d
```

### 6.4 只生成某一种输出

```bash
# 只生成 CSV
python3 main.py --full --s3 --s3-bucket ks-data --csv-only --output ./storage/ks_converted

# 只生成 DSB
python3 main.py --full --s3 --s3-bucket ks-data --dsb-only --output ./storage/ks_converted

# 只生成 LMDB
python3 main.py --full --s3 --s3-bucket ks-data --lmdb-only --output ./storage/ks_converted
```

### 6.5 增量更新

```bash
python3 main.py --incremental --s3 \
  --s3-bucket ks-data \
  --output ./storage/ks_converted \
  --products SHFE.rb \
  --periods m1,m5 \
  --dates 20240102,20240103
```

> **注意**：DSB 不支持增量——增量模式下 DSB 只写入新日期的 bars，会覆盖整个文件丢失历史数据。增量模式建议配合 `--no-dsb` 或接受 DSB 全量重写。

### 6.6 包含期权

期权支持有两条**正交**的开关：

**(a) 合约级 `--include-option`**：在期货品种目录（如 SHFE/CZCE/DCE/INE）内放宽期权合约过滤（匹配 `-C-/-P-` 或 `\d+[CP]\d{3,}`）。默认关闭。

```bash
python3 main.py --full --s3 \
  --s3-bucket ks-data \
  --include-option \
  --output ./storage/ks_converted
```

#### 6.6.1 CFFEX 股指期权独立流水线 `--include-cffex-options`

**(b) 品种级 `--include-cffex-options`**：启用 CFFEX 纯期权品种 IO/HO/MO 的**独立流水线**（与 `--include-option` 互不依赖、可单独或组合启用）：

- 合约通过 `option_only` 模式接受（屏蔽合成 `_main`/`_index`/`.HOT`，只留期权合约本身）
- 仅写 K 线（CSV/LMDB/DSB），不生成 contracts/fees/hots
- `commodities.json` 中以 `category=6`（`CC_SpotOption`，**非 2=CC_FutOption**，参见 `WTSTypes.h:22-29` 枚举）落库
- `pricetick`/`volscale` 当前走 fallback 占位，实盘/回测前需补 `references/wt_commodities_snapshot.json` 权威条目

```bash
# 仅跑 CFFEX 期权
python3 main.py --full --s3 \
  --s3-bucket ks-data \
  --include-cffex-options \
  --products CFFEX.IO,CFFEX.HO,CFFEX.MO \
  --periods m1,d \
  --output ./storage/ks_cffex_options

# 期货 + CFFEX 期权一锅出
python3 main.py --full --s3 \
  --s3-bucket ks-data \
  --include-cffex-options \
  --output ./storage/ks_converted
```

真实 S3 抽样验证（2025-09-12，单日 IO）：CSV 1598 个 `CFFEX.IO\d+-[CP]-\d+_(d|m1).csv`，0 个 `IO_main/IO_index`；LMDB m1=73,839 / d=104,665；DSB 1598 文件；`commodities.json` 中 CFFEX.IO/HO/MO 全部 `category=6`。

### 6.7 只生成 basefiles

```bash
python3 main.py --config-only --s3 \
  --s3-bucket ks-data \
  --output ./storage/ks_converted \
  --products SHFE.rb
```

### 6.8 验证已有输出

```bash
python3 main.py --verify --output ./storage/ks_converted
```

`--verify` 主要检查本地输出，不应作为重新拉取源数据的入口。

### 6.9 实盘启动前历史预检（`--live-preflight`）

```bash
# 1) 准备 live_requirements.json
cat > /tmp/live_req.json <<'EOF'
{
  "SHFE.rb.HOT": {"min1": {"min_bars": 240, "dates": ["20240102", "20240103"]}},
  "DCE.i.HOT":   {"day":  {"min_bars": 250}}
}
EOF

# 2) 跑只读预检 (不连 KS, 不写 .dmb)
python3 main.py --live-preflight \
  --output ./storage/ks_converted \
  --live-requirements /tmp/live_req.json
```

产物：`live_preflight_report.json`、`live_gate.json`、`live_alerts.jsonl`

### 6.10 投研派生视图导出（`--research-export`）

```bash
# 导出所有 CSV 对应的派生视图
python3 main.py --research-export \
  --output ./storage/ks_converted

# 只导出指定 stdCode + 周期
python3 main.py --research-export \
  --output ./storage/ks_converted \
  --research-codes SHFE.rb.HOT,DCE.i.HOT \
  --research-periods day,min5 \
  --research-output ./storage/ks_converted/research_v2
```

消费侧：

```python
from research.adapter import open_research_view

view = open_research_view("./storage/ks_converted/research")
df = view.load("SHFE.rb.HOT", "day", as_dataframe=True)
print(view.describe("SHFE.rb.HOT", "day"))
```

### 6.11 acceptance 全量验收 + 实盘 gate

```bash
# 转换完成后, 一键跑完整验收
python3 -m acceptance --output ./storage/ks_converted \
  --wt-readback \
  --freshness-max-age-hours 48 \
  --fail-on-warn

# 实盘启动脚本, 只跑轻量 freshness+completeness
python3 -m acceptance.live_gate \
  --output ./storage/ks_converted \
  --max-age-hours 24 \
  --completeness-warn-pct 5 \
  --completeness-fail-pct 5 \
  --strict
```

### 6.12 多品种独立进程（避免 OOM）

大品种（如 SHFE.ag）的 m1 数据量大，单进程多品种串行可能 OOM。建议每个品种独立进程：

```bash
# 逐品种独立进程跑数据（内存隔离），最后合并 config
for PROD in SHFE.ag CFFEX.IF CZCE.AP; do
  python3 main.py --full --s3 --no-config \
    --products "$PROD" --periods m1 \
    --output ./storage/ks_multi \
    --s3-bucket ks-data \
    --force
  sleep 5  # 等 GC 释放内存
done

# 合并生成 config
python3 main.py --config-only --s3 \
  --products SHFE.ag,CFFEX.IF,CZCE.AP \
  --periods d,m1 \
  --output ./storage/ks_multi \
  --s3-bucket ks-data
```

## 7. 三类访问场景

### 7.1 场景一：策略回测

转换后会在输出目录下生成回测模板：

```text
configbt_csv.yaml
configbt_dsb.yaml
configbt_lmdb.yaml
```

CSV 模式适合最小 smoke 或人工排查：

```yaml
replayer:
    mode: csv
    path: ./storage/ks_converted/csv
    basefiles:
        commodity: ./storage/ks_converted/common/commodities.json
        contract: ./storage/ks_converted/common/contracts.json
        holiday: ./storage/ks_converted/common/holidays.json
        hot: ./storage/ks_converted/common/hots.json
        session: ./storage/ks_converted/common/sessions.json
```

DSB 模式适合普通 WT storage：

```yaml
replayer:
    mode: wtp
    store:
        module: WtDataStorage
        path: ./storage/ks_converted/his
```

LMDB 模式适合 AD storage：

```yaml
replayer:
    mode: wtp
    store:
        module: WtDataStorageAD
        path: ./storage/ks_converted/lmdb
```

建议正式回测优先 DSB/LMDB，CSV 主要用于 debug 和早期验证。

### 7.2 场景二：实盘历史数据加载预检

`--live-preflight` 只读检查已经转换好的离线仓库：

- 不创建 KS/S3 reader；
- 不写 `rt/*.dmb`；
- 缺少必需历史 bar 或日期时输出 `decision=block`；
- 产物：`live_preflight_report.json`、`live_gate.json`、`live_alerts.jsonl`

### 7.3 场景三：投研分析派生视图

`--research-export` 从转换后的 WT CSV 仓库派生 pandas 友好的 CSV，并写 lineage metadata；`pyarrow` 可用时额外写 parquet sidecar。

派生 CSV/parquet 字段：

```text
datetime,std_code,period,open,high,low,close,volume,turnover,open_interest
```

注意：research CSV/parquet 都是派生视图，不是权威数据源。权威源仍是转换后的 WT storage、`conversion_manifest.json` 和 `quality_report.json`。

## 8. 数据格式说明

### 8.1 KS 输入字段

```text
date,time,open,high,low,close,volume,amount,openinterest
```

### 8.2 wtpy CSV 日线输出

```text
date,open,high,low,close,volume,turnover,open_interest
2010/01/04,4523.0,4610.0,4523.0,4579.0,1809996.0,82828166020.0,965458.0
```

### 8.3 wtpy CSV 分钟线输出

```text
date,time,open,high,low,close,volume,turnover,open_interest
2010/01/04,09:05:00,4523.0,4540.0,4523.0,4535.0,72564.0,3289037040.0,918970.0
```

### 8.4 WTSBarStruct / LMDB

```text
WTSBarStruct value: 88 bytes, pack 8
LMDB key: exchange[16] + code[32] + bartime(uint32 big-endian) = 52 bytes
```

| KS 字段 | wtpy CSV 字段 | WTSBarStruct 字段 |
|---------|---------------|-------------------|
| date | date | date |
| time | time | time |
| open/high/low/close | open/high/low/close | open/high/low/close |
| volume | volume | vol |
| amount | turnover | money |
| openinterest | open_interest | hold |
| 无 | settle | 0 |
| 无 | diff_interest/add | 0 |

### 8.5 storage_mode 语义

| storage_mode | 物理产物 | 适用场景 | WT 配置 mode |
|--------------|----------|----------|-------------|
| `csv` | `csv/<EXCHG>.<code>_{d,m1,m5}.csv` | 最小 smoke、人工排查 | `replayer.mode: csv` |
| `dsb` | `his/<period>/<EXCHG>/<code>.dsb` | 普通 WT/wtpy storage | `replayer.mode: wtp` + `WtDataStorage` |
| `lmdb` | `lmdb/<period>/<EXCHG>/data.mdb` | AD storage / 高吞吐 mmap | `replayer.mode: wtp` + `WtDataStorageAD` |

关键约束：DSB 文件名不带交易所点号；CZCE 去首位（AP2610→AP610.dsb）；WtDtServo.setStorage 传根目录（含 his/）。

## 9. 输出目录示例

```text
storage/ks_converted/
├── csv/   his/   lmdb/   common/   research/
├── conversion_manifest.json   quality_report.json
├── live_preflight_report.json live_gate.json
├── acceptance_report.json     acceptance_report.md
├── configbt_csv.yaml   configbt_dsb.yaml   configbt_lmdb.yaml
└── convert_state.json
```

## 10. 开发与验证

### 10.1 回归测试

```bash
python3 -m pytest -q tests/
```

### 10.2 编译检查

```bash
python3 -m py_compile config.py ks_reader.py csv_writer.py lmdb_writer.py dsb_writer.py \
  config_generator.py main.py remote_update.py s3_reader.py standardizer.py
python3 -m compileall -q acceptance research
```

### 10.3 真实 S3 smoke 原则

- 使用很小范围：单品种、少日期、少周期；
- 不输出凭据；
- 初始转换优先加 `--no-config`；
- 验收不能只看 exit code，要检查 manifest + quality_report + 实际输出文件。

## 11. acceptance 子包与实盘 gate

### 11.1 gate 矩阵

| gate | 作用 | 触发开关 |
|------|------|----------|
| manifest | 校验 manifest 完整性 | 总是跑 |
| quality | CSV/LMDB/DSB 质量扫描 | 总是跑 |
| readback | CSV/DSB/LMDB 读回校验 | 默认开 |
| wt_readback | WtDtServo C++ loader 读 DSB | `--wt-readback` |
| s3_matrix | S3 可达性 + 跨格式一致性 | 总是跑 |
| pytest | 回归测试 | 默认开 |
| pycompile | 编译检查 | 默认开 |
| freshness | 数据新鲜度 | 默认开，阈值 `--freshness-max-age-hours`(24h) |
| completeness | 数据完整性 | 默认开，阈值 `--completeness-warn-pct/fail-pct` |
| basefiles_loadback | wtpy 原生 loader 读回 | 默认开 |

### 11.2 全量验收

```bash
python -m acceptance --output ./storage/ks_converted --wt-readback --freshness-max-age-hours 48
```

### 11.3 实盘启动 gate

```bash
python -m acceptance.live_gate --output ./storage/ks_converted --max-age-hours 24 --strict
```

退出码：0=pass, 1=warn, 2=fail(or --strict 下的 warn)

### 11.4 与 wtpy C++ loader 契约要点

1. 策略 stdCode 三段，DSB 文件名两段
2. CZCE 月号 3 位，DSB 去首位（AP2610→AP610.dsb）
3. WtDtServo.setStorage 传根目录

> 深度文档：`docs/stdcode_and_cpp_pitfalls.md`

### 11.5 投研只读派生视图 adapter

```python
from research.adapter import open_research_view
view = open_research_view("/opt/wtdata/research")
df = view.load("GFEX.pd.HOT", "day", as_dataframe=True)
```

只读契约：不写文件；fmt='auto' 优先 parquet；缺失 → KeyError/FileNotFoundError；lineage 原样回传。

## 12. 已知边界与约束

### 12.1 KS 数据源结构性限制

- **无 5m 周期目录**：S3 真实布局只有 `kline_1d/kline_1m/kline_15m/kline_1h`，`kline_5m/` 不存在
- **无主力标记**：KS 数据源不提供官方主力合约标记，`{product}_main` 通过 OI 最大启发算法本地合成
- **分钟线按日切 tar**：无跨日 _main，每日 tar 内含 `_index` + 具体合约 + 期权 + SP 价差
- **CZCE 月号 3 位**：如 AP610（非 AP0610），年份解析依赖运行时当前年
- **部分品种日线极短**：如 DCE.a 仅 11 天日线
- **ad/IF 采集起点**：`kline_1m` 上游采集从 11:02 开始
- **HO 无 _index**：CFFEX.HO 是纯期权品种，已纳入 `OPTION_ONLY_PRODUCTS`（IO/HO/MO），可通过 `--include-cffex-options` 启用独立流水线

### 12.2 转换产物约束

- **HOT CSV 不做价格复权**：换月时直接拼接原始价格，会产生虚假跳空；复权由 WT C++ 运行时按 `hots.json` 动态计算
- **hots.json oldclose 零值边界**：当切换前一日为假日，`oldclose=0.0`，WT 复权因子可能除零
- **DSB 不支持增量写入**：每次全量重写 O(N) I/O，增量模式下建议 `--no-dsb`
- **LMDB mapsize 固定 512MB**：m1 大品种可能触发 MapFullError（已知影响 SHFE.ag m1）
- **fees.json 覆盖率 61%**：仅 36 个品种有费率数据，其余品种需手动补充
- **CFFEX 期权 commodity fallback**：`--include-cffex-options` 开启时，IO/HO/MO 的 `pricetick`/`volscale` 走 fallback 占位（commodities.json 字段齐但数值不可靠）；实盘/回测前需补 `references/wt_commodities_snapshot.json` 权威条目。category 固定 6（CC_SpotOption），结构正确
- **2026 年节假日暂用 2025 同期估值**：国务院发文后需手动更新 `extra_holidays_2023_2026.json`

### 12.3 运行时约束

- **WtBtEngine/WtDtServo singleton**：同一进程内不能多次创建/销毁；WT readback 测试需 subprocess 隔离
- **CSV 编码**：wtpy CSV 输出使用 `utf-8-sig`（带 BOM），WT C++ 侧兼容
- **hots.json 结构**：`{EXCHG: {product: [{date, from, to, oldclose, newclose}]}}`，WT 按此展开主力合约链

## 13. 安全注意事项

1. **凭据管理**：S3 endpoint/access_key/secret_key 必须通过环境变量或本地未提交的 `credentials.json` 注入；**禁止写入源码、命令日志、README 或任何版本控制文件**
2. **`credentials.json` 含敏感字段**：路径 `tools/ks2wtpy/credentials.json` 可提及，值不可复述；建议定期轮换
3. **MinIO endpoint 格式**：Python client 的 endpoint 不能带 scheme/path；从 `credentials.json.url` 提取时需 `urlparse().hostname + ':6900'`
4. **`.gitignore`**：确保 `credentials.json`、`storage/`、`s3_cache/` 均在忽略列表
5. **凭据泄漏风险**：本会话上下文中曾出现明文 S3 凭据；建议轮换

## 14. 已知问题与改进路线

### 14.1 P0 — 关键缺陷（影响正确性）

| # | 问题 | 现状 | 影响 |
|---|------|------|------|
| 1 | ~~**S3 空结果缓存永久驻留**~~ | **已修**: `_load_parsed_tar` 在 `_download_tar` 返回 None 和 `_extract_csv_from_tar` 返回空 dict 两种路径均不缓存，避免 pin=True 永久驻留空值。测试: `test_p01_empty_parse_result_not_cached` / `test_p01_download_failure_not_cached` | ~~品种数据静默丢失~~ |
| 2 | **hots.json oldclose 零值** | 切换前一日为假日 → oldclose=0.0 | WT 复权因子除零风险 |
| 3 | **CZCE 年份解析依赖运行时当前年** | 3 位月号如 AP610，跨年边界可能误判 | 合约号解析错误 |

### 14.2 P1 — 功能缺陷（影响可靠性）

| # | 问题 | 现状 |
|---|------|------|
| 4 | zstd 压缩异常被静默吞掉 | try/except 空 pass，DSB 损坏无感知 |
| 5 | verify_dsb 只验证前 10 个文件 | 大品种大部分 DSB 未验证 |
| 6 | LMDB mapsize 固定 512MB | m1 大品种 MapFullError |
| 7 | fees.json 只覆盖 61% 品种 | 剩余品种费率缺失 |
| 8 | 增量 CSV 每次全量重写 | O(N) I/O 无必要 |
| 9 | 品种分类字典有重复键 | Python 取最后一个，静默覆盖 |

### 14.3 P2 — 性能优化

| # | 问题 | 建议 |
|---|------|------|
| 10 | 内存缓存淘汰 O(N) 非 LRU | 改用 OrderedDict 或 cachetools.LRUCache |
| 11 | determine_hot_contract 线性扫描 | OI 已降序可二分查找 |
| 12 | LMDB 增量去重全表扫描 | 改用 cursor.get() 精确查找 |
| 13 | Prefetch 线程池重复创建/销毁 | main.py 每阶段新建 ThreadPoolExecutor |
| 14 | logret prev_anchor 用 list.index() O(N) | 改用 dict 预建索引 |
| 15 | 3 个 Writer 的 _bars_for() 闭包 DRY 违规 | 抽取公共基类或 mixin |

### 14.4 P3 — 功能缺失

| # | 缺失 | 建议 |
|---|------|------|
| 16 | 15m/30m/1h 周期 | KS S3 已有 kline_15m/kline_1h 目录 |
| 17 | ~~Tick 数据~~ | **已完成**: `tick_converter.py` + CLI `--tick/--tick-dates/--tick-code`，DSB 路径 `his/ticks/{exchg}/{date}/{code}.dsb`，WTSTickStruct=512B，含 zstd 压缩、增量 volume/turnover 计算。回归测试见 `tests/test_ks2wtpy_regression.py::test_tick_*`。 |
| 18 | 磁盘缓存无 TTL/ETag | 缓存永不过期，上游更新无法感知 |
| 19 | 并发转换 | 同品种不同周期可并行 |
| 20 | 断点续转 | 进程中断后从头重跑 |
| 21 | 结构化日志 | 全部 print + logging 混用 |

### 14.5 OOM 风险与缓解

**现状**：SHFE.ag 多品种 m1 转换在 prefetch 阶段 OOM Kill（RSS 14.5GB > 15GB 限制），即使降到 800 items + 8 workers + batch=150 仍爆。

**缓解方案**（已验证可行）：

```bash
# 方案：单品种独立进程 + sleep 间隔 + 最后合并 config
for PROD in SHFE.ag CFFEX.IF CZCE.AP; do
  python3 main.py --full --s3 --no-config \
    --products "$PROD" --periods m1 \
    --output ./storage/ks_multi --s3-bucket ks-data --force
  sleep 5
done
python3 main.py --config-only --s3 \
  --products SHFE.ag,CFFEX.IF,CZCE.AP --periods d,m1 \
  --output ./storage/ks_multi --s3-bucket ks-data
```

**环境变量调优**：

| 变量 | 默认 | 说明 |
|------|------|------|
| `KS2WTPY_MEM_CACHE_ITEMS` | 4000 | 内存缓存条目上限；OOM 时降至 800-2000 |
| `KS2WTPY_PREFETCH_WORKERS` | 16 | 预取线程数；实测 24 最优（单品种），OOM 时降至 4-8 |
| `KS2WTPY_MEM_CACHE_MODE` | auto | auto=按物理内存自动选 full/thin；full=全缓存；thin=最小缓存 |
| `KS2WTPY_PREFETCH_BATCH` | 200 | 预取批大小；OOM 时降至 50-100 |

## 15. 性能基准

| 场景 | 耗时 | 备注 |
|------|------|------|
| SHFE.rb 5年 m1+d full（旧版无缓存） | 3,675s | v1 基线 |
| SHFE.rb 5年 m1+d full（S3KsReader+prefetch） | 401.8s | **9.1x 加速** |
| SHFE.rb DSB readback | ~80s | 含在 401.8s 内 |
| SHFE.ag 多品种 m1（单进程） | OOM Kill | RSS 14.5GB，需独立进程方案 |
| SHFE.ag + CFFEX.IF + CZCE.AP（独立进程） | 待测 | 方案可行但未跑完整基准 |

## 16. 项目统计

| 指标 | 数值 |
|------|------|
| Python 文件数 | 32 |
| 测试用例 | 189（全绿） |
| acceptance gate 数 | 10 |
| S3 缓存 tar.gz 文件数 | ~6,232 |
| S3 缓存体积 | ~433MB |
| Storage 总体积 | ~2.6GB |
| v1 转换耗时（SHFE.rb 5y m1+d） | 3,675s |
| v2 转换耗时（同上，S3KsReader+prefetch） | 401.8s（9.1x） |
| 版本控制 | 未纳入 git |

## 17. 许可证

本项目随 WonderTrader/wtpy 主仓库发布，遵循其上游许可证。