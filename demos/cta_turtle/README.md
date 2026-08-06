# cta_turtle — 海龟趋势跟踪策略 (wtpy CTA)

基于 WonderTrader/wtpy CTA 框架的经典海龟交易系统，商品期货主力合约趋势跟踪。
支持日K/小时K 双周期（每品种独立配置）、VaR 波动率仓位反算、方向闸、双系统 S1+S2、经典加仓金字塔。

## 策略逻辑

| 组件 | 规则 |
|---|---|
| N | ATR(20) Wilder 平滑，交易周期上计算 |
| S1 入场 | 收盘突破 20 周期高/低点（边沿触发） |
| S1 出场 | 反向 10 周期突破 或 2N 止损 |
| S1 过滤 | 上次 S1 突破盈利 → 跳过本次；被跳过则武装 fail-safe：价格继续突破 55 周期强制入场；被跳过的模拟单 2N 止损后过滤器解除 |
| S2 | 55 周期突破入场 / 20 周期出场，无过滤 |
| 加仓 | 每盈利 0.5N 加一层（可配 `pyramid_step_atr`），上限 `pyramid_layers`（1=不加仓），加仓后整体止损上移至最后加仓价 ∓ 2N |
| 止损 | 2N 硬止损，bar 内 low/high 触碰判定，按止损价离场 |
| 方向闸 | `direction: both/long/short`，信号方向不符 → 禁止开仓（不触碰 S1 过滤器状态） |

## VaR 仓位反算

```
每层手数 lots = floor(units × unit_var / (N × volscale))
```

- `unit_var`: 1 个交易单位的 VaR 预算（默认 20 万，**1N 口径**：持仓 1 倍 ATR 不利波动 ≈ 预算亏损）
- `units`: 每品种配置的交易单位数（如 2 → 每层 40 万 VaR）
- `volscale`: 合约乘数，取自框架 `stra_get_comminfo()`（非配置抄写）
- 2N 止损触发时单层实际最大亏损 ≈ 2 × units × unit_var
- 预算不足 1 手 → 放弃该次入场（日志 veto）

## 快速开始

```bash
# 单品种回测
python runBT.py --code SHFE.rb.HOT --start 201801010900 --end 202304031500

# 全品种矩阵回测 (serial=进程内串行 / subprocess=子进程隔离)
python run_matrix.py --mode subprocess --start 201801010900 --end 202304031500

# 实盘 (多品种多实例 + WtMonSvr 监控)
python runLive.py --port 8080   # 先填 tdparsers.yaml/tdtraders.yaml 账户
```

回测产出：`outputs_bt/<stra>/` + `Strategy[...]_PnLAnalyzing_*.xlsx`（WtBtAnalyst）。
回测图形化复盘：`WtBtSnooper` 读取 outputs_bt。实盘 GUI：`http://127.0.0.1:8080/console`。

## 配置 (config/turtle_config.yaml)

```yaml
base:
  unit_var: 200000        # 1 单位 VaR = 20万 (1N 口径)
  atr_period: 20
  stop_atr_mult: 2.0
  pyramid_layers: 4       # 最大层数(含首仓), 1=不加仓
  pyramid_step_atr: 0.5
  bar_cnt: 120            # 需 >= s2_entry + atr_period
instruments:
  - code: SHFE.rb.HOT
    period: d1            # d1 | h1 (每品种独立)
    system: S1            # S1(20/10+过滤) | S2(55/20)
    units: 2              # 每层 2×20万=40万 VaR
    direction: both       # both | long | short
```

**品种启用前提**（缺任一项 C++ 回放器会段错误）：
1. `common/hots.json` 含该品种键（空列表=不换月即可）
2. `storage/` 有对应 period 的 K 线数据（h1 需小时线，可用 `wtpy.apps.WtHotPicker` + datahelper 准备）

## 框架对齐

- `.HOT` 主力连续合约：C++ 核心按 hots.json 自动换月移仓；策略内用 `avgpx` 漂移 re-anchor 各层成本价（换月后止损价连续）
- 图表 GUI：`set_chart_kline` + 主图通道/止损线 + 副图 ATR/持仓直方图 + 进出场 `add_chart_mark` → console 监控与回测 btchart 原生显示
- 持久化：`user_save_data` 引擎快照（方向/层/S1过滤器/fail-safe），重启恢复；孤儿持仓自动单层重建
- 执行：`stra_set_position` 目标仓位语义；live 由 `executers.yaml`（WtMinImpactExeUnit）落单，`actpolicy.yaml` 先平后开
- 组合风控：`env.riskmon`（SimpleRiskMon 回撤熔断）纯配置；通道合规 `tdtraders.yaml` riskmon
- 回测约束：`WtBtEngine` 单策略上下文 → 多品种须 `run_matrix.py` 逐品种回测后汇总

## 与经典海龟的偏离（框架适配必要妥协）

1. **止损用当前 N 每 bar 重算**（非入场时固定 N）：.HOT 换月后绝对入场价失效，改用框架调整后的 avgpx + 当前 N 重算
2. **信号收盘确认**（非盘中突破）：bar-close 语义，突破判定用当根收盘 vs 前 N 根通道（不含当根，无未来函数）
3. **avgpx 符号约定**：框架空头 avgpx 为负值（avgpx×pos=成本>0），成交价反推与换月检测均已做符号处理

## 测试

```bash
python3 -m pytest tests/ -q    # 57 passed
```

覆盖：ATR/Donchian 等价性（对照朴素循环）、S1/S2 入场出场边沿、2N 止损、加仓层数/步长、
S1 盈利跳过/fail-safe/sim 单推进、方向闸、VaR 反算、配置校验、换月 re-anchor、
avgpx 符号、on_calculate 全链路。

## 文件结构

```
StraTurtle.py            # 策略壳: 生命周期/图表/执行/持久化 (框架对接层)
helpers/
  indicators.py          # Wilder ATR (lfilter) + Donchian (sliding_window_view)
  TurtleEngine.py        # 信号状态机 (纯逻辑, 无框架依赖)
  PositionSizer.py       # VaR 反算
  ConfigLoader.py        # yaml 加载校验
schemas.py               # 配置 dataclass
runBT.py / run_matrix.py / runLive.py
configbt.yaml            # 回测引擎配置
config.yaml + tdparsers/tdtraders/executers/actpolicy/filters/logcfg  # 实盘模板
tests/                   # 57 个单测
```
