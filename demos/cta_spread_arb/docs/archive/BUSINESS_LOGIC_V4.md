# CTA跨品种套利策略 v4 — 完整业务逻辑与流程说明

## 一、整体架构：Pipeline模式

on_calculate 触发后，按Pipeline顺序执行6个模块：

1. DataPipe.fetch() — 数据获取+价差计算+趋势过滤
   输入: context, cur_date
   输出: BarData(prices, volumes, aux_bars, spread, spread_trend_filter, beta, min_len)

2. FactorEngine.calculate() — 因子计算+加权聚合
   输入: spread, prices, volumes, aux_bars, norm_coeffs
   输出: {total_score, factor_scores, direction, coint_p}

3. SignalGen.generate() — 多周期信号+自适应regime
   输入: factor_signal, aux_bars, current_direction, spread_trend_filter, spread, coint_p
   输出: SignalResult(entry_signal, direction, should_trade, should_exit, should_reverse)

4. RiskMgr.check() — 风控检查(4条内置规则)
   输入: context, sub, spread, spread_calc
   输出: 触发->描述字符串 / 未触发->None

5. ArbExecHelper.execute() — 执行协调(状态机)
   输入: context, sub, signal
   效果: 调用stra_set_position下单

6. StateManager.save_all() — 状态持久化

## 二、各模块详细流程

### 1. DataPipe — 数据获取与价差计算

入口: fetch(context, cur_date) -> BarData

流程:
1. 获取主周期K线(m5, 300根)
   - 对每个leg调用 context.stra_get_bars(code, "m5", 300)
   - 任一品种数据为空 -> return None

2. 获取辅助周期K线(d1, 60根)
   - context.stra_get_bars(code, "d1", 60)
   - 存入 aux_bars["SHFE.rb.HOT#d1"]

3. 提取价格/成交量
   - prices[code] = bars.closes, volumes[code] = bars.volumes
   - min_len = min(len(closes))，不足30根 -> return None

4. 计算价差
   - spread_mode == "coint": _calc_coint_spread()
     a. price_spread = SpreadCalculator.calc_price_spread()
     b. FactorEngine.refit_coint() -- 仅OLS+ADF，不计算因子
     c. beta, c = FactorEngine.get_coint_params()
     d. coint_spread = norm_coeff_rb*rb - beta*norm_coeff_i*i - c
   - spread_mode == "price": SpreadCalculator.calc_price_spread()

5. 更新布林带: update_bands(spread, lookback=60, threshold=2.0) -> mean/std/upper/lower

6. 计算日线趋势过滤: _calc_trend_filter()
   - 有日线: daily_spread = norm_c1*daily_rb + beta*norm_c2*daily_i
     slope_z = (MA5-MA20)/STD20, >0.5->1(上升), <-0.5->-1(下降)
   - 无日线: 用5min价差 MA20 vs MA60, >1.0->1, <-1.0->-1

价差计算核心(SpreadCalculator) — 名义价值比例归一化:
  pnl_coeff[rb] = lots(3)*multiplier(10)*sign(+1) = 30
  pnl_coeff[i]  = lots(2)*multiplier(100)*sign(-1) = -200
  total_abs = 230
  norm_coeff[rb] = +0.130, norm_coeff[i] = -0.870
  价差 = 0.130*rb_price - 0.870*i_price (i主导87%)

### 2. FactorEngine — 因子计算与聚合

入口: calculate(spread, prices, volumes, aux_bars, **kwargs) -> dict

当前启用4个因子:
| 因子 | 类 | direction_type | 权重 | 含义 |
|------|-----|---------------|------|------|
| zscore | ZScoreFactor | reversal | 0.35 | 滚动Z标准化(60根) |
| coint_residual | CointResidualFactor | reversal | 0.30 | 协整残差Z(200根,refit间隔5天) |
| half_life | HalfLifeFactor | reversal | 0.20 | 半衰期*偏离方向(120根) |
| spread_momentum | SpreadMomentumFactor | momentum | 0.15 | 价差动量(快10/慢30) |

因子计算流程:
  1. calculate(spread, prices, ...) -> raw_value
  2. _normalize(raw_value) -> [-1, 1]
     - rank窗口>=20: 排名百分位标准化
     - rank窗口<20: clip归一化(raw/3, clip到[-1,1])
  3. scores[因子名] = 标准化值

