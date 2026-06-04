# CTA跨品种套利策略 — 项目说明文档

> 版本: v8 (2025-05 审查优化后)
> 框架: WonderTrader/wtpy
> 品种: rb-i (螺纹钢-铁矿石, 5分钟级别)

---

## 一、项目概述

本项目实现了一个**CTA跨品种套利策略引擎**，核心思路是：

1. **价差建模** — 对两个相关品种(如rb/i)构建协整价差，捕捉均值回归机会
2. **Regime自适应** — 根据市场状态(trend/mr/range)动态调整因子权重和出场策略
3. **5步信号管道** — RegimeDispatch → RegimeWeight → PersistFilter → EntryJudge → ExitJudge
4. **状态机执行** — idle → opening → opened → closing → idle，完整覆盖开仓/追单/平仓/反转
5. **插件化风控** — 6条内置规则 + 自定义规则注册

### 核心数据流

```
K线数据
  │
  ▼
DataPipe ─── 获取K线 → 计算价差 → 日线趋势过滤 → 返回BarData
  │
  ▼
RegimeDetector ─── 4维度加权投票(Hurst/ADF/ADX/MA) → regime(trend/mr/range)
  │
  ▼
FactorEngine ─── 8个因子计算 → regime自适应权重调整 → 加权聚合 → FactorOutput
  │
  ▼
SignalPipeline ─── 5步管道处理 → SignalResult(direction/should_trade/should_exit/should_reverse)
  │
  ▼
RiskMgr ─── 6条风控规则检查 → RiskContext(pass/触发平仓)
  │
  ▼
ArbExecHelper ─── 状态机执行(开仓/追单/平仓/反转)
```

---

## 二、目录结构

```
cta_spread_arb/
├── schemas.py                    # 统一数据结构定义(10个dataclass)
├── runBT.py                      # 回测入口
├── runLive.py                    # 实盘入口
├── analyze_bt.py                 # 回测分析脚本
│
├── config/
│   ├── strategy_config.yaml      # 策略参数配置(regime/signal/risk/execution/strategies)
│   └── factor_config.yaml        # 因子参数配置(8个因子+权重+聚合方式)
│
├── strategies/
│   ├── SpreadArbStrategy.py      # 主策略(协调者, 遍历SubStrategy)
│   └── SubStrategy.py            # 子策略(单品种组合的完整流水线)
│
├── helpers/
│   ├── ConfigManager.py          # 配置管理(加载YAML+校验+结构化对象)
│   ├── DataPipe.py               # 数据管道(K线获取+价差计算+趋势过滤)
│   ├── ArbExecHelper.py          # 执行协调器(5状态状态机)
│   ├── RiskMgr.py                # 风控管理(6条内置规则+插件注册)
│   ├── SpreadCalculator.py       # 价差计算(price/coint两种模式)
│   ├── SignalGen.py              # 信号生成器门面(RegimeDetector+Pipeline)
│   ├── StateManager.py           # 状态持久化(重启恢复)
│   ├── regime_detector.py        # 市场状态检测(4维度加权投票)
│   ├── DiagLogger.py             # 诊断日志(交易/风控/因子三类)
│   ├── utils.py                  # 工具函数
│   │
│   └── pipeline/                 # 信号管道(5步)
│       ├── context.py            #   SignalContext(Steps间传递的可变状态)
│       ├── regime_dispatch.py    #   Step 1: Regime信号分派
│       ├── regime_weight.py      #   Step 2: Regime权重调整+确认
│       ├── persist_filter.py     #   Step 3: 信号持续性过滤
│       ├── entry_judge.py        #   Step 4: 入场判断(衰减+阈值+门控)
│       ├── exit_judge.py         #   Step 5: 出场判断(regime驱动)
│       └── pipeline.py           #   管道编排器
│
├── factors/
│   ├── FactorBase.py             # 因子基类(统一接口+标准化管道)
│   ├── FactorEngine.py           # 因子引擎(聚合+regime自适应权重)
│   ├── stat_factors.py           # 统计因子(ZScore/CointResidual/HalfLife)
│   ├── momentum_factors.py       # 动量因子(SpreadMomentum)
│   ├── trend_factors.py          # 趋势因子(MACD/ADX/ChannelBreak/TrendStrength)
│   └── vol_price_factors.py      # 量价因子(VWAP/量价背离/OBV/量比, 当前禁用)
│
├── scripts/                      # 工具脚本 (P2-clean 后保留项)
│   ├── analyze_signal_direction.py  # 信号方向分析
│   ├── check_coint_by_year.py       # 协整年度核查
│   ├── compare_spreads.py           # spread 对比
│   ├── verify_spread_mr.py          # 均值回归验证
│   └── show_results.py              # 回测结果展示
│   # 退役脚本(grid_search/optimize_v6/diagnose_*/c3/c6/migrate_v8_to_v9)
│   # 已于 2026-06-04 移至 _trash/p2_clean_20260604/
│
└── docs/                         # 架构文档
```

