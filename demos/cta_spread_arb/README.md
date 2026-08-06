# cta_spread_arb — 期货跨品种价差套利策略

基于 wtpy (WonderTrader Python 封装) 的 CTA 跨品种价差套利策略。
**v10 架构**: 单策略实例 = 单 ArbCore (单品种组合) + Pipeline 化决策流水线 + Regime 驱动自适应信号 + 分层 YAML 配置 + 子进程隔离实验矩阵化。

> 文档版本: 2026-06-09 (审计后整理)
> 当前规模: 70 个 Python 文件 / ~11,900 行；44 个 YAML+JSON+MD；18 个测试文件 / **153 测试全绿** ✅
> 当前状态: l/pp 2609 是唯一正样本(全区间 +299)，但 WF 4 折严重过拟合 (2/4 折反向、SUM≈0)；rb/hc 结构性亏损 (品种经济性问题)。

---

## 目录

- [项目结构](#项目结构)
- [架构概览](#架构概览)
- [决策流水线](#决策流水线)
- [核心模块说明](#核心模块说明)
- [配置体系](#配置体系)
- [使用方法](#使用方法)
- [当前回测结果](#当前回测结果)
- [已知问题](#已知问题)
- [优化路径](#优化路径)
- [开发约定](#开发约定)

---

## 项目结构

```
cta_spread_arb/
├── strategies/                  # 策略主体 (876 行 / 4 文件)
│   ├── ArbCore.py               # 套利核心流水线 (415) — sync→recheck→compute→execute→persist
│   ├── ArbCoreLifecycle.py      # on_init/on_bar 生命周期 (119)
│   ├── SpreadArbStrategy.py     # WT 适配层 (339) — 把 ArbCore 装进 BaseCtaStrategy
│   └── __init__.py
│
├── helpers/                     # 组件层 (~2,800 行 / 17 文件)
│   ├── DataPipe.py              # K线获取 + 价差计算 + trend filter (268)
│   ├── SpreadCalculator.py      # 名义价值归一化价差 (152)
│   ├── SignalGen.py             # SignalPipeline + RegimeDetector 装配 (121)
│   ├── SignalPipeline.py        # Pipeline 顶层壳(已迁至 pipeline/) (30)
│   ├── RiskMgr.py               # 风控规则引擎 (311)
│   ├── ArbExecHelper.py         # 状态机执行器 IDLE→OPENING→OPENED→CLOSING (364)
│   ├── StateManager.py          # CtaContext user_save/load_data 持久化 (159)
│   ├── ConfigManager.py         # 顶层 YAML 加载 (167)
│   ├── ConfigResolver.py        # 分层配置合并 base+contract+experiment+profile (364)
│   ├── DiagLogger.py            # JSONL 全量埋点 (factor/risk/trade) (169)
│   ├── ArbIndexWriter.py        # 图表指标写入 (68)
│   ├── preflight.py             # 实盘启动自检 (208)
│   ├── regime_detector.py       # m5 regime 检测(Hurst/ADF/ADX/MA) (382)
│   ├── auto_profile.py          # 日线 profile 自动选择 R²>0.55→trending (213)
│   └── pipeline/                # Pipeline Steps (~750 行 / 7 文件)
│       ├── context.py           # SignalContext 上下文 (87)
│       ├── regime_dispatch.py   # Step1 regime 分发 (50)
│       ├── regime_weight.py     # Step2 协整门控+动量确认 (93)
│       ├── persist_filter.py    # Step3 信号持续性过滤 (39)
│       ├── entry_judge.py       # Step4 入场判定(含 trend_blocked 软衰减) (85)
│       ├── exit_judge.py        # Step5 出场判定(regime 驱动) (208)
│       └── pipeline.py          # Pipeline 组合器 (145)
│
├── factors/                     # 因子层 (1,306 行 / 7 文件)
│   ├── FactorBase.py            # 因子基类 + normalize 工具 (106)
│   ├── FactorEngine.py          # YAML 驱动因子发现 + regime 权重 (385)
│   ├── stat_factors.py          # ZScore / CointResidual / HalfLife (238)
│   ├── momentum_factors.py      # SpreadMomentum (79)
│   ├── trend_factors.py         # MACDSpread/ADXSpread/ChannelBreak/TrendStrength/R2Profile (300)
│   └── vol_price_factors.py     # VWAPDev/OBVSpread/VolRatio/VolPriceDiv (185)
│
├── tools/                       # 离线分析工具 (398 行)
│   ├── BacktestAnalyzer.py      # 回测结果指标计算 (214)
│   └── PairScreener.py          # 品种对协整/经济性筛选 (184) [未集成]
│
├── scripts/                     # 离线脚本
│   ├── walk_forward.py          # WF 验证 (199) — 多折分割+过拟合诊断 ✅
│   ├── _wf_worker.py            # WF 子进程 worker (绕开 WtBtEngine singleton)
│   ├── factor_ic_analysis.py    # 因子 IC 分析 (374) [独立未联动]
│   ├── profile_backtest.py      # 单 profile 回测
│   ├── check_coint_by_year.py   # 按年协整稳定性
│   ├── compare_spreads.py       # 价差对比
│   ├── verify_spread_mr.py      # 价差均值回归性
│   ├── analyze_signal_direction.py
│   └── show_results.py
│
├── config/                      # 分层配置
│   ├── base/                    # 公共默认值
│   │   ├── execution.yaml       # 执行参数(超时/重试/最小bar)
│   │   ├── regime.yaml          # RegimeDetector + REGIME_WEIGHT_MAP
│   │   ├── signal.yaml          # SignalPipeline 阈值
│   │   ├── risk.yaml            # RiskMgr 参数
│   │   └── factors.yaml         # 因子默认值
│   ├── contracts/               # 品种对配置(legs + ratios)
│   │   ├── l_pp.yaml            # DCE.l ↔ DCE.pp
│   │   └── rb_hc.yaml           # SHFE.rb ↔ SHFE.hc
│   ├── profiles/                # 参数 profile (auto_profile 自动选)
│   │   ├── trending_contract.yaml  # 趋势性合约: entry_threshold↑ / coint 门控放宽
│   │   └── mr_contract.yaml        # 均值回归合约: entry_threshold↓ / coint 门控严
│   ├── experiments/             # 实验配置 = 合约 × 月份 × profile 三元组
│   │   ├── default.yaml
│   │   ├── l_pp_2509.yaml / l_pp_2609.yaml
│   │   ├── rb_hc_2510.yaml / rb_hc_2601.yaml
│   │   ├── matrix_{l_pp,rb_hc}_{2601,2609}_{auto,mr,trending}.yaml  (12 个)
│   │   └── profile_p3_6.yaml
│   ├── strategy_config.yaml     # [DEPRECATED] 仅作 runBT/runLive fallback
│   └── factor_config.yaml       # 因子启用清单(总开关)
│
├── docs/                        # 文档
│   ├── PROJECT_GUIDE.md         # 项目说明 + 数据流
│   ├── CONFIG_GUIDE.md          # YAML schema + profile + experiment 用法
│   ├── WT_INFRA_GUIDE.md        # WT 基础设施配置 + 凭据安全
│   ├── STRATEGY_DESIGN_BASELINE.md
│   ├── PROJECT_ASSESSMENT_20260605.md
│   ├── PROJECT_ASSESSMENT_20260609.md  # ← 最新评估报告
│   └── archive/                 # 历史架构文档
│
├── storage/                     # 本地数据缓存
│   ├── csv/                     # KS S3 → CSV(带点文件名,WT 回测要求)
│   └── his/{day,min5}/{SHFE,DCE}/  # WT DSB 二进制行情
│
├── tests/                       # 18 个测试文件 / 153 用例 ✅
│
├── schemas.py                   # 所有 dataclass (ArbCoreConfig / Leg / Risk / Signal / Regime / SignalResult / FactorOutput)
├── runBT.py                     # 回测入口 (单 experiment)
├── runLive.py                   # 实盘入口 (含 preflight)
├── run_batch.py                 # 批量跑(老入口)
├── run_multi.py                 # 多合约同进程(老入口)
└── run_matrix.py                # ✅ 实验矩阵化 — 笛卡尔积 + 子进程隔离 + 汇总
```

---

## 架构概览

### 总体设计哲学

- **单策略实例 = 单 ArbCore = 单品种对**。多品种用多策略实例(WT 层并行)，避免共享状态导致的耦合。
- **分层配置**：base ← contract ← experiment ← profile 四层覆盖，跑实验改 experiment YAML 不改代码。
- **Pipeline 化决策**：信号生成拆为 5 个独立 Step，每步只做一件事，便于单步替换和单测。
- **子进程隔离实验**：`WtBtEngine` 是 `@singleton` 同进程只能跑一次，所有矩阵/WF 都走子进程。
- **Regime 驱动 + auto_profile**：m5 regime (range/mr/trend) 影响因子权重和信号衰减；日线 R² 驱动 profile 选择影响阈值。
- **诊断优先**：DiagLogger 全量 JSONL 埋点，因子/风控/交易事件可回溯。

### 调用链

```
WT BaseCtaStrategy.on_calculate(context)
   │
   ▼
SpreadArbStrategy.on_calculate(context)
   │  (适配层，把 WT context 喂给 ArbCore)
   ▼
ArbCore.process(context, cur_date)
   │
   ├─ 1. sync_state()              ← 持仓与状态机一致性、状态机推进、超时保护
   ├─ 2. _recheck_profile()        ← 实盘每 N 日重算日线 profile (回测一次定)
   ├─ 3. compute_decision()        ← 主流水线: DataPipe → Regime → Factor → Signal → Risk
   ├─ 4. execute_decision()        ← ArbExecHelper 下单
   └─ 5. persist_state()           ← StateManager 持久化
```

---

## 决策流水线

### compute_decision() 内部流水线

```
┌─────────────────────────────────────────────────────────────────────┐
│ DataPipe.fetch()                                                    │
│   → BarData(prices, volumes, spread, aux_bars, spread_trend_filter) │
└──────────────────────────┬──────────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│ SignalGen.detect_regime(spread, aux_bars, coint_p)                  │
│   RegimeDetector(Hurst+ADF+ADX+MA 投票) + 5-bar 记忆                 │
│   → regime_dict {regime, confidence, direction}                     │
└──────────────────────────┬──────────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│ FactorEngine.apply_regime_weights(regime)                           │
│   按 REGIME_WEIGHT_MAP 调整 momentum/reversal/neutral 权重 + 翻转位  │
└──────────────────────────┬──────────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│ FactorEngine.calculate()                                            │
│   8 个启用因子并行计算 → 加权聚合                                     │
│   → FactorOutput {total_score, factor_scores, direction, coint_p}   │
└──────────────────────────┬──────────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│ SignalGen.generate_with_regime()                                    │
│   SignalPipeline 5-Step:                                            │
│     Step1 RegimeDispatchStep   regime 信号分发(trend boost/mr/range) │
│     Step2 RegimeWeightStep     协整门控 + 动量确认                     │
│     Step3 PersistFilterStep    持续性过滤(OR_2of3)                    │
│     Step4 EntryJudgeStep       入场判定(含 trend_blocked 软衰减 ×0.5)  │
│     Step5 ExitJudgeStep        出场判定(regime 驱动 + 浮盈追踪止盈)    │
│   → SignalResult {entry_signal, should_trade/exit/reverse}          │
└──────────────────────────┬──────────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│ RiskMgr.check()                                                     │
│   spread_deviation / max_loss / profit_take / time_stop /           │
│   coint_quality 5 条规则                                             │
└──────────────────────────┬──────────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│ ArbExecHelper.execute()                                             │
│   状态机 IDLE→OPENING→OPENED→CLOSING→IDLE                            │
│   追单/重试/反转/紧急全平                                              │
└──────────────────────────┬──────────────────────────────────────────┘
                           ▼
                  context.stra_set_position()
```

### 状态机

```
                  open signal
        IDLE ────────────────► OPENING ──┐
         ▲                       │       │ 超时→重试(≤3)
         │                       │       │ 失败→紧急全平
         │       平仓完成        ▼       │
         └─── CLOSING ◄──── OPENED ◄────┘
                ▲             │
                │             │ exit/reverse/risk
                └─────────────┘
                              │
                              └─ reverse: CLOSING 完成立即新方向 OPENING
```

---

## 核心模块说明

### strategies/

| 模块 | 行数 | 职责 |
|---|---|---|
| **ArbCore** | 415 | 套利核心流水线协调器，5 阶段 process()；持有 dp/sc/fe/sg/rm/eh/sm/diag 全部组件 |
| **ArbCoreLifecycle** | 119 | WT on_init/on_bar 回调适配；处理 isMain 单 _main_key 限制 |
| **SpreadArbStrategy** | 339 | WT BaseCtaStrategy 适配层；ConfigResolver 合并配置；构建 ArbCore |

### helpers/ 组件层

**核心组件**:

| 模块 | 行数 | 职责 |
|---|---|---|
| **DataPipe** | 268 | 从 WT context 拉 m5 主K + d1 辅K；用 SpreadCalculator 计价差；输出 BarData |
| **SpreadCalculator** | 152 | 名义价值归一化 (`norm_coeff = ratio * multiplier`)；维护滚动 mean/std + 上下轨 |
| **SignalGen** | 121 | 装配 RegimeDetector + SignalPipeline；管理 trailing 状态 |
| **RiskMgr** | 311 | 5 条内置规则 + 插件 `register_rule()`；冷却期/peak 追踪 |
| **ArbExecHelper** | 364 | 状态机实现；目标仓位计算；追单超时+重试+反转+紧急全平 |
| **StateManager** | 159 | CtaContext.user_save_data/user_load_data 持久化；JSON 序列化复杂类型 |
| **ConfigResolver** | 364 | base ← contract ← experiment ← profile 四层 YAML 合并 |
| **DiagLogger** | 169 | JSONL 写入(factor/risk/trade)，按日轮转 |
| **regime_detector** | 382 | m5 regime 检测：Hurst(0.30) + ADF(0.25) + ADX(0.25) + MA(0.20) 加权投票 |
| **auto_profile** | 213 | 日线 spread R² 测试：R²>0.55→trending_contract / 否则→mr_contract |
| **preflight** | 208 | 实盘启动自检：行情连通/合约信息/历史数据/凭据存在性 |

**pipeline/ — 信号 5-Step**:

| Step | 文件 | 职责要点 |
|---|---|---|
| 1 | `regime_dispatch.py` | trend regime 顺势 ×1.3 / 逆势 ×0.3；mr 透传；range ×0.8 |
| 2 | `regime_weight.py` | mr regime 下 coint_p≥0.10→×0.49 衰减、≥0.05→×0.7；动量未确认 ×0.7 |
| 3 | `persist_filter.py` | 最近 3 bars 方向投票，OR_2of3；强信号(>1.5x) 跳过 |
| 4 | `entry_judge.py` | trend regime 中逆势开仓 ×0.5 软衰减 (P0-γ 硬阻断现 unreachable) |
| 5 | `exit_judge.py` | 反转门槛 = entry_threshold；trend/mr/range 三套出场策略；v7 浮盈追踪止盈安全网 |

### factors/ 因子体系

**FactorBase**: 提供 `_normalize()` clip 归一化、`direction_type` 三态、refit hook。

**FactorEngine 加权聚合**: `total_score = Σ(score_i × effective_weight_i × flip_i) / Σ|effective_weight_i|`

**REGIME_WEIGHT_MAP**:

| regime | momentum 因子 | reversal 因子 | neutral |
|---|---|---|---|
| trend | ×1.5, 不翻转 | ×0.5, 不翻转 | ×1.0 |
| mr | ×0.5, **翻转** | ×1.5, **翻转** | ×1.0 |
| range | ×0.3 | ×0.3 | ×0.5 |

**8 个启用因子**:

| 因子 | 类型 | 默认权重 | 说明 |
|---|---|---|---|
| `zscore` | reversal | 0.25 | 滚动 Z 标准化(60) |
| `coint_residual` | reversal | 0.20 | 协整残差 Z(200)，5 天 refit |
| `half_life` | reversal | 0.10 | 半衰期 × 偏离方向(120) |
| `spread_momentum` | momentum | 0.15 | 快/慢均差(10/30) |
| `macd_spread` | momentum | 0.10 | MACD(12/26/9) |
| `adx_spread` | momentum | 0.05 | 简化 ADX(14) |
| `channel_break` | momentum | 0.05 | 通道突破(20) |
| `trend_strength` | momentum | 0.10 | 斜率/std(10/30) |

`vol_price_factors` 4 个因子配置中保留但默认禁用。

### tools/ + scripts/

| 工具 | 用途 | 状态 |
|---|---|---|
| `tools/BacktestAnalyzer.py` | 回测结果指标计算(夏普/MDD/胜率/PF) | 已集成 |
| `tools/PairScreener.py` | 品种对协整 + 经济性预筛 | **未集成主流程** |
| `scripts/walk_forward.py` | N 折时间序列切片 + 子进程 + 过拟合诊断 | ✅ |
| `scripts/factor_ic_analysis.py` | 因子 IC + IR | **未联动信号** |
| `scripts/profile_backtest.py` | 单 profile 快速回测 | |
| `scripts/check_coint_by_year.py` | 按年协整稳定性 | |
| `run_matrix.py` | **实验矩阵 笛卡尔积 pairs × months × profiles** | ✅ |

---

## 配置体系

### 分层覆盖顺序

```
base/*.yaml (默认)
   ↓ override
contracts/{pair}.yaml (合约对特定)
   ↓ override
experiments/{exp}.yaml (实验级)
   ↓ override
profiles/{trending,mr}_contract.yaml (auto_profile 选)
   ↓ runtime
ConfigResolver.resolve() → ArbCoreConfig / RiskConfig / SignalConfig / RegimeConfig
```

### profile 机制

- **trending_contract**: entry_threshold ↑、coint 门控放宽、范围内做空门槛 ↑
- **mr_contract**: entry_threshold ↓、coint 门控严、信号衰减加重
- **auto** (默认): 日线 spread R² 滚动测试，>0.55 选 trending；回测开始时一次定，实盘每 N 日重算

**已验证现象**:
- `l_pp_2609 + auto/trending`：唯一正样本 (+299)
- `l_pp_2609 + mr`：高频烧费 (-1268)
- `rb_hc_2601 + 任意 profile`：结构性亏损 (-2100~-2300)

详见 `docs/CONFIG_GUIDE.md` 和 skill `cta-spread-arb-param-profile`。

---

## 使用方法

### 1. 单次回测

```bash
cd /mnt/d/gf_pc/WonderTrader/wtpy/demos/cta_spread_arb
python runBT.py                           # 使用 config/strategy_config.yaml (fallback)
# 或指定实验
python runBT.py --experiment l_pp_2609    # 加载 config/experiments/l_pp_2609.yaml
```

### 2. 实验矩阵 (推荐)

```bash
# pairs × months × profiles 笛卡尔积、子进程隔离、自动汇总
python run_matrix.py -p l_pp rb_hc --months 2601 2609 \
                     --profiles auto mr trending \
                     --parallel 4

# 输出: matrix_results/<timestamp>/{*.json, summary.csv, failed.txt}
```

### 3. Walk-Forward 验证 (防过拟合)

```bash
# 4 折时间序列分割 + 子进程 + flip_count 诊断
python scripts/walk_forward.py --experiment l_pp_2609 --folds 4

# 输出严重过拟合警告示例:
#   ⚠️ 2/4 折 Train/Test 方向相反 (Fold3: +115/-790, Fold4: -1065/+975)
#   SUM train +70 / test +25  ← alpha 不稳定
```

### 4. 实盘 (尚未生产就绪)

```bash
python runLive.py    # 包含 preflight 自检
```

### 5. 凭据注入 (严禁打印)

```bash
# ❌ 错误: 直接 cat / read / python -c print
# ✅ 正确: 子进程 mktemp + trap + source
tmp=$(mktemp) && trap "rm -f $tmp" EXIT && \
  cp /path/to/credentials.json "$tmp" && \
  source <(python -c "...") && python runLive.py
```

详见 skill `cta-spread-arb-architecture` → `refs/config-secrecy-workflow.md`。

---

## 当前回测结果

### 实验矩阵 (2×2×3 = 12 实验)

| pair | month | profile | net PnL | 备注 |
|---|---|---|---|---|
| **l_pp** | **2609** | **auto / trending** | **+299** | **唯一正样本** ✅ |
| l_pp | 2609 | mr | -1268 | 高频烧费 |
| l_pp | 2601 | * | exit=11 数据缺失 | |
| rb_hc | 2601 | auto / mr | -2283 | 结构性亏损 |
| rb_hc | 2601 | trending | -2130 | 同上 |
| rb_hc | 2609 | * | exit=11 数据缺失 | |

### Walk-Forward (l_pp 2609, 4 折)

| Fold | Train | Test | 方向一致 |
|---|---|---|---|
| 1 | +1030 | +30 | ✅ |
| 2 | -10 | -190 | ✅ (都亏) |
| 3 | +115 | **-790** | ❌ 反向 |
| 4 | **-1065** | **+975** | ❌ 反向 |
| **SUM** | **+70** | **+25** | **alpha ≈ 0** |

**结论**: 全区间 +299 是过拟合产物，4 折中 2 折 Train/Test 方向相反，SUM 趋近 0。

### 品种经济性对比 (诊断根因)

| 指标 | rb/hc 2601 | l/pp 2609 |
|---|---|---|
| 单轮 fee (开+平) | 4.14 | 2.72 |
| spread 波幅 (avg_win) | 168 | 280 |
| fee / avg_win | **1.27%** | **0.49%** |
| 盈亏平衡胜率 | 51.3% | 50.5% |
| 实际胜率 | 50.5% | 50.3% |

rb fee 按金额收取 (万 1，~3.2 元/手)，hc 按手数 (~1 元/手)。rb/hc spread 波幅只有 l/pp 的 60%，叠加 fee 比 1.57 倍 → **fee/avg_win 是 l/pp 的 2.5 倍**。这不是调参能解决的，是品种经济性问题。

---

## 已知问题

### 🔴 Critical (alpha 层根因)

1. **l/pp 信号 WF 严重过拟合** — 2/4 折 Train/Test 方向相反，全区间 +299 不可信。需协整漂移检测 / IC 自适应权重 / 换 pair。
2. **rb/hc 结构性亏损** — 品种经济性问题，fee/avg_win 1.27%，调参无效。保留做对照样本。

### 🟠 High (代码 bug，审计发现)

3. **`FactorBase._append_rank_value()` 从未被调用** — `rank_normalize` 生产路径休眠（`use_rank_v2=True` 时有测试锁定，非字面死代码；所有 YAML 因子均未开启）。修复方向：要么删 rank 路径，要么挂上调用。
4. ~~**`ArbCore._auto_profile_cfg` 未从 `SpreadArbStrategy` 传入**~~ — ✅ 已修复(2026-07 A-5): 构造函数真正传入，`_recheck_profile` 死分支消除。
5. ~~**`CointResidualFactor` refit backoff 反转**~~ — ✅ 已修复(A3): 指数递增退避 + 32x cap。另：2026-07 B-4 修复 refit 间隔 YYYYMMDD 跨月/跨年算术失真。
6. ~~**4 个 `vol_price_factors` 缺 `DIRECTION_TYPE`**~~ — ✅ 已修复(A4): Vwap/VPD=reversal, OBV/VolRatio=momentum。
7. ~~**`ArbExecHelper.check_state_timeout` 忽略回测模式**~~ — ✅ 已修复(2026-07 C-1): 回测走 bar 计数(`timeout_bars`), 与机器速度无关, 回测可复现。
8. ~~**`exit_judge` 5 个 PnL 阈值硬编码**~~ — ✅ 已修复(2026-07): 全部参数化到 `SignalConfig` (mr_profit_tier1/tier2, mr_loss_scale, pnl_trailing_tier1/tier2)，YAML 可覆盖。
9. **P0-γ trend_block 硬阻断 unreachable** — `entry_judge` ×0.5 软衰减已挡死 `should_trade`，硬阻断永远走不到。代码保留+注释，单测保留作回归。

### 🟡 Medium (架构/重复)

10. ~~**`run_multi.py` + `run_batch.py` + `walk_forward.py` 入口职责重叠**~~ — 文件已删除，由 `run_matrix.py` 统一（⚠️ `--mode session` 已废弃：WtBtEngine 单 context 设计导致回调路由错误，结果不可信，用 serial/subprocess）。
11. **`PairScreener` 未集成主流程** — 跑过的实验里 rb/hc 经济性问题如果 preflight 阶段筛掉，本可避免几小时实验。
12. **`factor_ic_analysis.py` 374 行独立脚本** — 不联动信号生成，无法自动降权弱因子。
13. **`strategy_config.yaml` 标记 DEPRECATED 但仍是 runBT/runLive 默认入口** — 两份维护，易漂移。
14. **WT 日期解析 bug**：2025 → 3509，影响 log/jsonl 文件名但不影响 PnL。

### 🟢 Low (运行环境)

15. **实盘 0 验证** — runLive.py 未做端到端干跑。按用户拍板推迟。

---

## 优化路径

### 已完成 ✅

- v10 架构: ArbCore 单核 + Lifecycle 拆分
- Pipeline 化 5-Step 信号生成
- 分层 YAML + auto_profile + experiment 体系
- DiagLogger 全量埋点
- WtBtEngine singleton 限制的子进程隔离方案
- `run_matrix.py` 实验矩阵化 + `walk_forward.py` 过拟合诊断
- C2 诊断: rb/hc 品种经济性根因定位
- 188/188 测试全绿
- 2026-07 四维评估修复批 (docs/PROJECT_ASSESSMENT_20260729.md, 209/209 测试全绿):
  - A-1 run_matrix session 模式废弃 (单context引擎回调路由错误, 结果不可信)
  - C-1 check_state_timeout 回测 bar 计数 (消除回测不可复现)
  - B-4 refit 间隔跨月/跨年日期算术修复
  - B-1 双重追踪止盈合并 (删 RiskMgr profit_take, 保留 ExitJudge 软止盈)
  - B-2 协整 entry gating 前移 compute_decision (原 block-entry 死分支)
  - C-2 配置单源化 (signal_mode 生效/from_yaml 默认值单源/死字段删除/
    experiment级覆盖显式报错/RegimeConfig 补 detector 参数)
  - B-3 反转开仓补入场校验 (persist filter + blocked_hours veto)
  - A-2/A-3 实盘恢复 on_tick tick级状态机推进 (回测保持bar级)
  - P-1 align_series 向量化 (intersect1d+searchsorted) + coint 模式去重 join
  - P-3 回测跳过 write_indicator 构造 / P-5 StateManager dirty-flag
  - C-3 持仓/盈亏同bar取数去重 (compute→RiskMgr.check/indicators 复用)
  - P-6 删除 on_calculate 每bar日志 / A-5 auto_profile_cfg 透传
  - P-8 DiagLogger 60s 兜底 flush / P-4 ArbIndexWriter 缓冲批量写
- 2026-07 剩余项收尾 (224/224 测试全绿):
  - P-2 因子向量化: MACD EMA/RSI/ADX 纯Python循环 → lfilter+sliding_window_view
    (EMA位级一致, Wilder数值等价; 趋势族启用不再有性能悬崖)
  - C-4/C-5/C-6: regime状态归一SignalGen / 冗余接口deprecated / 权重重建合一
  - B-5 PairScreener 品种经济性准入 (fee/avg_win<0.8%, 拦截rb/hc类组合)
  - B-6 理论vs实际PnL对账 (session_end输出gap=滑点+执行损耗)
  - A-4 runLive 多实例 (--experiment逗号分隔, marker合并写入)
- 2026-07 修复批 (审计复核后):
  - B2 价差时间戳对齐 (bartimes join, 缺 timestamps 降级截断)
  - B5 `_mr_exit` 浮亏降阈等回归 (行为确认, 注释修正+5测试锁定)
  - B6 regime 切换不再重置 `_peak_pnl` (消除追踪止盈真空)
  - B11 SignalGen.entry_threshold setter 穿透到 pipeline steps (原死代码+实盘 AttributeError 隐患)
  - B12 反转门槛加 1.2x 缓冲 (reverse_buffer_multiplier)
  - C1 exit_judge PnL 阈值全参数化
  - B4 风控规则优先级重排 (coint_quality 提前)
  - D2 unrealized_pnl 信号路径去重计算
  - A8 ArbDecision 移至 schemas.py
  - B7 复核后回退: 报告前提不成立(detect_regime 不消费 coint_p), 重排反致权重滞后一 bar

### 短期 (按 ROI 排序)

| 优先级 | 项 | 工作量 | 解锁价值 |
|---|---|---|---|
| **P1-A** | 修审计发现的 High bugs (Issues #3-#8) | 1 天 | 消除死代码/逻辑反转/硬编码 |
| **P1-B** | `PairScreener` 接入 preflight (fee/avg_win > 阈值警告) | 2h | 防 rb/hc 类品种重复踩坑 |
| **P2-A** | **多周期多窗口分析框架** (用户提出) | 2-3 天 | 找到品种在哪些 TF×window 上 trend/mr 效果最好 |
| **P2-B** | l/pp WF 失稳根因诊断 (协整漂移 / regime 误判) | 1 天 | 找到 alpha 修复点 |
| **P3-A** | `factor_ic_analysis` 接入 SignalGen 自动降权 | 1 天 | 期望 +20% alpha |
| **P3-B** | regime_detector lookback 60/100/150 对照 | 4h | 期望 +5~10% |

### 中期

- 退役 `run_multi.py` / `run_batch.py` / `strategy_config.yaml`，统一 `run_matrix.py` + experiments
- `exit_judge` PnL 阈值提取到 `signal.yaml`
- `vol_price_factors` 补 DIRECTION_TYPE 后逐个回测启用 / 删除
- 实盘干跑 + preflight 增强

### 长期

- Cross-validation 增强 (Embargoed Walk-Forward, Combinatorial Purged)
- 多策略组合 (相关性约束 + 资金分配)
- L1/L2 正则化因子权重 (代替手工 REGIME_WEIGHT_MAP)
- 实盘准入 (paper trading 1 个月)

---

## 开发约定

### 数据源铁律

- **KS S3 是唯一可信数据源**，缺数据报"无"，**严禁 m5 → d1 合成**
- 真伪日线判据：KS S3 `kline_1d/` 目录是否存在 (m5_sum_volume == daily_volume 不是伪线证据，期货定义)
- `future_code_list.json` GBK 编码权威源 (HOT = 纯 OI 最大启发)

### WT 集成陷阱

- `WtBtEngine` 是 `@singleton`，**同进程多次 init() exit=11 崩溃** → walk-forward 必子进程
- `stra_get_time()` 返回 HHMM 整数 (如 930)，不是秒数
- CTA 策略无 cancel API (只 HftContext 有)
- `user_load_data` 默认 `vType=float`，整数字段需指定 `int`
- `write_indicator` time 格式: `date*10000 + time`
- `stdCode` 三段式 `EXCHG.product.contract` (SHFE.rb.rb2601)，DSB 不带点
- `_normalize(raw, clip_val=3.0)` clip 到 [-3,3] 后除以 3 → [-1,1]

### 凭据安全

- credentials.json 值**严禁入对话**：禁 cat / read_file / print / `python -c print`
- 一般 config YAML 同此禁，复核走 read_file 行号通道
- 仅子进程 `mktemp + trap + source` 注入 env

### 代码改动约定

- 任何架构变更类请求 → 先输出 **诊断+方案+权衡+待拍板问题** 文档，不直接 patch
- 清理噪声文件 `mv → _trash/`，**不批量 rm -rf**
- 修改 yaml 后必须确认 base/ 与 strategy_config.yaml 同步 (现两份漂移问题待统一)

### 相关文档

- `docs/PROJECT_GUIDE.md` — 项目说明 + 数据流详解
- `docs/CONFIG_GUIDE.md` — YAML schema + profile 用法
- `docs/WT_INFRA_GUIDE.md` — WT 基础设施 + 凭据安全
- `docs/STRATEGY_DESIGN_BASELINE.md` — 策略设计基线
- `docs/PROJECT_ASSESSMENT_20260609.md` — 最新评估报告
- Skill `cta-spread-arb-architecture` — 架构详解 + 评估模板
- Skill `cta-spread-arb-param-profile` — profile 机制细节
