# 策略设计与开发基准报告

生成日期: 2026-06-02
适用范围: cta_spread_arb 以及后续基于 wtpy/WonderTrader 的套利、CTA、组合策略设计与开发。

## 0. 强制执行原则

后续所有策略的设计、开发、重构、优化和实盘化，都必须先以本报告为基础。

在动代码前，先检查当前方案是否符合以下边界：

1. 策略层只负责 alpha、信号、目标仓位和本策略/pair 级保护。
2. 执行、路由、限速、追价、自成交防护，优先复用 WT executer/trader/riskmon 层。
3. 组合级资金分配、组合级回撤、跨策略风险预算，不放进单策略内部。
4. 多品种/多月份优先用多个 WT 策略实例装配，而不是恢复单策略内部多 sub/PositionRouter。
5. 架构调整必须先输出“诊断 + 候选方案 + 权衡 + 待拍板问题”，经确认后再实施。

## 1. cta_spread_arb 的正确定位

cta_spread_arb 是单品种对/单价差组合的 CTA alpha 策略模块。

它应该负责：
- 价差计算
- 因子与 regime 检测
- 信号生成
- pair-level 风控
- 双腿目标仓位计算
- 双腿开平仓状态协调
- tag 生成与 WT 31-char 安全约束

它不应该负责：
- 全账户组合风控
- 多策略资金分配
- 全局订单路由
- 交易通道限速
- 自成交防护
- 多策略订单队列

## 2. wtpy 分层对齐

推荐分层：

1. Strategy / CtaContext
   - 策略逻辑、信号、目标仓位。
   - cta_spread_arb 的 SpreadArbStrategy/ArbCore 属于这一层。

2. Engine / WtEngine / WtBtEngine
   - 多策略实例管理、回测、数据、监控。
   - 多合约月份、多品种对应该在这一层装配多个策略实例。

3. Executer
   - 目标仓位到委托的执行策略。
   - 下单间隔、追价、过期、价格模式、拆单等优先在此处理。

4. Trader / Adapter
   - 交易通道、报撤流控、柜台适配。

5. RiskMon / filters
   - 组合级风控、全局暂停、禁策略、禁合约、人工干预。

## 3. 核心架构边界

### 3.1 不在策略内部重造 OrderRouter

不要在 SpreadArbStrategy 或 ArbCore 内部实现全局 OrderRouter。

ArbExecHelper 可以保留，但语义必须限定为“双腿状态协调器”，不是全局订单路由器。

ArbExecHelper 只负责：
- signal direction 到 leg target positions
- idle/opening/opened/closing 状态机
- 双腿目标是否达成
- 一腿异常时的保护性处理
- WT tag 安全

如果实盘需要更复杂执行：
1. 先配置 WT executer；
2. WT executer 不足时，再考虑自定义 WT executer；
3. 不把通用执行路由塞进策略业务模块。

### 3.2 不在单策略内部实现 PortfolioRiskMgr

RiskMgr 应明确为 pair-level risk manager。

RiskMgr 可以做：
- max_loss_per_pair
- spread_deviation
- coint_quality
- quick_stop
- time_stop
- pair-level trailing stop
- cooldown

组合级能力应放在：
- env.riskmon
- filters.yaml
- trader riskmon
- runLive 装配层
- 外部 portfolio config / portfolio coordinator

不要在单个 SpreadArbStrategy 里做组合级权益回撤、跨策略 risk_scale、资金预算分配。

### 3.3 多品种/多月份 = 多 WT 策略实例

v10 的方向是正确的：

单 SpreadArbStrategy 实例 = 单 ArbCore = 单价差组合。

多品种对/多合约月份应使用多个 WT 策略实例：

```python
for exp in experiments:
    stra = SpreadArbStrategy.from_experiment(
        name=f"spread_arb_{exp}",
        experiment=exp,
        config_root=config_root,
    )
    engine.add_cta_strategy(stra)
```

回测侧可用 run_batch.py 逐个 experiment 回测，再合并 funds.csv。不要为了组合回测便利把多 pair 塞回单策略内部。

## 4. 后续开发优先级

推荐优先级：

1. P0: 固化架构边界和文档，防止策略层继续膨胀。
2. P1: runLive.py 支持 --experiments，多策略实例装配。
3. P1: live config 对齐 wtpy 原生能力：riskmon / executers / filters / trader riskmon。
4. P1: 核心单测：FactorEngine / RiskMgr / SignalGen / ArbExecHelper。
5. P2: 命名和文档清理：RiskMgr = pair-level，ArbExecHelper ≠ OrderRouter。
6. P2: 禁用因子逐一回测验证，做因子 ablation。
7. P2: HOT 换月、收盘强平、实盘恢复。
8. P3: FactorBase / RegimeDetector / DataPipe 性能优化。
9. P3: Paper trading 和监控告警。

## 5. 设计检查清单

每次新增策略、改策略架构、实盘化前，先回答：

1. 这项能力属于 Strategy、Engine、Executer、Trader、RiskMon 哪一层？
2. 是否把执行/组合风控塞进了策略层？
3. 是否可以通过 WT 原生 executer/riskmon/filter/trader 配置解决？
4. 多品种/多月份是否使用多策略实例，而不是单策略内部多 sub？
5. 是否需要先建立回测基线和 SHA256 manifest？
6. 是否影响 WT tag 31-char 约束？
7. 是否有核心单测或回归验证？
8. 是否已经先向用户输出诊断、方案、权衡、待拍板问题？

## 6. 权威参考

长期权威记录：
- Hermes skill: quant/cta-spread-arb-architecture
- Skill reference: references/wtpy-framework-alignment-2026-06-02.md
- Skill reference: references/project-gap-analysis-2026-06-02.md

注意：project-gap-analysis-2026-06-02.md 中关于“在策略内部集成 OrderRouter / PortfolioRiskMgr”的早期建议，已被 wtpy-framework-alignment-2026-06-02.md 修正。以后以后者为准。