---

## 三、核心业务逻辑

### 3.1 价差计算 (SpreadCalculator)

支持两种价差模式，由strategy_config.yaml的`spread_mode`控制：

| 模式 | 公式 | 适用场景 |
|------|------|----------|
| `price` | spread = Σ(coeff_i × price_i) | 简单加权价差 |
| `coint` | spread = p1 - β×p2 - c | 协整残差价差(推荐) |

**coint模式关键逻辑：**
- β和c由CointResidualFactor通过OLS回归+ADF检验动态估计
- 协整不显著(p≥0.05)时，自动降级为price spread z-score
- refit间隔由`refit_interval`控制(默认4根m5=20分钟)
- refit失败时指数退避(interval减半)

**归一化系数：** 将不同量级的品种价格归一化到可比范围，用于因子计算。
**PnL系数：** 考虑合约乘数和手数，用于盈亏计算。

### 3.2 Regime检测 (RegimeDetector)

通过**4维度加权投票**判断市场状态：

| 维度 | 权重 | 检测方法 | trend判定 | mr判定 |
|------|------|----------|-----------|--------|
| Hurst指数 | 0.30 | R/S法 | H>0.55 | H<0.45 |
| ADF检验 | 0.25 | statsmodels | p≥0.10 | p<0.05 |
| ADX指标 | 0.25 | DMI计算 | ADX>25 | ADX<20 |
| MA趋势 | 0.20 | 短MA vs 长MA | 短MA>长MA | — |

**投票机制：**
- 每个维度返回+1(trend)/-1(mr)/0(不确定)
- 加权求和 → score > 0.2 为trend，score < -0.2 为mr，否则range
- **regime记忆：** 保留最近N bar历史，平滑regime切换(避免频繁跳变)
- **decay-not-block原则：** regime切换需足够置信度，否则保持上次regime

### 3.3 因子体系 (FactorEngine + Factors)

#### 因子分类与方向语义

| 方向类型 | 含义 | 因子 | 信号解读 |
|----------|------|------|----------|
| `reversal` | 均值回归 | ZScore, CointResidual, HalfLife | 正值=spread偏高→做空回归 |
| `momentum` | 趋势动量 | SpreadMomentum, MACD, ADX, ChannelBreak, TrendStrength | 正值=spread上升动量→做多顺势 |
| `neutral` | 中性确认 | VWAP, 量价背离, OBV, 量比 | 辅助确认，不独立决策 |

**因子输出语义统一：**
- 正值 = spread偏高 / 上升动量
- 负值 = spread偏低 / 下降动量
- 方向翻转由FactorEngine根据regime统一处理，因子自身不包含方向假设

#### 当前启用因子 (factor_config.yaml)

| 因子 | 类 | 方向 | 权重 | lookback | 说明 |
|------|-----|------|------|----------|------|
| zscore | ZScoreFactor | reversal | 0.25 | 20 | 滚动z-score |
| coint_residual | CointResidualFactor | reversal | 0.20 | 20 | 协整残差z-score |
| half_life | HalfLifeFactor | reversal | 0.10 | 20 | MR半衰期强度 |
| spread_momentum | SpreadMomentumFactor | momentum | 0.15 | 20 | 价差动量 |
| macd_spread | MACDSpreadFactor | momentum | 0.10 | — | MACD柱状图 |
| adx_spread | ADXSpreadFactor | momentum | 0.05 | — | ADX方向强度 |
| channel_break | ChannelBreakFactor | momentum | 0.05 | — | 通道突破 |
| trend_strength | TrendStrengthFactor | momentum | 0.10 | — | 多周期MA一致性 |

