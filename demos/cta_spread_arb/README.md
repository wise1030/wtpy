# cta_spread_arb — 期货跨品种价差套利策略

基于 wtpy (WonderTrader Python封装) 的 CTA 跨品种/跨周期价差套利策略系统。
v4架构: Pipeline模式 + Regime驱动的自适应信号生成 + SubStrategy容器化。

## 项目结构

```
cta_spread_arb/
├── config/                          # 配置文件
│   ├── strategy_config.yaml         # 策略配置(品种腿/信号/风控/regime)
│   └── factor_config.yaml           # 因子配置(因子列表/权重/参数)
├── strategies/                      # 策略主模块
│   ├── SpreadArbStrategy.py         # 协调者(遍历SubStrategy)
│   └── SubStrategy.py               # 子策略容器(自包含流水线)
├── factors/                         # 因子模块
│   ├── FactorEngine.py              # 因子引擎(regime驱动权重+加权聚合)
│   ├── FactorBase.py                # 因子基类(normalize工具)
│   ├── stat_factors.py              # 统计因子(ZScore/CointResidual/HalfLife)
│   ├── momentum_factors.py          # 动量因子(SpreadMomentum)
│   ├── trend_factors.py             # 趋势因子(MACD/ADX/ChannelBreak/TrendStrength)
│   └── vol_price_factors.py         # 量价因子(已禁用,保留配置)
├── helpers/                         # 辅助模块
│   ├── SignalPipeline.py            # 信号管道(4步Pipeline)
│   ├── SignalGen.py                 # 信号生成器(封装Pipeline+RegimeDetector)
│   ├── regime_detector.py           # 市场状态检测器(Hurst/ADF/ADX/MA投票)
│   ├── RegimeConfig.py              # RegimeDetector工厂
│   ├── DataPipe.py                  # 数据获取+价差计算+趋势过滤
│   ├── SpreadCalculator.py          # 价差计算器(名义价值归一化)
│   ├── RiskMgr.py                   # 风控管理(5条规则+插件化)
│   ├── ArbExecHelper.py             # 套利执行协调器(状态机)
│   ├── StateManager.py              # 状态持久化(user_save/load_data)
│   ├── ConfigManager.py             # 配置加载+校验
│   └── DiagLogger.py                # 诊断日志(未集成)
├── schemas.py                       # 统一数据结构(BarData/FactorOutput/SignalResult等)
├── reporter/                        # 实盘数据报告模块
├── scripts/                         # 诊断/优化脚本
├── docs/                            # 文档
│   ├── PROJECT_GUIDE.md             #   项目说明 + 数据流 + 各模块职责
│   ├── CONFIG_GUIDE.md              #   策略配置 (yaml schema + profile + 实验)
│   ├── WT_INFRA_GUIDE.md            #   WT 基础设施 yaml 字段标注 + 凭据安全 (R3)
│   ├── STRATEGY_DESIGN_BASELINE.md  #   策略设计基线
│   ├── archive/                     #   历史版本文档
│   └── plans/                       #   阶段计划
├── runBT.py                         # 回测启动脚本
└── runLive.py                       # 实盘启动脚本 (preflight 启动期自检)
```

## 策略架构

### 整体架构: 协调者 + SubStrategy容器

```
SpreadArbStrategy (协调者, 95行)
  ├── on_init: 遍历SubStrategy.on_init()
  ├── on_calculate: 遍历SubStrategy.process()
  ├── on_tick: _code_to_subs路由到对应SubStrategy
  └── register_risk_rule: 插件化风控规则注册

SubStrategy[rb_i_m5] (自包含容器, 287行)
  │   组件: DataPipe + SpreadCalculator + FactorEngine
  │         + SignalGen(含RegimeDetector+SignalPipeline)
  │         + RiskMgr + ArbExecHelper + StateManager
  │
  └── process() 流水线:
      DataPipe.fetch() → RegimeDetect → FactorEngine.apply_regime_weights()
      → FactorEngine.calculate() → SignalGen.generate_with_regime()
      → RiskMgr.check() → ArbExecHelper.execute() → StateManager.save_all()

SubStrategy[rb_hc_m5] (新增品种只需加YAML配置)
  └── ...
```

### 数据流

