# cta_spread_arb v4 架构审计报告

## 一、架构总览

### 代码规模

| 模块 | 文件数 | 代码行数 | 职责 |
|------|--------|----------|------|
| strategies/ | 3 | 385 | 协调者+子策略容器 |
| factors/ | 6 | 931 | 因子基类+引擎+4类因子 |
| helpers/ | 12 | 2,479 | 信号/风控/执行/数据/状态 |
| schemas.py | 1 | 256 | 统一数据结构 |
| **合计** | **22** | **4,051** | |

### 依赖关系图

```
SpreadArbStrategy
  ├── ConfigManager ──→ strategy_config.yaml + factor_config.yaml
  ├── schemas ──→ SubStrategyConfig, SignalConfig, RiskConfig, RegimeConfig
  └── SubStrategy[0..N]
        ├── DataPipe ──→ SpreadCalculator, FactorEngine(refit_only)
        ├── RegimeDetector ──→ RegimeConfig(工厂)
        ├── FactorEngine ──→ FactorBase子类(auto_discover)
        ├── SignalGen ──→ SignalPipeline(4步)
        │     └── RegimeDispatchStep → RegimeWeightStep → PersistFilterStep → ExitJudgeStep
        ├── RiskMgr ──→ 5条内置规则 + 插件化
        ├── ArbExecHelper ──→ 状态机(IDLE/OPENING/OPENED/CLOSING)
        └── StateManager ──→ user_save_data/user_load_data
```

### 数据流(单次process调用)

```
context(CtaContext)
    │
    ▼
DataPipe.fetch() ──→ BarData
    │
    ├─→ RegimeDetector.detect() ──→ regime_dict
    │         │
    │         ▼
    │   FactorEngine.apply_regime_weights(regime)
    │         │
    │         ▼
    │   FactorEngine.calculate() ──→ factor_signal(dict)
    │         │
    │         ▼
    │   SignalPipeline.generate(regime_dict) ──→ SignalResult
    │
    ▼
RiskMgr.check() ──→ risk_result(str|None)
    │
    ▼
ArbExecHelper.execute(signal) ──→ stra_set_position()
    │
    ▼
StateManager.save_all()
```

## 二、模块职责与边界

### SpreadArbStrategy(协调者, 95行)

职责: 遍历SubStrategy，路由tick事件
边界: 不做任何业务逻辑，不持有交易状态
扩展: 新增品种对只需在YAML加配置，无需改代码

### SubStrategy(容器, 287行)

职责: 封装单品种对的完整流水线
边界: 组件创建+process()编排，不直接下单
关键: process()方法按固定顺序调用各组件

### DataPipe(201行)

职责: 数据获取+价差计算+趋势过滤
边界: 只输出BarData，不做交易决策
注意: coint模式先refit_coint()再calculate()，避免双重计算

### FactorEngine(304行)

职责: 因子计算+regime驱动权重+加权聚合
边界: 输出total_score+factor_scores，不做信号判断
关键: apply_regime_weights()必须在calculate()之前调用

### RegimeDetector(306行)

职责: 多维度regime检测(Hurst/ADF/ADX/MA)
边界: 输出RegimeResult，不修改任何外部状态
有状态: regime_history用于平滑，reset()在session_begin调用

### SignalPipeline(591行)

职责: 4步信号处理(分派→确认→持续→出场)
边界: 输出SignalResult，不直接触发交易
关键: v4后regime由外部注入，Pipeline不再内部检测

### RiskMgr(249行)

职责: 风控检查+状态跟踪(持仓时间/冷却期/盈亏)
边界: 只返回检查结果，不直接平仓(由SubStrategy调用ArbExecHelper)
插件化: register_rule()注册自定义规则

### ArbExecHelper(302行)

职责: 执行协调(状态机+追单+重试)
边界: 只负责下单和状态流转，不做信号判断
注意: 反转时先平后开，CLOSING完成后立即OPENING

### StateManager(86行)

职责: 状态持久化(direction/risk_state/last_signal_time)
边界: 只做存取，不做业务逻辑
API: user_save_data(key, val) / user_load_data(key, defVal, vType)

## 三、架构质量评估

### 优点

1. **SubStrategy容器化**: 新增品种零代码改动
2. **Pipeline模式**: 信号处理步骤可独立测试和替换
3. **regime驱动**: 自适应权重避免固定模式假设
4. **插件化风控**: register_rule()扩展方便
5. **结构化配置**: schemas.py统一数据结构，YAML驱动
6. **因子自动发现**: _auto_discover_factors()扫描factors包