**MR因子权重合计0.55，动量/趋势因子0.45** — 偏MR但保留趋势捕获能力。

#### Regime自适应权重调整

FactorEngine根据当前regime动态调整因子权重和翻转标志：

| regime | momentum因子 | reversal因子 | neutral因子 |
|--------|-------------|-------------|-------------|
| trend | 权重×1.5, 不翻转 | 权重×0.5, 不翻转 | 权重×1.0 |
| mr | 权重×0.5, **翻转** | 权重×1.5, **翻转** | 权重×1.0 |
| range | 权重×0.3, 不翻转 | 权重×0.3, 不翻转 | 权重×0.5 |

**翻转含义：** mr regime下，momentum因子输出取反(spread上升动量→做空回归)，reversal因子输出取反(spread偏高→做多顺势)。所有权重乘数和翻转标志均可通过YAML配置。

#### 因子标准化管道

每个因子输出经过FactorBase._normalize()处理：
1. **优先rank标准化：** 滚动排名百分位(bisect实现)，窗口由rank_window控制
2. **fallback clip：** rank窗口不足时，clip到[-1, 1]
3. 最终输出范围[-1, 1]

### 3.4 信号管道 (SignalPipeline)

5步管道，SignalContext在Steps间传递可变状态：

```
factor_signal ──→ [Step1] ──→ [Step2] ──→ [Step3] ──→ [Step4] ──→ [Step5] ──→ SignalResult
                  Regime     Regime     Persist     Entry      Exit
                  Dispatch   Weight     Filter      Judge      Judge
```

#### Step 1: RegimeDispatch — Regime信号分派

- regime由外部(SubStrategy)前置检测并注入，本Step不再内部检测
- 固定模式(trend/mean_revert)覆盖regime
- 直接传递total_score为entry_signal(不做额外乘法，避免双重调整)

#### Step 2: RegimeWeight — Regime权重调整+确认

替代原CointGateStep + MomentumConfirmStep：

| regime | 确认逻辑 |
|--------|----------|
| trend | 检查momentum因子是否与信号同向，不确认时coint_decay=0.7 |
| mr | 协整门控(p≥0.10→decay², p>0.05→decay) + 动量反向检查 |
| range | 不额外衰减(v4修复，避免三重衰减) |

#### Step 3: PersistFilter — 信号持续性过滤

- 要求信号方向连续N bar一致(persist_bars)
- 强信号(>1.5×entry_threshold)跳过持续性检查
- persist_bars=1时不过滤(默认)

#### Step 4: EntryJudge — 入场判断

6步判断流程：

1. **确认失败衰减：** trend regime趋势不确认→信号×coint_decay；mr regime动量不确认→信号×0.7
2. **趋势逆势衰减：** trend regime下信号方向与regime_direction相反→信号×trend_counter_decay_factor(默认0.5)
3. **入场阈值：** |final_signal| > entry_threshold；做空信号×short_entry_multiplier(更严格)
4. **协整门控：** coint_p≥0.05且空仓且非trend regime→阻止开仓
5. **价差趋势过滤：** mr regime下信号方向与价差趋势同向→阻止开仓
6. **综合判断：** should_trade = 阈值通过 ∧ 空仓 ∧ 持续性通过 ∧ 协整通过 ∧ 趋势过滤通过

#### Step 5: ExitJudge — 出场判断

regime驱动的出场策略：

| regime | 出场策略 | 触发条件 |
|--------|----------|----------|
| trend | 追踪止损 | 信号从峰值回撤>trend_trailing_ratio(50%)且持续>trend_trailing_bars(3bar) |
| trend | 盈利保护 | 峰值>profit_protect_peak(0.7)且当前<profit_protect_floor(0.35) |
| trend | 极弱信号 | 信号<exit_threshold×0.5 |
| mr | 信号衰竭 | 信号<exit_threshold(亏损时阈值提升) |
| mr | 盈利保护 | 同trend |
| mr | 追踪止损 | 信号<峰值×trailing_decay_ratio且持续>trailing_exit_bars |
| range | 快速出场 | 信号<range_exit_threshold(0.4) |