模式方向调整(MODE_DIRECTION_MAP):
  当前模式: adaptive -> momentum/reversal/neutral均(1.0, 不翻转)
  adaptive模式下FactorEngine不做方向调整，由MultiPeriodSignal处理

加权聚合:
  total_score = Sum(scores[i]*effective_weight[i]*flip_sign[i]) / Sum|effective_weight[i]|
  direction = +1 if total_score>0.1, -1 if total_score<-0.1, 0 otherwise

协整refit流程(CointResidualFactor):
  触发: 首次 OR 距上次refit>=effective_interval天
  effective_interval = refit_interval(5) / 2^fail_count (指数退避)
  步骤:
  1. p1,p2 = _apply_norm_coeffs(prices, norm_coeffs)
  2. OLS回归: p1 = beta*p2 + c -> beta, c
  3. ADF检验: adfuller(residual, maxlag=1) -> p值
  4. p<=0.05 -> 协整有效,更新beta/c,fail_count=0; p>0.05 -> beta=None,fail_count+=1
  5. 指数退避: 失败后重试间隔缩短(5->2->1天)

### 3. SignalGen + MultiPeriodSignal — 信号生成

入口: SignalGen.generate() -> SignalResult
内部调用: MultiPeriodSignal.generate(factor_signal, aux_bars, current_direction, ...)

adaptive模式信号生成流程:

1. _adaptive_signal(total_score, factor_scores, spread_trend_filter, spread)
   a. 大方向判断:
      major_trend = spread_trend_filter(日线趋势,来自DataPipe)
      若major_trend==0: 用5min价差MA斜率z-score判断
        slope_z = (MA20-MA60)/STD60, >1.0->1, <-1.0->-1
   b. 瞬时regime判定:
      - major_trend!=0:
        信号与趋势同向 -> trend_aligned_mr, signal*1.2(顺势增强)
        信号与趋势反向 -> trend_counter_mr, signal*0.5(逆势衰减)
        信号为零 -> uncertain, signal=0
      - major_trend==0(无趋势):
        half_life强度>0.1 -> mean_revert, signal不变
        half_life强度<=0.1 -> uncertain, signal*0.5
   c. regime状态记忆(v4新增):
      _regime_history.append(instant_regime)
      取最近5 bars的regime投票 -> 多数决定当前regime
      self._current_regime = Counter(history).most_common(1)[0][0]

2. 协整门控衰减: coint_p>0.05 且 regime非trend_follow/trend_aligned_mr -> signal*0.8

3. 动量确认: regime==mean_revert时,MR信号方向与momentum不一致 -> signal*0.8

4. 入场阈值: effective_threshold=0.45, 做空方向: 0.45*1.2=0.54

5. 信号持续性过滤: 最近3 bars信号方向必须一致; 强信号(>0.675)跳过

6. 趋势行情逆势屏蔽: regime==trend_follow时逆势信号trend_blocked=True

7. 最终入场: should_trade = |final_signal|>threshold AND current_direction==0 AND persistent AND NOT blocked

8. 出场判断(持仓时):
   - 反向信号>entry_threshold -> should_reverse=True
   - 信号<exit_threshold(0.08) -> should_exit=True
   - 追踪止损: 信号从peak回落>trailing_decay(0.3)且持续>=6 bars -> should_exit=True

### 4. RiskMgr — 风控检查

入口: check(context, sub, spread, spread_calc) -> str|None

4条内置规则(按优先级顺序):

1. spread_deviation — 价差偏离度
   |spread[-1]-mean|/std > max_spread_deviation(4.0)
   触发: "spread_deviation: 5.2 > 4.0"

2. max_loss — 单组合最大亏损
   Sum(各腿pnl) < -max_loss_per_pair(-8000)
   触发: "max_loss: -9500 < -8000"

3. profit_take — 盈利保护(v4新增)
   peak_unrealized > profit_take_threshold(3000)
   AND 当前pnl < peak*profit_drawdown_ratio(0.5)
   触发: "profit_take: peak=5000, current=2000, drawdown=60%"
   逻辑: 盈利曾达5000(>3000目标),回撤到2000(<2500=5000*0.5) -> 平仓保护