### 问题与风险

1. **SignalPipeline过重(591行)**: 4个Step类+SignalContext+SignalPipeline全在一个文件
   - 建议: 每个Step独立文件，SignalPipeline只做组合

2. **SubStrategy._sub兼容dict**: 为ArbExecHelper/RiskMgr保留dict接口
   - 问题: 双重表示(SubStrategyConfig+dict)，维护成本高
   - 建议: 逐步迁移ArbExecHelper/RiskMgr使用结构化配置

3. **FactorEngine持有signal_cfg**: entry/exit_threshold从factor_config读取但被strategy_config覆盖
   - 问题: 两个配置文件都可能定义阈值，优先级不明确
   - 建议: 统一由strategy_config.yaml管理，factor_config只管因子定义

4. **RegimeDetector参数映射**: RegimeConfig.py做YAML字段名→内部参数名映射
   - 问题: 映射关系不透明(如regime_switch_cost→regime_min_confidence)
   - 建议: 统一参数命名，消除映射层

5. **RiskMgr接受dict**: check(context, sub, spread, spread_calc)中sub是dict
   - 问题: 与结构化SubStrategyConfig不一致
   - 建议: 改为接受SubStrategyConfig

6. **sys.path.insert(0)**: 几乎每个文件都有手动sys.path操作
   - 问题: 隐式依赖加载顺序，不利于包管理
   - 建议: 改为proper package(__init__.py导出)或setup.py安装

7. **SignalContext可变状态**: 17个字段在Steps间修改
   - 问题: Step间隐式耦合，难以独立测试
   - 建议: 每个Step返回明确结果，而非修改共享context

8. **ExitJudgeStep有状态**: _bars_since_peak和_peak_signal是Step内部状态
   - 问题: 状态重置依赖reset_trailing()，容易遗漏
   - 建议: 状态提升到SubStrategy或专门的状态对象

9. **配置默认值不一致**: schemas.py默认值与strategy_config.yaml不同
   - 例: SignalConfig.entry_threshold默认0.525, yaml是0.80
   - 建议: schemas默认值应与yaml一致，或yaml必填

10. **DiagLogger未集成**: 128行代码但未被使用
    - 建议: 集成或删除

## 四、改进方案

### 方案1: Pipeline拆分(优先级: 高)

将SignalPipeline.py(591行)拆分为独立文件:

```
helpers/pipeline/
├── __init__.py              # SignalPipeline组合器
├── context.py               # SignalContext(17字段)
├── regime_dispatch.py       # RegimeDispatchStep(183行)
├── regime_weight.py         # RegimeWeightStep(80行)
├── persist_filter.py        # PersistFilterStep(47行)
├── exit_judge.py            # ExitJudgeStep(163行)
```

好处: 每个Step可独立测试，修改一个Step不影响其他。
风险: 低，纯文件拆分，不改逻辑。

### 方案2: 结构化配置统一(优先级: 高)

消除SubStrategy._sub兼容dict:

1. ArbExecHelper改用LegConfig列表(而非dict)
2. RiskMgr.check()改用SubStrategyConfig(而非dict)
3. 删除SubStrategy._sub和to_leg_dicts()

好处: 消除双重表示，类型安全。
风险: 中，需修改ArbExecHelper/RiskMgr接口。

### 方案3: 配置优先级统一(优先级: 中)

1. factor_config.yaml只定义因子(类/参数/权重)，不定义信号参数
2. 所有信号参数(entry/exit_threshold等)只在strategy_config.yaml
3. FactorEngine.__init__不再从factor_config读取signal_aggregation
4. schemas.py默认值与strategy_config.yaml一致

好处: 配置来源单一，消除歧义。
风险: 低，FactorEngine接口小改。

### 方案4: RegimeDetector参数统一命名(优先级: 低)

1. strategy_config.yaml直接使用RegimeDetector内部参数名
2. 删除RegimeConfig.py映射层
3. RegimeConfig.from_yaml()直接透传

好处: 消除映射层，配置更直观。
风险: 低，YAML字段名变更需同步。

### 方案5: SignalContext不可变化(优先级: 中)

1. 每个Step返回StepResult而非修改SignalContext
2. SignalPipeline组合StepResult为最终SignalResult
3. Step间无隐式耦合

好处: Step可独立测试，无隐式状态修改。
风险: 中，需重写Pipeline接口。

### 方案6: sys.path清理(优先级: 低)

