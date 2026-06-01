# CTA跨品种套利策略 — 业务逻辑文档 v6

> 最后更新: 2026-05-08 | 回测改善: -132,050(v0) → -3,810(v5) 改善97.1%

## 1. 架构总览

```
DataPipe → FactorEngine → SignalPipeline → RiskMgr → ArbExecHelper
   ↓           ↓              ↓               ↓           ↓
 价差计算    因子评分      信号过滤链       风控规则     执行路由
```

### 核心原则
- **Pipeline模式**: 数据单向流动，每层职责单一
- **衰减非禁止**: 过滤器衰减信号而非完全阻断(decay-not-block)
- **配置分离**: signal/risk_control/adaptive三节独立配置

## 2. 模块职责

### 2.1 DataPipe — 数据管道
- 获取K线数据，计算价差序列
- 日线趋势过滤(spread_trend_filter)
- **v6修复**: 日线价差统一用 `SpreadCalculator.calc_spread(mode='coint')`

### 2.2 SpreadCalculator — 价差计算器(v3)
- **唯一入口**: `calc_spread(prices_dict, mode, beta, c)`
- mode='price': 名义价值归一化价差
- mode='coint': 协整残差价差(beta/c为None时回退price模式)
- **删除OLS回退**: 不再自行计算beta/c，由外部传入

### 2.3 FactorEngine — 因子引擎
- 计算多因子加权信号
- **v6修复**: 异常因子标记None跳过加权，active_weight用abs(eff_w)
- 返回 `{total_score, factor_scores, direction, failed_factors}`

### 2.4 SignalPipeline — 信号管道(v2, 替代MultiPeriodSignal)
5个独立Step顺序执行:

| Step | 职责 | 关键参数 |
|------|------|----------|
| ModeDispatch | 信号模式分派(adaptive/hybrid/MR/trend) | trend_aligned_boost, trend_counter_decay |
| CointGate | 协整门控衰减 | coint_decay |
| MomentumConfirm | 动量确认衰减 | momentum_decay |
| PersistFilter | 信号持续性过滤 | signal_persist_bars, persist_logic |
| ExitJudge | 出场/反转/追踪止损判断 | entry_threshold, trailing_decay_ratio |

**扩展方式**: 新增过滤只需添加Step类，不改核心逻辑

### 2.5 SignalGen — 信号生成器
- 封装SignalPipeline，输出标准化SignalResult
- `SignalGen.from_config(signal_mode, entry_threshold, exit_threshold, adaptive_cfg, signal_cfg)`

### 2.6 RiskMgr — 风控管理
5条内置规则(按优先级):

| 规则 | 触发条件 | 动作 |
|------|----------|------|
| spread_deviation | 价差偏离>3σ | 平仓 |
| max_loss | 单笔亏损>阈值 | 平仓 |
| profit_take | 盈利回撤超比例 | 平仓 |
| time_stop | 持仓超时 | 平仓 |
| coint_quality(v6) | 协整p>=0.05禁开仓, p>=0.10强平 | 限制/平仓 |

### 2.7 ArbExecHelper — 执行路由
- 双腿下单/平仓/反转
- **v6修复**: infer_direction增加all_nonzero完整性检查

### 2.8 ConfigManager — 配置管理
- strategy_config.yaml拆分三节: signal / risk_control / adaptive
- factor_config.yaml: 因子权重+阈值

### 2.9 StateManager — 状态持久化
- 统一API: `load_all(context, risk_mgr)` / `save_all(context, risk_mgr)`

## 3. 信号生成流程

```
FactorEngine.calculate()
    ↓ total_score, factor_scores, direction
SignalPipeline.generate()
    ↓ ModeDispatch → CointGate → MomentumConfirm → PersistFilter → ExitJudge
    ↓ SignalContext(可变状态在Steps间传递)
SignalResult(entry_signal, direction, should_trade, should_exit, should_reverse)
    ↓
RiskMgr.check() → 风控规则链
    ↓
ArbExecHelper.execute() → 双腿下单
```

## 4. 配置结构

### strategy_config.yaml
```yaml
signal:
  signal_mode: adaptive
  entry_threshold: 0.525
  exit_threshold: 0.1
  coint_decay: 0.8
  momentum_decay: 0.8
  trend_aligned_boost: 1.2
  trend_counter_decay: 0.5
  signal_persist_bars: 3
  persist_logic: AND
  trailing_decay_ratio: 0.3
  trailing_exit_bars: 6

risk_control:
  max_loss_per_trade: 5000
  profit_take_threshold: 3000
  profit_drawdown_ratio: 0.5
  max_hold_bars: 20
  spread_deviation_sigma: 3.0

adaptive:
  hl_mr_threshold: 15.0
  hl_trend_threshold: 30.0
  trend_z_threshold: 1.0
```

## 5. 工具集(tools/)

| 工具 | 用途 |
|------|------|
| PairScreener | 品种组合筛选: 协整质量评估+排序+批量扫描 |
| BacktestAnalyzer | 回测分析: 参数敏感性/滚动回测/walk-forward/热力图 |
| DiagLogger | 诊断日志: 交易/风控/因子三类结构化JSON日志 |

## 6. 已修复BUG清单

| BUG | 问题 | 修复 |
|-----|------|------|
| BUG-1 | 持久化API不一致 | StateManager.load_all统一接口 |
| BUG-2 | DataPipe日线价差计算不一致 | 统一用SpreadCalculator.calc_spread |
| BUG-3 | SpreadCalculator OLS回退不可靠 | 删除OLS回退，beta/c为None回退price模式 |
| BUG-4 | infer_direction部分腿成交误判 | 增加all_nonzero完整性检查 |
| BUG-5 | RiskMgr profit_take冗余判断 | 删除无意义isinstance检查 |
| BUG-6 | FactorEngine异常因子处理不当 | 异常因子跳过加权+failed_factors列表 |
| BUG-7 | CointResidualFactor OLS重复 | 由SpreadCalculator统一处理 |

## 7. 回测绩效轨迹

| 版本 | realized | 收益率 | 笔数 | 胜率 | PF | 关键变更 |
|------|----------|--------|------|------|-----|----------|
| v0 | -132,050 | -26.4% | - | - | - | 基线 |
| v2 | -8,810 | -1.76% | 70 | 33% | 0.45 | 0.45阈值 |
| v4 | -8,520 | -1.70% | 70 | 33% | 0.46 | 逆势MR衰减+regime记忆 |
| v5 | -3,810 | -0.76% | 70 | 33% | 0.64 | 阈值0.525 |
| v6 | 待验证 | - | - | - | - | SignalPipeline+协整门控 |

## 8. 已知限制

1. **rb-i不协整**: ADF p>0.05, CointResidual>0仅18%，信号噪声比低
2. **入场阈值非单调**: 0.525→0.54突变(PF 0.61→0.44)，过拟合风险高
3. **spread由i主导**: w_i=-0.87, rb贡献极小
4. **推荐**: 换品种(rb-hc/m-y)或降频(15min)可能显著改善