```
K线数据 (m5 + d1)
    │
    ▼
DataPipe.fetch() ──→ BarData(prices, volumes, spread, trend_filter)
    │
    ▼
RegimeDetector.detect() ──→ regime_dict(regime, confidence, direction)
    │
    ▼
FactorEngine.apply_regime_weights(regime) ──→ 动态调整因子权重
    │
    ▼
FactorEngine.calculate() ──→ {total_score, factor_scores, direction, coint_p}
    │
    ▼
SignalPipeline.generate(regime_dict) ──→ SignalResult(entry_signal, should_trade/exit/reverse)
    │   4步Pipeline:
    │   1. RegimeDispatchStep  — regime信号分派(趋势boost/MR透传/range衰减)
    │   2. RegimeWeightStep    — 协整门控+动量确认
    │   3. PersistFilterStep   — 信号持续性过滤(OR_2of3逻辑)
    │   4. ExitJudgeStep       — regime驱动出场策略
    │
    ▼
RiskMgr.check() ──→ 风控触发/未触发
    │
    ▼
ArbExecHelper.execute() ──→ 状态机执行(IDLE→OPENING→OPENED→CLOSING→IDLE)
    │
    ▼
wtpy CtaContext.stra_set_position() ──→ 下单
```

### 状态机

```
IDLE ──→ OPENING ──→ OPENED ──→ CLOSING ──→ IDLE
  │         │           │          │
  │    开仓下单     持仓等待     平仓下单
  │         │           │          │
  │    on_tick追单   信号衰减/反转  on_tick追单
  │         │           │          │
  │    超时重试3次   should_exit   超时重试3次
  │         │           │          │
  │    失败→紧急全平  should_reverse→先平后开
```

## 功能模块详解

### 1. SpreadArbStrategy — 协调者(95行)

继承 `BaseCtaStrategy`，职责极简:
- **__init__**: 加载配置 → 创建SubStrategy列表 → 构建_code_to_subs查找表
- **on_init**: 遍历sub.on_init()
- **on_calculate**: 遍历sub.process()
- **on_tick**: 路由到对应SubStrategy的ArbExecHelper
- **register_risk_rule**: 插件化风控规则注册

新增品种只需在strategy_config.yaml加一条配置，无需改Python代码。

### 2. SubStrategy — 子策略容器(287行)

自包含对象，封装单个品种组合的所有组件和流水线逻辑。

**process() 核心流程(v4)**:
1. 仓位同步: 实际仓位归零但状态机未重置 → reset()
2. 状态机推进: eh.on_tick_check()
3. DataPipe.fetch() → bar_data
4. **RegimeDetect(前置)**: sg.detect_regime() → regime_dict
5. **apply_regime_weights**: fe.apply_regime_weights(regime)
6. FactorEngine.calculate() → factor_signal
7. SignalGen.generate_with_regime() → signal
8. 更新指标(regime_val/dir/conf)
9. RiskMgr + 交易决策
10. StateManager.save_all()

**v4关键改进**: regime检测前置到FactorEngine.calculate()之前，解决权重时序错位问题。

### 3. RegimeDetector — 市场状态检测器(306行)

多维度综合判断当前regime(trend/mr/range):

| 维度 | 方法 | 权重 | 判断逻辑 |
|------|------|------|----------|
| Hurst指数 | R/S法 | 0.30 | H>0.55→趋势, H<0.45→MR, 中间→range |
| ADF检验 | adfuller | 0.25 | p<0.05→MR, p>0.15→趋势, 中间→range |
| ADX趋势强度 | 简化ADX | 0.25 | ADX>25→趋势, ADX<20→range |
| MA斜率 | 快慢MA差/std | 0.20 | 强斜率→趋势, 弱斜率→range |

**投票机制**: 各维度独立判断，加权投票决定最终regime。
**regime记忆**: 最近5 bars投票平滑，置信度不够时保持上次regime(decay-not-block原则)。

输出: RegimeResult(regime, confidence, direction, hurst, adf_p, adx)

### 4. FactorEngine — 因子引擎(304行)

管理8个启用因子，regime驱动的自适应权重:

**当前启用8因子**:

| 因子 | 类 | direction_type | 权重 | 含义 |
|------|-----|---------------|------|------|
| zscore | ZScoreFactor | reversal | 0.25 | 滚动Z标准化(60根) |
| coint_residual | CointResidualFactor | reversal | 0.20 | 协整残差Z(200根,refit间隔5天) |
| half_life | HalfLifeFactor | reversal | 0.10 | 半衰期*偏离方向(120根) |
| spread_momentum | SpreadMomentumFactor | momentum | 0.15 | 价差动量(快10/慢30) |
| macd_spread | MACDSpreadFactor | momentum | 0.10 | MACD价差(12/26/9) |
| adx_spread | ADXSpreadFactor | momentum | 0.05 | ADX趋势强度(14) |
| channel_break | ChannelBreakFactor | momentum | 0.05 | 通道突破(20) |
| trend_strength | TrendStrengthFactor | momentum | 0.10 | 趋势强度(10/30) |