4. time_stop — 时间止损
   hold_bars > max_hold_bars(120)
   触发: "time_stop: held 125 bars (>120) with pnl=-500"

风控状态管理:
- hold_bars: 持仓bar计数(每bar+1,空仓归零)
- cooldown: 冷却期(风控平仓后12 bars内禁止开仓)
- peak_unrealized: 持仓期间最大未实现盈利(每bar更新)
- _closing: 风控平仓进行中标志(防止重复触发)
- realized_pnl: 累计已实现盈亏(平仓时累加unrealized_at_close)

### 5. ArbExecHelper — 执行协调(状态机)

状态流转:
  IDLE -> OPENING(开仓) -> OPENED(全部成交) -> CLOSING(平仓/反转) -> IDLE
  反转时: CLOSING完成后立即以新方向OPENING

开仓逻辑 _open_position(context, sub, direction):
  1. state=OPENING, _direction=direction
  2. 计算各腿目标仓位:
     ratio>0: target=direction*lots (rb: +1*3=3 或 -1*3=-3)
     ratio<0: target=-direction*lots (i: -1*2=-2 或 +1*2=2)
  3. stra_set_position(code, target) 对每腿下单
  4. 记录pending: {code: {target, retry_count, timestamp}}

平仓逻辑 _close_position(context, sub, reverse_after):
  1. state=CLOSING
  2. stra_set_position(code, 0) 对每腿平仓
  3. reverse_after=True时保存_pending_direction供反转

on_tick_check — 成交确认与追单:
  1. 检查pending中每腿:
     cur_pos==target -> 成交,移入filled
     超时(实盘30秒/回测30 ticks):
       retry<3 -> 重新下单; retry>=3 -> 紧急全平,state->IDLE
  2. 所有pending成交后:
     OPENING->OPENED; CLOSING->反转开仓或IDLE

### 6. 主流程编排(SpreadArbStrategy._process_sub)

_process_sub(context, sub) 完整流程:

1. 仓位同步: 实际仓位==0但direction!=0 -> exec_helper.reset(), rm.on_position_cleared()
2. 状态机推进: exec_helper.on_tick_check(context, sub)
3. DataPipe.fetch() -> bar_data (None则return)
4. FactorEngine.calculate() -> factor_signal {total_score, factor_scores, direction, coint_p}
5. 方向推断: actual_direction = exec_helper.infer_direction(context)
   (用各腿实际持仓*lots加权判断,修复time_stop方向丢失)
6. RiskMgr.update_hold_bars(current_direction)
7. SignalGen.generate() -> signal (SignalResult)
8. 指标写入(context.set_index_value)
9. 缓存未实现盈亏: rm.cache_unrealized(Sum各腿position_profit) -> 更新peak_unrealized
10. 交易决策(优先级从高到低):
    - 风控触发: rm.on_risk_close() + exec_helper.close_all() + 冷却期12 bars
    - 开仓信号: 冷却期内tick_cooldown(); 冷却期外exec_helper.execute()
    - 出场/反转: rm.on_signal_close() + exec_helper.execute()
    - 无信号: rm.tick_cooldown() (冷却期递减)
11. 状态持久化: StateManager.save_all(context, direction, risk_mgr, now)

## 三、关键数据流

配置层:
  strategy_config.yaml -> ConfigManager -> 子策略/risk/adaptive参数
  factor_config.yaml -> FactorEngine -> 因子权重/阈值/signal_mode

运行时数据流:
  context.stra_get_bars()
    -> DataPipe.fetch() [prices, volumes, spread, spread_trend_filter]
      -> FactorEngine.calculate() [4因子->total_score]
        -> MultiPeriodSignal.generate() [regime判定+信号增强/衰减->final_signal]
          -> RiskMgr.check() [4条风控规则]
            -> ArbExecHelper.execute() [状态机: IDLE->OPENING->OPENED->CLOSING->IDLE]
              -> StateManager.save_all() [direction, risk_state, last_signal_time]

## 四、生命周期回调