**反转判断(所有regime通用)：** 信号方向与持仓方向相反 且 |信号|>entry_threshold → should_reverse=True

**MR出场盈亏维度(BUG-6修复)：** 亏损持仓时降低出场阈值，加速止损。亏损越大出场越积极。

**regime切换保护(P0-3修复)：** regime切换时重置peak追踪，避免trend→mr时peak残留导致错误平仓。

### 3.5 风控管理 (RiskMgr)

6条内置风控规则，按优先级顺序执行：

| 规则 | 条件 | 动作 | 参数 |
|------|------|------|------|
| spread_deviation | 价差偏离>阈值 | 平仓 | spread_deviation_exit=3.0σ |
| max_loss | 持仓亏损>阈值 | 平仓 | max_loss_per_sub=8000 |
| profit_take | 追踪止盈触发 | 平仓 | profit_take_trailing=0.5 |
| time_stop | 持仓时间>阈值 | 平仓 | max_hold_bars=480(2天) |
| min_hold | 持仓时间<阈值 | 仅记录 | min_hold_bars=4 |
| coint_quality | 协整p值恶化 | 平仓 | coint_p_max=0.15 |

**插件化规则注册：** `register_rule(name, rule_fn)` 支持自定义风控规则。
**冷却期：** 平仓后cooldown_bars(默认8)内不允许重新开仓。反转同样触发冷却期(不豁免)。
**状态持久化：** 通过StateManager保存/恢复RiskMgr快照，重启后自动恢复。

### 3.6 执行协调器 (ArbExecHelper)

5状态状态机：

```
idle ──[should_trade]──→ opening ──[两腿成交]──→ opened ──[should_exit/reverse]──→ closing ──→ idle
                            │                        │
                            [超时/追单]               [风控平仓]
                            ▼                        ▼
                        追单/紧急全平              close_all
```

**开仓流程(idle → opening → opened)：**
1. 根据信号方向计算两腿下单方向和手数
2. 同时下两腿限价单(对手价)
3. tick回调中检查成交：单腿成交→追另一腿；两腿成交→转入opened
4. 单腿超时(leg_timeout)→追单(最多max_retry次)
5. 追单失败→紧急全平→回到idle

**平仓流程(opened → closing → idle)：**
1. 根据should_exit/should_reverse/risk_result决定平仓方向
2. 同时下两腿平仓单
3. 两腿平仓完成→转入idle
4. 如需反转→回到idle，下一bar信号自然驱动反转开仓

**方向推断：** 从实际持仓推断当前方向(不依赖信号)，确保状态一致性。

### 3.7 子策略流水线 (SubStrategy.process)

每bar执行的核心流水线：

```
1. 仓位同步 ─── 从context读取实际持仓，推断current_direction
2. 状态机推进 ─── ArbExecHelper.on_tick_check()检查挂单成交/超时
3. 数据获取 ─── DataPipe.fetch()获取K线+计算价差+趋势过滤
4. Regime检测 ─── RegimeDetector.detect()前置检测regime
5. 因子计算 ─── FactorEngine.apply_regime_weights() + calculate()
6. 信号生成 ─── SignalGen.generate_with_regime()传入已检测regime
7. 交易决策 ─── _execute_decision()三层决策:
   a. 风控平仓(最高优先级)
   b. 开仓(空仓+should_trade)
   c. 信号平仓/反转(持仓+should_exit/should_reverse)
8. 状态持久化 ─── StateManager保存direction和RiskMgr状态
9. 诊断日志 ─── DiagLogger记录交易/风控/因子信息
```

---

## 四、配置说明

### 4.1 strategy_config.yaml