**REGIME_WEIGHT_MAP — regime驱动的因子权重调整**:

| regime | momentum | reversal | neutral |
|--------|----------|----------|---------|
| trend  | 1.5x,不翻转 | 0.5x,不翻转 | 1.0x |
| mr     | 0.5x,翻转 | 1.5x,翻转 | 1.0x |
| range  | 0.3x,不翻转 | 0.3x,不翻转 | 0.5x |

**加权聚合**: total_score = Sum(score_i * effective_weight_i * flip_sign_i) / Sum|effective_weight_i|

**协整refit**: CointResidualFactor每5天(失败后指数退避5→2→1天)重做OLS+ADF。

### 5. SignalPipeline — 信号管道(591行)

4步Pipeline，SignalContext在Steps间传递:

**Step 1: RegimeDispatchStep** — regime信号分派
- v4: 不再内部检测regime，使用外部注入的regime_dict
- trend regime: 顺势信号*1.3 boost, 逆势信号*0.3 decay
- mr regime: 信号直接透传(FactorEngine已处理翻转)
- range regime: 信号*0.8衰减(提高入场门槛)

**Step 2: RegimeWeightStep** — 协整门控+动量确认
- trend regime: 检查趋势因子是否确认信号方向
- mr regime: 协整门控(p>=0.10→衰减0.49, p>0.05→衰减0.7) + 动量确认
- range regime: v4不再额外衰减(避免三重衰减)

**Step 3: PersistFilterStep** — 信号持续性过滤
- 最近3 bars信号方向需一致(OR_2of3逻辑: 3个中2个同向即可)
- 强信号(>1.5x阈值)跳过持续性检查

**Step 4: ExitJudgeStep** — regime驱动出场策略
- trend regime: 追踪止损(信号从峰值回撤50%+持续3 bars) + 盈利保护 + 信号衰竭兜底
- mr regime: 信号衰竭(<exit_threshold) + 盈利保护 + 追踪止损
- range regime: 快速出场(信号<range_exit_threshold=0.4)
- 做空门槛: short_entry_multiplier=1.5(做空需1.5倍信号强度)
- 趋势regime中禁止逆势开仓; 非MR regime做空需趋势方向确认

### 6. RiskMgr — 风控管理(249行)

5条内置规则(按优先级):

| 规则 | 条件 | 动作 |
|------|------|------|
| spread_deviation | \|spread-mean\|/std > 4.0 | 全平+冷却期 |
| max_loss | Σ(pnl) < -8000 | 全平+冷却期 |
| profit_take | peak>3000 且 current<peak*0.5 | 盈利保护平仓 |
| time_stop | hold_bars > 120 | 时间止损平仓 |
| coint_quality | coint_p>=0.10强制平仓, >=0.05禁止新开仓 | 协整质量门控 |

冷却期: 风控平仓后12 bars内禁止开新仓。
插件化: register_rule(name, rule_fn)注册自定义规则。

### 7. ArbExecHelper — 执行协调(302行)

完整状态机实现:
- 目标仓位: ratio>0的腿 target=direction*lots, ratio<0的腿 target=-direction*lots
- 追单: 实盘30秒超时/回测30个tick
- 重试: 最多3次，超过后紧急全平
- 反转: CLOSING完成后立即以新方向OPENING
- 状态恢复: restore_state()从持久化数据恢复

### 8. StateManager — 状态持久化(86行)

v4修复: 使用CtaContext正确API:
- user_save_data(key, val) — 保存
- user_load_data(key, defVal, vType) — 加载(需指定类型)
- 复杂类型(dict/list)用JSON序列化

## 参数配置说明

### strategy_config.yaml (当前最优参数)

```yaml
regime:
  hurst_window: 100
  trend_hurst_threshold: 0.55
  mr_hurst_threshold: 0.45
  trend_adx_threshold: 25.0
  regime_memory_bars: 5
  regime_switch_cost: 0.3
  # REGIME_WEIGHT_MAP
  trend_momentum_weight: 1.5    trend_reversal_weight: 0.5
  mr_momentum_weight: 0.5       mr_reversal_weight: 1.5
  range_momentum_weight: 0.3    range_reversal_weight: 0.3

signal:
  signal_mode: adaptive
  entry_threshold: 0.80         # 入场阈值(0.80最优, 网格搜索确认)
  exit_threshold: 0.12
  trend_aligned_boost: 1.3      # 趋势顺势增强
  trend_counter_decay: 0.3      # 趋势逆势衰减
  range_signal_decay: 0.8       # range信号衰减
  coint_decay: 0.7              # 协整不显著衰减
  momentum_decay: 0.7           # 动量不确认衰减
  signal_persist_bars: 3        # 持续性过滤bar数
  persist_logic: OR_2of3        # 3个中2个同向即可
  short_entry_multiplier: 1.5   # 做空门槛倍率
  trailing_exit_bars: 6         # 追踪止损持续bar数
  trailing_decay_ratio: 0.35    # 追踪止损衰减比
  range_exit_threshold: 0.4     # range快速出场阈值

risk_control:
  cooldown_bars: 12
  max_hold_bars: 120
  max_loss_per_pair: 8000
  max_spread_deviation: 4.0
  profit_take_threshold: 3000
  profit_drawdown_ratio: 0.5

strategies:
- name: rb_i_m5
  type: cross_commodity
  signal_mode: adaptive
  spread_mode: coint
  legs:
    - code: SHFE.rb.HOT   lots: 3   multiplier: 10   ratio: 1
    - code: DCE.i.HOT     lots: 2   multiplier: 100  ratio: -1
  periods:
    main_period: m5    main_bar_cnt: 300
    aux_periods: [{period: d1, bar_cnt: 60}]
```