on_init():
  1. stra_prepare_bars() — 注册K线订阅(m5主周期+d1辅助)
  2. stra_sub_ticks() — 订阅tick(用于成交确认)
  3. register_index() — 注册价差/因子指标线
  4. StateManager恢复direction -> exec_helper.restore_state()

on_session_begin():
  -> FactorEngine.reset_refit() — 每交易日重置refit间隔
  -> CointResidualFactor._last_refit_day = -999

on_session_end():
  -> 日志输出: realized_pnl + unrealized + total

on_tick():
  -> exec_helper.on_tick_check() — 成交确认+追单

on_bar():
  -> 已废弃(辅助周期数据在_process_sub中直接获取)

## 五、当前v4核心参数一览

[策略参数]
  品种: rb(SHFE) + i(DCE)
  主周期: m5, 辅助: d1
  信号模式: adaptive
  价差模式: coint

[仓位]
  rb: 3手(10吨/手), i: 2手(100吨/手)
  归一化: rb=+0.130, i=-0.870

[因子权重]
  zscore=0.35, coint_residual=0.30, half_life=0.20, momentum=0.15
  entry_threshold=0.45, exit_threshold=0.08

[风控]
  max_loss=8000, max_hold=120bars, cooldown=12bars
  profit_take=3000, profit_drawdown=0.5
  spread_deviation=4.0sigma, short_entry_mult=1.2

[信号]
  顺势MR增强*1.2, 逆势MR衰减*0.5
  协整不显著衰减*0.8, 动量不确认衰减*0.8
  regime记忆5 bars投票, 信号持续性3 bars
  trailing_decay=0.3, trailing_exit_bars=6

## 六、v4已知瓶颈

1. 品种问题: rb-i 5分钟级别不协整(ADF p>0.05)，MR逻辑根基薄弱
2. i主导价差: norm_coeff[i]=-0.87，spread方向87%由i决定，rb几乎无影响
3. coint_residual分布: >0仅18%，严重左偏，信号质量差
4. 胜率低: 37.7%，仅49+bars持仓区间盈利，短持仓(1-24bars)持续亏损

回测结果: realized=-8,520, 收益率-6.05%, 回撤6.24%, 146笔, PF=0.68

## 七、文件清单

| 文件 | 行数 | 职责 |
|------|------|------|
| strategies/SpreadArbStrategy.py | 329 | 主策略编排器 |
| helpers/DataPipe.py | 213 | 数据获取+价差计算+趋势过滤 |
| helpers/SpreadCalculator.py | 128 | 价差计算(名义价值归一化) |
| helpers/MultiPeriodSignal.py | 314 | 多周期信号+自适应regime |
| helpers/SignalGen.py | 72 | 信号标准化(SignalResult) |
| helpers/RiskMgr.py | 219 | 风控检查(4条规则+插件) |
| helpers/ArbExecHelper.py | 297 | 执行协调(状态机) |
| helpers/ConfigManager.py | 92 | 配置加载+校验 |
| helpers/StateManager.py | 74 | 状态持久化 |
| factors/FactorEngine.py | 228 | 因子引擎(加权聚合+模式方向) |
| factors/FactorBase.py | 94 | 因子基类(rank标准化) |
| factors/stat_factors.py | 229 | ZScore/CointResidual/HalfLife因子 |
| config/strategy_config.yaml | 35 | 策略配置 |
| config/factor_config.yaml | 72 | 因子配置 |

### 7. StateManager — 状态持久化(86行)

入口: save_all(context, direction, risk_mgr, last_signal_time) / load_all(context, risk_mgr)

v4修复: 使用CtaContext正确API:
- user_save_data(key, val) — 保存(自动转str)
- user_load_data(key, defVal, vType) — 加载(需指定类型)

存储内容:
- {sub_name}_direction: int — 交易方向(1/-1/0)
- {sub_name}_risk_state: JSON — RiskMgr完整状态(hold_bars/cooldown/realized_pnl/closing)
- {sub_name}_last_signal_time: int — 上次信号时间

恢复流程(on_init):
1. load_all() → direction, last_signal_time
2. direction!=0 → ArbExecHelper.restore_state(direction)
3. RiskMgr.restore(risk_state_dict)

## 三、主流程编排(SubStrategy.process)