```yaml
# ============================================================
# Regime检测参数
# ============================================================
regime:
  hurst_window: 20          # Hurst指数计算窗口(根)
  adx_window: 14            # ADX计算窗口(根)
  regime_memory: 5          # regime记忆长度(平滑切换)
  # --- 权重乘数(可配置，覆盖硬编码默认值) ---
  trend_momentum_weight: 1.5   # trend regime momentum因子权重倍率
  trend_reversal_weight: 0.5   # trend regime reversal因子权重倍率
  mr_momentum_weight: 0.5      # mr regime momentum因子权重倍率
  mr_reversal_weight: 1.5      # mr regime reversal因子权重倍率
  range_momentum_weight: 0.3   # range regime momentum因子权重倍率
  range_reversal_weight: 0.3   # range regime reversal因子权重倍率
  range_neutral_weight: 0.5    # range regime neutral因子权重倍率
  # --- 翻转标志(可配置) ---
  trend_momentum_flip: false   # trend regime momentum是否翻转
  trend_reversal_flip: false   # trend regime reversal是否翻转
  mr_momentum_flip: true       # mr regime momentum翻转(关键!)
  mr_reversal_flip: true       # mr regime reversal翻转(关键!)
  range_momentum_flip: false
  range_reversal_flip: false

# ============================================================
# 信号参数
# ============================================================
signal:
  signal_mode: adaptive       # adaptive/trend/mean_revert
  entry_threshold: 0.65       # 入场信号阈值(|signal|>此值开仓)
  exit_threshold: 0.12        # 出场信号阈值(MR regime)
  direction_threshold: 0.1    # 方向判定阈值
  coint_decay: 0.7            # 协整不显著时信号衰减系数
  momentum_decay: 0.7         # 动量不确认时衰减系数
  signal_persist_bars: 1      # 信号持续性过滤(1=不过滤)
  short_entry_multiplier: 1.0 # 做空入场门槛倍率(>1更严格)
  trend_counter_decay_factor: 0.5  # 趋势逆势信号衰减系数
  # --- 出场参数 ---
  trailing_exit_bars: 6       # 追踪止损持续bar数
  trailing_decay_ratio: 0.3   # 追踪止损衰减比
  profit_protect_peak: 0.7    # 盈利保护峰值阈值
  profit_protect_floor: 0.35  # 盈利保护回落阈值
  trend_trailing_ratio: 0.5   # trend regime追踪止损比
  trend_trailing_bars: 3      # trend regime追踪止损bar数
  range_exit_threshold: 0.4   # range regime出场阈值

# ============================================================
# 风控参数
# ============================================================
risk_control:
  cooldown_bars: 8            # 平仓后冷却期(bar数)
  max_hold_bars: 480          # 最大持仓时间(480×5min=2天)
  min_hold_bars: 4            # 最小持仓时间(仅记录)
  max_loss_per_sub: 8000      # 单子策略最大亏损(元)
  spread_deviation_exit: 3.0  # 价差偏离出场阈值(σ)
  profit_take_trailing: 0.5   # 追踪止盈回撤比
  coint_p_max: 0.15           # 协整p值上限(超过则平仓)

# ============================================================
# 执行参数
# ============================================================
execution:
  leg_timeout: 30             # 单腿超时(秒/回测tick数)
  max_retry: 3                # 追单最大重试次数
  bollinger_lookback: 60      # 布林带计算窗口
  bollinger_threshold: 2.0    # 布林带宽度倍数
  min_bar_length: 20          # 最小K线数量(低于此值不计算)

# ============================================================
# 子策略配置(新增品种只需加一条)
# ============================================================
strategies:
  - name: rb_i_m5
    legs:
      - code: SHFE.rb.HOT     # 螺纹钢(主力)
        volume: 3              # 3手
        coeff: 1.0             # 价差系数(正=多头)
      - code: DCE.i.HOT       # 铁矿石(主力)
        volume: 2              # 2手
        coeff: -1.0            # 价差系数(负=空头)
    spread_mode: coint         # 价差模式: price/coint
    main_period: m5            # 主周期
    aux_period: d1             # 辅助周期(日线MA10趋势过滤)
    lookback: 20               # 因子lookback(根)
```

### 4.2 factor_config.yaml

