# 第二阶段实施记录（2026-10-26）

> 范围：trigger/close 语义、事件时序、加仓与组合资金、组合研究及复现。
> 本文不把短窗口收益作为新绩效结论，不声明多 profile、完整换月数据链或
> 生产 OOS 已完成。未刷新历史黄金基线，未部署生产。

## 1. trigger 通道统一

- 新增 `next_*` 通道：常规次日武装使用最近 N 根（含刚闭合 bar）。
- `arm_exclude_signal=true` 使用已排除信号根的 current 通道，并忽略
  `trigger_envelope`，不再发生 prev + 当根包络导致“排除失效”。
- StraTurtle 与 PortfolioSim 在评估同一根时共用 current/prev/next 三组值，
  消除研究错位一根。entry/exit/fs 同口径。
- trigger 过滤否决方向进入 `disarmed` 并持久化，exit/rearm/重启不再复活；
  新评估日才重新裁决。

## 2. 事件与成交价时序

- PortfolioSim trigger 不再在早盘前使用当日 d1 close；每个 bar 以 open
  为事件起点，决策后推进 close，日终才用 d1 close 记账。
- h1 策略回测先按旧武装消费 m5，再闭合 h1 评估未来；h1 组合研究同样
  不在 check_bar 前使用本桶 close。
- close 的持仓管理使用根前 N；当根 N 用于新决策/下一根，防止用收盘后
  收紧的止损回溯触发本根 earlier low/high。prev_n 入快照。
- on_price 的 stop/channel/add 使用实际 tick 价，不再按理想武装价美化跳空。
- close 组合研究传入 open，跳空成交价与 S1 输赢保持同源。

## 3. 加仓与组合资金

- `post_risk_base/post_risk_per_lot` 接入风控：加仓按假设成交后的共同
  止损重锚计算 post-risk，拦截确认后才会穿透预算的请求。
- 日评估、成交均价回写后同步风险账；锁盈到 0 风险时重启也保留 0。
- 总风险预算使用独立 `portfolio_equity`（总池 + 全部 sid 净盈亏），
  本品种/保证金仍用 sid 分配权益；未启用分配时保留旧行为。
- `register_allocated_capital` 统一 runLive/deploy 的分配注册，消除生产
  模板漏注册回退到每策略 15M 的入口漂移。

## 4. 组合研究成本、换月与窗口

- 费率键按大小写候选匹配，缺失 fail-loud；异常比例费率阻断。
- INE.SC 已按项目既有 20 元/手口径把实际 `common/fees.json` 的
  `byvolume` 修为 true（该文件是外部链接目标，仅改单位，不改数值）。
- 平仓按每层开仓日分别计算平今/平昨，覆盖隔夜首层+当日加仓。
- 新增 hots 换月日历：有价差时平移层价/武装/估值锚，缺价差仅在换月日
  用 open-prev_close 估算并告警；结果记录 roll 事件与日历覆盖。
- 当前本地 hots 无历史规则时明确输出 `roll_calendar_products=0` 和
  “跨换月持仓盈亏未覆盖”，不再默认为已解决。
- h1/d1 混合只用共同交易日，不冻结期末权益；速度 blend 按日期合并，
  起止区间外的品种不占权重。

## 5. 复现与工程防线

- PortfolioSim 输出 manifest：代码版本、配置、费率、commodities、hots、
  实际 d1/m5 数据 hash；A/B 聚合拒绝不同 config/spec/manifest 的结果。
- 速度研究输出同型 manifest 并已按真实数据重生成。
- 黄金基线：配置 hash 不一致、输入指纹不一致或基线缺失均显式失败；
  不再自动接受当前输出。
- `has_bars` 需要有效可读行，header-only/不可解析数据不再视为可用。
- walk-forward 非法月份不循环、全失败不写空报告，保留 fallback 标志。
- 配置：quoted bool、浮点 speed 窗口不再静默改写；notify 开启必须有 URL。
- ghost trim 支持末层部分裁剪，非边界目标不再多裁真实仓。

## 6. 验证

- **541 passed**（56 个测试文件）；Ruff 全部通过。
- trigger/close 短窗口组合验证均完成，输出到 `matrix_results/_validation`，
  包含 manifest；`roll_calendar_products=0`，仅作链路验证，不作绩效宣传。
- 黄金基线在语义修正后从 33.37% 变为 21.85%，属声明式口径变化；
  当前还会因配置 hash 不同而阻断。**未执行 `--update`，历史基线未刷新。**

## 7. 仍未完成/后续条件

1. 多 profile 的毛决策、净目标与逐 profile 成交归因需统一 ExecutionBatch；
2. 换月日历完整后，需继续验证 ATR/Donchian 窗口的换月连续性；
3. 生产真实 webhook、共享账户订单级 userTag、组合 OOS/容量压力测试；
4. h1 与多速度须在新事件语义和共同窗口下重新评估，不沿用旧负/正结论。