```
def process(context, cur_date):
    # 1. 仓位同步
    actual_has_pos = any(stra_get_position(leg.code) != 0 for leg in self.legs)
    if not actual_has_pos and self.eh.direction != 0:
        self.eh.reset()
        self.rm.on_position_cleared()

    # 2. 状态机推进(追单/成交确认)
    self.eh.on_tick_check(context, self._sub)

    # 3. DataPipe
    bar_data = self.dp.fetch(context, cur_date)
    if bar_data is None: return  # 数据不足

    # 4. RegimeDetect(前置) — v4核心改进
    regime_dict = self.sg.detect_regime(bar_data.spread, bar_data.aux_bars, coint_p=1.0)
    regime = regime_dict.get('regime', 'range')

    # 5. 应用regime权重 — 在calculate之前
    self.fe.apply_regime_weights(regime)

    # 6. FactorEngine
    factor_signal = self.fe.calculate(bar_data.spread, prices=bar_data.prices,
        volumes=bar_data.volumes, aux_bars=bar_data.aux_bars, cur_date=cur_date,
        norm_coeffs=self.sc.get_norm_coeffs())

    # 7. SignalGen(使用已检测的regime，不重复检测)
    current_direction = self.eh.direction
    actual_direction = self.eh.infer_direction(context)
    if actual_direction != 0 and current_direction == 0:
        current_direction = actual_direction
    self.rm.update_hold_bars(current_direction)

    signal = self.sg.generate_with_regime(factor_signal, bar_data.aux_bars,
        current_direction, spread_trend_filter=bar_data.spread_trend_filter,
        spread=bar_data.spread, coint_p=factor_signal.get('coint_p', 1.0),
        regime_dict=regime_dict)

    # 8. 更新指标(regime_val/dir/conf + factor_scores)
    self._update_indicators(context, bar_data, factor_signal, signal)

    # 9. 风控+交易决策
    self._execute_decision(context, bar_data, signal, factor_signal, current_direction)

    # 10. 持久化
    now = context.stra_get_date() * 10000 + context.stra_get_time()
    self.sm.save_all(context, self.eh.direction, self.rm, now)
```

_execute_decision 交易决策逻辑:
```
if current_direction != 0:
    cur_unrealized = sum(stra_get_position_profit(leg.code) for leg in self.legs)
    self.rm.cache_unrealized(cur_unrealized)

risk_result = RiskMgr.check() if current_direction!=0 and not closing else None

if risk_result is not None:
    # 风控平仓(最高优先级)
    RiskMgr.on_risk_close()
    ArbExecHelper.close_all(tag="RiskClose")
elif signal.should_trade:
    # 开仓(需通过冷却期)
    if RiskMgr.is_in_cooldown():
        RiskMgr.tick_cooldown()
    else:
        ArbExecHelper.execute(signal)
elif signal.should_exit or signal.should_reverse:
    # 信号平仓/反转
    RiskMgr.on_signal_close()
    ArbExecHelper.execute(signal)
else:
    # 无信号
    RiskMgr.tick_cooldown()
```

## 四、完整参数表

### strategy_config.yaml

| 参数 | 值 | 含义 |
|------|-----|------|
| signal_mode | adaptive | 信号模式(adaptive/trend/mean_revert) |
| entry_threshold | 0.80 | 入场阈值(网格搜索最优) |
| exit_threshold | 0.12 | MR出场阈值 |
| trend_aligned_boost | 1.3 | 趋势顺势信号增强倍率 |
| trend_counter_decay | 0.3 | 趋势逆势信号衰减倍率 |
| range_signal_decay | 0.8 | range信号衰减倍率 |
| coint_decay | 0.7 | 协整不显著时衰减系数 |
| momentum_decay | 0.7 | 动量不确认时衰减系数 |
| signal_persist_bars | 3 | 持续性过滤bar数 |
| persist_logic | OR_2of3 | 持续性逻辑(3中2同向) |
| short_entry_multiplier | 1.5 | 做空门槛倍率 |
| trailing_exit_bars | 6 | MR追踪止损持续bar数 |
| trailing_decay_ratio | 0.35 | MR追踪止损衰减比 |
| profit_protect_peak | 0.65 | 盈利保护触发峰值 |
| profit_protect_floor | 0.30 | 盈利保护平仓阈值 |
| trend_trailing_ratio | 0.5 | 趋势追踪止损回撤比 |
| trend_trailing_bars | 3 | 趋势追踪止损持续bar数 |
| range_exit_threshold | 0.4 | range快速出场阈值 |
| cooldown_bars | 12 | 风控冷却期bar数 |
| max_hold_bars | 120 | 最大持仓bar数 |
| max_loss_per_pair | 8000 | 单组合最大亏损(元) |
| max_spread_deviation | 4.0 | 价差偏离度阈值(σ) |
| profit_take_threshold | 3000 | 盈利保护触发阈值(元) |
| profit_drawdown_ratio | 0.5 | 盈利回撤比 |