1. 每个目录的__init__.py导出关键类
2. 删除所有sys.path.insert(0)
3. 改为from helpers.signal_gen import SignalGen等正规导入

好处: 包管理规范，IDE支持更好。
风险: 低，纯重构。

## 五、重构路线图

### Phase 1: 立即可做(低风险)

1. Pipeline拆分(方案1) -- 纯文件拆分
2. 配置默认值同步(方案3部分) -- schemas.py默认值与yaml一致
3. 删除DiagLogger(未使用) -- 减少死代码
4. 删除scripts/下旧优化脚本(optimize_v2~v5) -- 只保留v6

### Phase 2: 中期改进(中风险)

1. 结构化配置统一(方案2) -- ArbExecHelper/RiskMgr改用LegConfig
2. 配置优先级统一(方案3) -- FactorEngine不再读signal_aggregation
3. SignalContext不可变化(方案5) -- Pipeline接口重写

### Phase 3: 长期优化(低风险但大工作量)

1. sys.path清理(方案6) -- 正规包导入
2. RegimeDetector参数统一(方案4) -- YAML字段名变更
3. ExitJudgeStep状态提升 -- 从Step内部提升到SubStrategy

## 六、业务逻辑优化方向

### 1. 出场逻辑改善(改善盈亏比0.62->目标0.8+)

当前问题: 平均亏损563 > 平均盈利346，亏损交易持仓时间过长。

改进方案:
- 动态止损: 根据regime和信号强度调整止损阈值
  trend regime: 追踪止损更激进(trend_trailing_ratio从0.5->0.6)
  range regime: 更快出场(range_exit_threshold从0.4->0.5)
- 盈利保护提前触发: profit_protect_peak从0.65->0.55
- 亏损限制: 信号衰减到exit_threshold*0.3时强制出场(而非0.5)

### 2. regime-adaptive入场阈值(改善交易笔数和做空)

当前问题: 固定entry_threshold=0.80对所有regime一刀切。

改进方案:
- trend regime: entry_threshold=0.65(趋势确认后降低门槛)
- mr regime: entry_threshold=0.80(协整回归需高置信度)
- range regime: entry_threshold=1.0(震荡几乎不入场)
- 做空: trend regime下short_multiplier=1.0(趋势确认后做空门槛降低)

### 3. 品种对扩展(改善协整根基)

当前瓶颈: rb-i 5min不协整(ADF p>0.05)。

改进方案:
- 新增rb-hc(螺纹-热卷): 同品种链，协整性好
- 新增m-y(豆粕-玉米): 农产品链，协整性好
- 降频测试: 15min/30min级别协整性可能改善
- YAML加配置即可，无需改代码(SubStrategy容器化优势)

### 4. 因子权重优化

当前: reversal=0.55, momentum=0.45(固定比例)

改进方案:
- regime-driven动态权重已在FactorEngine实现
- 可进一步: 根据品种对特性调整基础权重
  rb-hc: momentum权重提高(同品种链趋势更明显)
  m-y: reversal权重提高(农产品回归特性更强)

## 七、测试建议

### 单元测试(优先级: 高)

1. RegimeDetector.detect() -- 各种spread序列的regime判断
2. FactorEngine.calculate() -- 因子计算+加权聚合
3. SignalPipeline各Step -- 独立测试每个Step
4. RiskMgr.check() -- 各风控规则触发条件
5. ArbExecHelper状态机 -- 各状态转换

### 集成测试(优先级: 中)

1. SubStrategy.process() -- 完整流水线
2. SpreadArbStrategy.on_calculate() -- 多品种对并行
3. StateManager持久化 -- save/load一致性

### 回测验证(优先级: 高)

1. 不同品种对(rb-i/rb-hc/m-y)
2. 不同频率(m5/m15/m30)
3. 不同参数组合(entry_threshold/regime参数)
4. 不同回测区间(至少1年)

## 八、总结

v4架构相比v3有显著改善:
- regime前置解决权重时序错位(P0)
- 三重衰减修复(P3)
- SHORT方向优化(P4)
- Pipeline解耦(P5)
- 收益从-11.96%改善到-3.58%

但仍存在架构层面可优化的空间:
- SignalPipeline过重(591行)需拆分
- 配置双重表示(dict+dataclass)需统一
- SignalContext可变状态需不可变化
- sys.path操作需正规化

业务逻辑层面:
- rb-i 5min不协整是根本瓶颈，需换品种对
- 盈亏比0.62偏低，需改善出场逻辑
- 固定入场阈值需改为regime-adaptive