```yaml
# 因子lookback=20根m5=100分钟
# 短窗口: MR策略持仓1-2小时, 因子窗口匹配持仓周期

signal_aggregation:
  method: weighted_sum         # 聚合方式
  entry_threshold: 0.65        # (被strategy_config覆盖)
  exit_threshold: 0.12

factors:
  - class: ZScoreFactor
    enabled: true
    name: zscore
    params:
      lookback: 20             # 滚动窗口(根)
      rank_window: 60          # rank标准化窗口
    weight: 0.25

  - class: CointResidualFactor
    enabled: true
    name: coint_residual
    params:
      lookback: 20
      refit_interval: 4        # refit间隔(根m5=20分钟)
      coint_p_threshold: 0.05  # ADF显著性阈值
      rank_window: 60
    weight: 0.20

  - class: HalfLifeFactor
    enabled: true
    name: half_life
    params:
      lookback: 20
      max_half_life: 10        # 最大半衰期(根m5=50分钟)
      min_half_life: 2         # 最小半衰期(根m5=10分钟)
      rank_window: 60
    weight: 0.10

  - class: SpreadMomentumFactor
    enabled: true
    name: spread_momentum
    params:
      lookback: 20
      rank_window: 60
    weight: 0.15

  - class: MACDSpreadFactor
    enabled: true
    name: macd_spread
    params:
      fast_period: 12
      slow_period: 26
      signal_period: 9
      rank_window: 60
    weight: 0.10

  - class: ADXSpreadFactor
    enabled: true
    name: adx_spread
    params:
      period: 14
      rank_window: 60
    weight: 0.05

  - class: ChannelBreakFactor
    enabled: true
    name: channel_break
    params:
      channel_period: 20
      rank_window: 60
    weight: 0.05

  - class: TrendStrengthFactor
    enabled: true
    name: trend_strength
    params:
      periods: [5, 10, 20]
      rank_window: 60
    weight: 0.10
```

---

## 五、数据结构 (schemas.py)

### 5.1 配置结构

| dataclass | 用途 | 关键字段 |
|-----------|------|----------|
| `SignalConfig` | 信号参数 | entry_threshold, exit_threshold, signal_mode, coint_decay, 各出场参数 |
| `RiskConfig` | 风控参数 | cooldown_bars, max_hold_bars, max_loss_per_sub, spread_deviation_exit |
| `RegimeConfig` | Regime参数 | hurst_window, adx_window, regime_memory, 各regime权重/翻转 |
| `ExecConfig` | 执行参数 | leg_timeout, max_retry, bollinger_lookback, bollinger_threshold, min_bar_length |
| `LegConfig` | 单腿配置 | code, volume, coeff |
| `SubStrategyConfig` | 子策略配置 | name, legs, spread_mode, main_period, aux_period, lookback |

### 5.2 运行时数据结构

| dataclass | 用途 | 关键字段 |
|-----------|------|----------|
| `BarData` | K线数据 | spread(np.ndarray), main_bars, aux_bars, trend_filter |
| `FactorOutput` | 因子输出 | total_score, factor_scores, direction, coint_p, signal_mode |
| `SignalResult` | 信号结果 | direction, entry_signal, should_trade, should_exit, should_reverse, regime |
| `RiskContext` | 风控结果 | should_close, reason, details |

---

## 六、审查优化记录 (2025-05)

### 6.1 优化总览

| 优先级 | 编号 | 任务 | 状态 |
|--------|------|------|------|
| P0 | P0-1 | DataPipe min_len硬编码30→配置化，update_bands冗余4次→1次 | ✅ |
| P0 | P0-2 | RegimeConfig.py死代码→DEPRECATED空壳 | ✅ |
| P1 | P1-1 | ConfigManager返回结构化对象，消除isinstance(dict)兼容 | ✅ |
| P1 | P1-2 | 硬编码参数配置化→ExecConfig | ✅ |
| P1 | P1-3 | 覆盖on_backtest_end，DiagLogger最终flush | ✅ |
| P1 | P1-4 | DiagLogger接入核心流水线 | ✅ |
| P2 | P2-1 | SignalPipeline 584行→pipeline/包(7文件) | ✅ |
| P2 | P2-2 | print()→logging(RiskMgr+FactorEngine) | ✅ |
| P2 | P2-3 | 清理scripts/legacy/_opt_results + .gitignore | ✅ |
| P2 | P2-4 | 反转开仓冷却期策略文档化 | ✅ |
| P3 | P3-1 | sys.path hack→正式包结构 | ⏸ 暂缓 |
| P3 | P3-2 | on_tick与process状态机冲突修复 | ✅ |
| P3 | P3-3 | 因子默认参数与YAML对齐(lookback 60→20) | ✅ |