### factor_config.yaml

8个启用因子(权重合计1.0):
- reversal: zscore(0.25) + coint_residual(0.20) + half_life(0.10) = 0.55
- momentum: spread_momentum(0.15) + macd_spread(0.10) + adx_spread(0.05) + channel_break(0.05) + trend_strength(0.10) = 0.45

5个禁用因子(保留配置): vwap_deviation, volume_price_divergence, obv_spread, volume_ratio, rsi_spread

## 运行说明

### 回测

```bash
cd /mnt/d/gf_pc/WonderTrader/wtpy/demos/cta_spread_arb
python runBT.py
```

回测配置在configbt.yaml中: 数据源../storage/, 回测区间3个月, 滑点2跳, 初始资金500000。

### 实盘

```bash
python runLive.py
```

### 参数优化

> 注: 旧版 `scripts/optimize_v6.py` / `scripts/grid_search.py` 已于 2026-06-04
> P2-clean 退役。新版参数优化路径待 P3 阶段重建（基于 ConfigResolver + experiments）。

## 回测结果

### v4最终回测指标 (entry_threshold=0.80)

| 指标 | 值 |
|------|-----|
| 收益率 | -3.58% |
| 最大回撤 | 3.69% |
| 交易笔数 | 28 |
| LONG/SHORT | 28/0 |
| 胜率 | 39.3% |
| 盈亏比 | 0.62 |
| 平均盈利 | 346 |
| 平均亏损 | 563 |
| 手续费 | ~980 |

### 改善轨迹

| 版本 | 收益率 | 笔数 | 关键改动 |
|------|--------|------|----------|
| v0 | -132,050 | - | 原始版本 |
| v8f | -4.17% | 58 | 旧基准 |
| v3 | -11.96% | 146 | 架构重构(5个BUG) |
| v4 | -3.58% | 28 | P0-P5修复+参数调优 |

### entry_threshold网格搜索

| 阈值 | 收益率 | 笔数 |
|------|--------|------|
| 0.65 | -11.91% | 92 |
| 0.75 | -5.40% | 44 |
| 0.78 | -4.43% | 34 |
| **0.80** | **-3.58%** | **28** |

## 已知瓶颈与改进方向

### 根本瓶颈

1. **rb-i 5min不协整**: ADF p>0.05，MR逻辑根基薄弱。需换品种对(rb-hc/m-y)或降频(15min)
2. **i主导价差**: norm_coeff[i]=-0.87，spread方向87%由i决定
3. **盈亏比0.62偏低**: 平均亏损563 > 平均盈利346，需改善出场逻辑
4. **SHORT方向几乎无交易**: entry=0.80+multiplier=1.5导致做空门槛过高

### 改进方向

1. **换品种对**: rb-hc(螺纹-热卷)或m-y(豆粕-玉米)协整性更好
2. **降频**: 15min级别协整性可能改善
3. **出场优化**: 追踪止损/盈利保护改善盈亏比
4. **regime-adaptive阈值**: 不同regime使用不同entry_threshold
5. **回测区间扩展**: 至少1年数据覆盖不同市场状态

## 技术要点备忘

- `stra_get_time()` 返回HHMM整数(如930)，不是秒数
- CTA策略无cancel API，只有HftContext有`stra_cancel`
- `user_load_data` 默认vType=float，整数字段需指定`int`
- WtBtEngine是@singleton，同一进程只能跑一次回测
- `write_indicator` time格式: date*10000 + time
- `_normalize(raw, clip_val=3.0)`: clip到[-3,3]后除以3 → [-1,1]
- 配置文件参数会覆盖代码默认值，修改yaml后需确认同步