### factor_config.yaml

| 因子 | 权重 | direction_type | 关键参数 |
|------|------|---------------|----------|
| zscore | 0.25 | reversal | lookback=60 |
| coint_residual | 0.20 | reversal | lookback=200, refit_interval=5, coint_p_threshold=0.05 |
| half_life | 0.10 | reversal | lookback=120, max_half_life=30, min_half_life=1 |
| spread_momentum | 0.15 | momentum | fast_period=10, slow_period=30 |
| macd_spread | 0.10 | momentum | fast=12, slow=26, signal=9 |
| adx_spread | 0.05 | momentum | period=14, threshold=25 |
| channel_break | 0.05 | momentum | period=20, breakout_threshold=0.5 |
| trend_strength | 0.10 | momentum | short=10, long=30, threshold=0.3 |

## 五、v4修复清单与回测改善

| 修复 | 问题 | 修复方案 | 影响 |
|------|------|----------|------|
| P0: regime前置 | FactorEngine权重时序错位 | detect_regime→apply_regime_weights→calculate | 消除1 bar延迟 |
| P3: range衰减 | 三重衰减0.32(0.8*0.7*0.57) | range_signal_decay=0.8,移除双重coint_decay乘法 | 信号不再过度衰减 |
| P4: SHORT限制 | 做空胜率25.3%系统性亏损 | short_entry_multiplier=1.5,趋势确认,range不禁止 | 做空门槛提高 |
| P5: 架构解耦 | RegimeDispatchStep内部重复检测 | regime由SubStrategy前置,Step只使用注入值 | 消除重复计算 |
| StateManager | user_read_data不存在 | 改用user_load_data(key, defVal, vType) | 持久化正常工作 |
| ExitJudgeStep | 无条件coint_decay_val乘法 | 仅在确认失败时衰减 | 消除双重衰减 |

改善轨迹: -11.96%(v3) → -3.58%(v4), 超过v8f基准(-4.17%)

## 六、已知瓶颈与改进方向

### 根本瓶颈

1. **rb-i 5min不协整**: ADF p>0.05，MR逻辑根基薄弱
   - i主导价差87%(norm_coeff=-0.87)
   - 价差方向几乎由i决定，rb贡献仅13%
   - 改善: 换品种对(rb-hc/m-y)或降频(15min)

2. **盈亏比0.62偏低**: 平均亏损563 > 平均盈利346
   - 出场时机偏晚，亏损交易持仓时间过长
   - 改善: 追踪止损更激进/盈利保护更早触发

3. **SHORT方向几乎无交易**: entry=0.80*1.5=1.20门槛过高
   - 信号强度中位数0.707，做空几乎不可能触发
   - 改善: regime-adaptive阈值(trend下做空门槛降低)

4. **交易笔数偏少**: 28笔/3个月，平均2.7笔/周
   - 高阈值过滤掉大量潜在交易
   - 改善: 降低阈值+改善信号质量(而非降低门槛)

### 改进方向

1. **换品种对**: rb-hc(螺纹-热卷)或m-y(豆粕-玉米)协整性更好
2. **降频**: 15min级别协整性可能改善
3. **出场优化**: 追踪止损/盈利保护改善盈亏比
4. **regime-adaptive阈值**: 不同regime使用不同entry_threshold
5. **回测区间扩展**: 至少1年数据覆盖不同市场状态
6. **SHORT方向优化**: trend regime下做空门槛降低(1.0x而非1.5x)