### 6.2 关键修复详情

**P0-1: DataPipe min_len硬编码**
- 问题: min_len硬编码30，与因子lookback=20矛盾(前30根K线无法计算因子)
- 修复: min_len默认值改为20，与lookback对齐；bollinger参数从ExecConfig读取

**P0-2: RegimeConfig.py死代码**
- 问题: SubStrategy实际import的是regime_detector.py版本，RegimeConfig.py从未被使用
- 修复: 改为DEPRECATED空壳(用户拒绝删除)

**P1-1: ConfigManager结构化**
- 问题: ConfigManager返回dict，下游用isinstance(dict)做兼容，代码冗余
- 修复: ConfigManager一次性构建SignalConfig/RiskConfig/RegimeConfig/ExecConfig结构化对象，SubStrategy/RiskMgr直接接收结构化对象

**P1-2: 硬编码参数配置化**
- 问题: ArbExecHelper中LEG_TIMEOUT=30/MAX_RETRY=3硬编码，无法通过配置调整
- 修复: 新增ExecConfig dataclass，strategy_config.yaml新增execution节，ArbExecHelper.from_config()从配置读取

**P2-1: SignalPipeline拆分**
- 问题: SignalPipeline.py 584行，6个类混在一个文件，难以维护
- 修复: 拆分为pipeline/包(7文件)，每个Step一个文件，SignalPipeline.py保留为兼容性代理

**P2-4: 冷却期策略文档化**
- 问题: 反转开仓是否豁免冷却期未明确，可能导致过度交易
- 修复: 在_execute_decision中明确文档化: 反转不豁免冷却期，反转本质是"平旧仓+开新仓"应独立评估

**P3-2: on_tick与process冲突**
- 问题: on_tick中调用eh.on_tick_check()推进状态机，与process()中的推进形成两条路径，可能导致重复检测/状态冲突
- 修复: 移除on_tick中的on_tick_check调用，状态机推进统一在process()开头执行

**P3-3: 因子默认参数对齐**
- 问题: ZScoreFactor默认lookback=60，CointResidualFactor默认200，HalfLifeFactor默认120，与YAML配置lookback=20不一致
- 修复: 所有默认值改为20，与YAML配置对齐

---

## 七、回测结果参考

| 配置 | 交易数 | 胜率 | 总PnL | PF |
|------|--------|------|-------|-----|
| 原始(entry=0.80) | 0 | — | 0 | — |
| adaptive+entry=0.65 | 30 | 0% | -116,072 | 0.000 |
| adaptive+entry=0.65+trend_rev_flip | 2 | 0% | -1,175 | 0.000 |
| adaptive+entry=0.65+rev_w=0.8 | 38 | 5.3% | -80,237 | 0.006 |

> 注: rb-i在5分钟级别协整性差(ADF p>0.05)，MR策略表现不佳属于预期内。项目架构和代码质量已通过审查优化，换到协整性更好的品种对(如rb-hc)预期表现会改善。

---

## 八、扩展指南

### 新增品种对

只需在strategy_config.yaml的strategies节加一条配置：

```yaml
  - name: rb_hc_m5
    legs:
      - code: SHFE.rb.HOT
        volume: 2
        coeff: 1.0
      - code: SHFE.hc.HOT
        volume: 5
        coeff: -1.0
    spread_mode: coint
    main_period: m5
    aux_period: d1
    lookback: 20
```

无需修改任何Python代码。

### 新增因子

1. 在factors/目录创建新文件(如`custom_factors.py`)
2. 继承FactorBase，实现calculate()方法，设置DIRECTION_TYPE
3. 在factor_config.yaml中添加配置

```python
class MyFactor(FactorBase):
    DIRECTION_TYPE = "reversal"  # 或 "momentum" / "neutral"

    def calculate(self, spread, **kwargs):
        # ... 计算逻辑 ...
        return self._normalize(raw_value, clip_val=3.0)
```

### 新增风控规则

```python
strategy.register_risk_rule(
    name="my_rule",
    rule_fn=lambda ctx, config, spread, sc, **kw: RiskContext(should_close=False, reason=""),
    sub_name="rb_i_m5"  # 可选，指定子策略
)
```
