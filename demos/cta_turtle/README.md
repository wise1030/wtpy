# cta_turtle — 海龟趋势跟踪策略 (wtpy CTA)

基于 WonderTrader/wtpy CTA 框架的经典海龟交易系统，商品期货主力合约趋势跟踪
（日线信号，每品种独立配置）。核心能力：VaR 波动率仓位反算、**双模式信号确认**
（`trigger` 盘中武装-触发，生产默认 / `close` 收盘确认）、**可插拔信号过滤器**
（方向闸/ADX/波动率带/流动性下限）、**组合级风险预算**（单品种本笔+单品种累计+
总敞口+保证金+相关性组单位限制+共享账户资金闸）、**双系统 S1+S2 并行**（各半
units 共享品种预算）、经典加仓金字塔、m5 盘中止损、**小时级 h1 信号**
（策略层 m5→h1 聚合，2026-10-21 一期）、交易通道真值对账
（rtdata.json 持仓/资金 + trades.csv 逐笔成交）、walk-forward 滚动寻优、
**共享账本组合回测**（close/trigger 双模式，真正执行组合约束）。

> 变更历史与路线全貌见 `../AGENTS.md`（本 README 只描述当前状态）。

## 系统架构

```
TurtleEngine (纯逻辑零依赖)   信号状态机: 武装-触发/加仓金字塔/S1过滤+fail-safe/2N止损
     ▲
StraTurtle (框架壳)           生命周期/图表/执行/持久化/对账/盘中止损/武装-触发接线
     │
├─ filters/                   可插拔信号过滤链 (方向闸/ADX/波动率带/流动性, 只读不改引擎)
├─ core/                      被 import 的策略核心库 (纯逻辑零框架依赖)
│   TurtleEngine/Indicators   信号状态机 + Wilder ATR/Donchian (向量化)
│   RiskManager               下单前最后闸门: 单品种本笔/累计/组合预算+相关性组+保证金+共享账户资金闸
│   LiveTruth                 交易通道真值 (rtdata.json 真实持仓/资金 + trades.csv 逐笔成交)
│   ExecJournal               执行层订单状态机+归因 (旁路侧车 scripts/exec_journal.py)
│   aggregate                 m5→h1 聚合 (h1 一期, backtest/live 同源)
│   PositionSizer/BarJournal  VaR 反算 / K线旁路日志 (宕机窗口幂等回补)
│   DataChecker/ConfigLoader  数据预检 (时效断言) / yaml 校验 (未知键拒绝)
└─ schemas.py                 配置 dataclass (唯一权威定义)
```

运行时三层防御：**python RiskManager**（缩量优先）→ **真值链检测**（ghost_fill/
对账/mismatch）→ **C++ SimpleRiskMon**（账户级回撤熔断）。实盘 tick 驱动触发，
状态随触发事件落快照，重启逐字节恢复；每日 20:40 hotdaily 重建 HOT 日线 DSB
并重启交易进程（reader 缓存无失效 API），顺带重置执行单元 + 复活真值查询链。

## 策略逻辑

| 组件 | 规则 |
|---|---|
| N | ATR(20) Wilder 平滑，交易周期上计算 |
| S1 入场 | 突破 20 周期高/低点：`entry_mode: close`=收盘确认（边沿触发）；`trigger`=盘中触及即入场（经典海龟原生，生产默认） |
| S1 出场 | 反向 10 周期突破 或 2N 止损 |
| S1 过滤 | 上次 S1 突破盈利 → 跳过本次；被跳过则武装 fail-safe：价格继续突破 55 周期强制入场；被跳过的模拟单 2N 止损后过滤器解除（`enable_s1_filter` 可关闭） |
| S2 | 55 周期突破入场 / 20 周期出场，无过滤 |
| 双系统 | `system: "S1+S2"` 两子系统并行，各分 ceil/floor 半量 units，独立通道/引擎状态，框架净仓=两者之和，品种内风险/保证金预算按 units 占比切分 |
| 加仓 | 每盈利 0.5N 加一层（可配 `pyramid_step_atr`），上限 `pyramid_layers`（1=不加仓），加仓后整体止损上移至最后加仓价 ∓ 2N；`stop_n_mode=ratchet` 时加仓位只在两次加仓之间单调向不利方向（防 N 缩小时价格未推进就加仓） |
| 止损 | 2N 硬止损，bar 内 low/high 触碰判定，按止损价离场；`stop_n_mode` 控制 N 口径（默认 ratchet=只收紧不放宽，见「与经典海龟的偏离」）；`intrabar_stop: true` 时主K切换为分钟线（默认 m5）**盘中即时离场**；`entry_mode: trigger` 时止损/加仓/通道出场全部由 tick 武装位盘中触发 |
| 触发执行 | `entry_mode: trigger`（方案C）：日评估仅武装价位（含当根包络通道），tick 穿越即触发；**过滤器于武装时（收盘）前置裁决，风控于触发时复核当前权益**；否决→当日撤武装防刷屏；实盘日线不闭合（F3）由 session_end 用当日 m5 聚合合成日K评估；回测走 check_bar 触碰仿真（跳空按开盘价/触碰按武装位，填充价=信号bar收盘，与 close 模式同填充模型——纯信号语义 A/B） |
| 过滤链 | 引擎入场决策后、下单前依次判定：隐式方向闸(`direction`) + `filters:` 显式配置链，任一否决即放弃（不触碰引擎状态） |
| 组合风控 | 下单前最后闸门（链式，逐项缩量）：本笔 2N 潜亏 ≤ equity×`max_risk_per_mkt`；**单品种累计**已占风险 ≤ equity×`max_risk_per_market`（4 层金字塔不再突破累计预算）；在途总潜亏 ≤ equity×`max_total_risk_frac`（**在途风险按"当前实际止损位"记账**，见下）；预估保证金占用 ≤ equity×`max_margin_usage`（**按策略 sid 分组**，他策略重仓不占本策略额度）；同组/全组合同时持仓品种数限制（按 `units` 累计）；**共享账户可用资金闸门**（`account_gate_mode` off/warn/veto，基数取交易通道真值源的券商口径 available）。归零才否决 |
| 在途风险口径 | `RiskManager` 的在途敞口 = Σ (层价 − **当前武装止损位**) × 方向 × 手数 × 乘数（净额下限 0），即"全部持仓同时被止损打掉"的真实最坏亏损；早期用"每层 2N"口径会虚高数倍（实盘实测 52.7 万 vs 20.4 万）→ 组合预算被占满、加仓全被 `total_risk(cap=0)` 否决（2026-09-16 修复） |
| 运行时对账 | 实际持仓与引擎层数据连续 `recon_patience` 根 bar 失配 → 告警并按实际持仓重建单层（防部分成交/手工干预脱管）；实际持仓优先取**交易通道真值源** `generated/traders/<id>/rtdata.json`（真值缺失时回退框架信号口径） |
| 成交确认 | 分批窗口闭合判定优先真值源：真值仓未达目标 → 不回写且 `ghost_fill` 告警（执行单元卡死的直接证据）；真值缺失时用 fees 代理兜底并超时放行（`fill_timeout`）。回写价优先级：逐笔成交 `trades.csv`（每层按手数分配，双系统/连续加仓都精确）→ 净均价反推（仅单层未知时可解） |

## VaR 仓位反算

```
每层手数 lots = floor(units × unit_var / (N × volscale))
```

- `unit_var`: 1 个交易单位的 VaR 预算（默认 20 万，**1N 口径**：持仓 1 倍 ATR 不利波动 ≈ 预算亏损）
- `units`: 每品种配置的交易单位数（如 2 → 每层 40 万 VaR）
- `volscale`: 合约乘数，取自框架 `stra_get_comminfo()`（非配置抄写）
- 2N 止损触发时单层实际最大亏损 ≈ 2 × units × unit_var
  （仅在 `stop_n_mode=entry|ratchet` 下严格成立：这两种口径的止损不会因 N 变大而放宽；
  `current` 口径下 N 放大时止损被放宽，实际亏损可超上式）
- 预算不足 1 手 → 放弃该次入场（日志 veto）

## 快速开始

```bash
# 数据预检 (hots键/K线健康/暖机深度; --fix-hots 补键, --repair-journal 回补)
python3 scripts/prepare_data.py

# 新浪主力连续日线拉取 (akshare, 免费兜底源; 幂等合并进 storage/csv)
python3 scripts/fetch_data_sina.py                       # 配置内全部品种
python3 scripts/fetch_data_sina.py --codes SHFE.cu.HOT DCE.m.HOT

# 合约信息每日更新 (CTPLoader 从 CTP 交易前置拉 contracts/commodities.json,
#   当日幂等+过期合约剔除+原子替换+日期备份; 凭据 --cred tdtraders.yaml
#   或 CTP_* 环境变量; deploy run.py 启动链默认自动执行,
#   生产另有 systemd timer wt-turtle-contracts.timer 每日 20:30 兜底)
python3 scripts/update_contracts.py --common ../common --force

# 单品种回测
python runBT.py --code SHFE.rb.HOT --start 201801010900 --end 202304031500

# 全品种矩阵回测 (serial=进程内串行 / subprocess=子进程隔离)
#   产出单品种汇总 + 组合层权益合成 matrix_results/portfolio_equity.csv
#   注意: --capital 同时作为 RiskManager 权益基数 (2026-10-20 起)
python run_matrix.py --mode subprocess --start 201801010900 --end 202304031500

# 组合约束回放 (基于已生成 trades/funds; 统计 max_units/组上限的暴露)
#   这是"事后筛选交易流"的近似, 不是重跑信号; 结果见 portfolio_overlay.json
python research/run_portfolio.py --config config/turtle_config.yaml --bt-dir outputs_bt

# 共享账本组合回测 (真正的组合约束; close/trigger 双模式)
#   12 品种共用 RiskManager, 逐日执行单品种/累计/总敞口/保证金/单位上限
#   默认 --capital 6000 万 = 12×500 万 (旧矩阵口径); 同时输出无约束对照
python research/run_portfolio_bt.py --config config/turtle_config.yaml \
    --capital 60000000 --start 20190101 --end 20260831 --slippage 1 \
    --priority strength
# trigger 模式: 日线武装 + m5 check_bar 触碰仿真; 无 m5 品种自动退化 d1
#   触碰 (实盘 bt_no_simtick 同口径); 产出 portfolio_bt_trigger.csv/json
python research/run_portfolio_bt.py --config config/turtle_config.yaml \
    --capital 60000000 --start 20190101 --end 20260831 --slippage 1 \
    --entry-mode trigger
# h1 小时级组合 (S3, 2026-10-24): m5→h1 聚合, 窗口计数按 h1 bar=更快速
#   度档; 仅本地有 m5 数据的品种参与; --codes 同篮子 d1/h1 对比
python research/run_portfolio_bt.py --config config/turtle_config.yaml \
    --capital 60000000 --start 20190101 --end 20260831 \
    --period h1 --codes rb,IF --tag h1probe
# S4 信号口径 A/B 变体: --no-envelope / --arm-exclude-signal /
#   --no-same-bar-reversal / --no-gap-stop (配 --tag 留档)
python research/run_portfolio_bt.py --config config/turtle_config.yaml \
    --capital 60000000 --priority strength --entry-mode trigger \
    --arm-exclude-signal --tag calib_classic
# A/B 聚合: 读各变体 json → matrix_results/caliber_ab.json 对照表
python research/run_caliber_ab.py
# h1/d1 混合速度证据: 半资金混合 + 日收益相关 + 分散化门槛判定
#   (2026-10-24 实证: 相关≈0 但 h1 独立期望深负 → 门槛未通过)
python research/run_h1_mix.py --codes rb,IF --capital 60000000 \
    --start 20190101 --end 20260831 --entry-mode close --priority strength

# 滑点敏感性矩阵 (0/1/2/3 tick → outputs_bt/sens/sens_report.csv)
python research/run_slippage_sens.py --code SHFE.rb.HOT --slippages 0,1,2,3

# Walk-forward 滚动寻优 (训练窗网格寻优 → 验证窗 OOS 汇总)
python research/run_walkforward.py --code SHFE.rb.HOT \
    --grid '{"stop_atr_mult":[1.5,2.0,2.5],"pyramid_layers":[3,4]}' \
    --start 201901010900 --end 202401010900
# 分析面板: 汇总卡片/参数稳定性/全网格IS热力图/逐窗明细 → wf_results/wf_report.html
python3 research/wf_report.py

# 多速度结构研究 (7 档速度海龟信号的相关/混合/饱和曲线 → speed_study.json)
#   口径: 日PnL=决策前层数×Δclose(含金字塔层数, 2026-10-20 修复);
#   json 自带 meta.caliber —— 2026-09-16 旧文件为固定 1 单位口径
#   (total_R/夏普被低估), 跨版本对比前先核对 meta
python3 research/run_speed_study.py

# TqSdk 数据获取 (需 pip install tqsdk + 快期账户, 凭据走 TQ_USER/TQ_PASS)
python3 scripts/fetch_data_tqsdk.py --periods day min5 --start 20200101
python3 scripts/fetch_data_tqsdk.py --fix-hots   # hots.json 写当前主力映射 (仅空键)

# 实盘 (多品种多实例 + WtMonSvr 监控 + webhook 告警)
# 双进程拓扑: 先起 datakit (行情+K线合成, 凭据填 deploy_tpl/mdparsers.yaml),
# 交易侧 tdparsers.yaml 走 ParserUDP 收行情; 生产部署见 docs/DEPLOY.md
python deploy_tpl/runDt.py &    # 数据底盘 (或经 make_deploy 部署后由 WatchDog 托管)
python runLive.py --port 8080   # 交易引擎 (先填 tdtraders.yaml 交易账户)

# 执行层订单/成交事件旁路侧车 (订单状态机留痕 + localid 级滑点/部成率归因;
#   CTA 层看不到订单回报, 此侧车订阅引擎总线 TRD_ORDER/TRADE 落盘 JSONL)
python3 scripts/exec_journal.py --mq ipc:///tmp/wt_turtle.ipc
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
  entry_mode: trigger     # trigger=盘中武装-触发(生产默认) | close=收盘确认
  stop_n_mode: ratchet    # 止损/加仓 N 口径: ratchet=只收紧(默认) | entry=冻结 | current=当根
  enable_s1_filter: true  # S1 盈利跳过开关(false=裸20/10)
  ghost_auto_trim: true   # ghost_fill 触发后信号层自动裁到与券商一致
  recon_patience: 3       # 对账: 连续失配 N 根 bar 后重建
risk:                     # 组合风险预算 (RiskManager)
  max_risk_per_mkt: 0.02  # 单品种本笔 2N 潜亏/权益上限
  max_risk_per_market: 0.08  # 单品种累计 2N 潜亏/权益上限
  max_total_risk_frac: 0.06
  max_margin_usage: 0.10  # 本策略保证金占用上限 (按 sid)
  margin_rate: 0.12
  max_units_per_group: 2  # 相关性组同时持仓品种数(0=不限)
  max_units_total: 4      # 全组合(0=不限)
  fallback_capital: 15000000  # 回测 --capital 会覆盖
  portfolio_capital: 0   # 资金分配总池 (0=关闭; >0 时按 alloc_weights 分配到品种)
  daily_loss_stop_frac: 0.0    # 日损停新开 (0=关闭)
  daily_loss_flatten_frac: 0.0 # 日损全平 (0=关闭)
  account_gate_mode: warn      # 共享账户可用资金闸 off|warn|veto (真值源口径)
  account_reserve_frac: 0.20   # 可用资金安全垫 (保留比例)
notify:
  enabled: false          # webhook 事件推送 (企业微信/钉钉/generic, 冷却去重)
instruments:
  - code: SHFE.rb.HOT
    period: d1            # d1 | h1 (h1=策略层 m5→h1 聚合, 需 m5 数据)
    system: S1            # S1(20/10+过滤) | S2(55/20)
    units: 2              # 每层 2×20万=40万 VaR
    direction: both       # both | long | short (隐式方向闸)
    group: black          # 相关性组标签
    filters:              # 辅助过滤链 (可省略; 内置类型见 filters/builtins.py)
      - type: AdxTrend
        params: {period: 14, threshold: 25}
      - type: VolatilityBand
        params: {window: 250, min_pct: 0.10, max_pct: 0.90}
```

**品种启用前提**（缺任一项 C++ 回放器会段错误）：
1. `common/hots.json` 含该品种键（空列表=不换月即可，实盘空规则会 segfault —
   `prepare_data.py` 预检会拦截）
2. `storage/` 有该品种日线 K 线；trigger 模式的 m5 盘中止损另需 m5 数据
   （缺失时自动退化为 d1 触碰口径）；**h1 品种以 m5 为原料**（策略层聚合，
   见「小时级 h1 信号」）

## 框架对齐

- `.HOT` 主力连续合约：C++ 核心按 hots.json 自动换月移仓；策略内用 `avgpx` 漂移 re-anchor 各层成本价（换月后止损价连续）
- 图表 GUI：`set_chart_kline` + 主图通道/止损线 + 副图 ATR/持仓直方图 + 进出场 `add_chart_mark` → console 监控与回测 btchart 原生显示
- 持久化：`user_save_data` 引擎快照（方向/层/S1过滤器/fail-safe），重启恢复；孤儿持仓自动单层重建
- 执行：`stra_set_position` 目标仓位语义；live 由 `executers.yaml`（WtMinImpactExeUnit）落单，`actpolicy.yaml` 先平后开
- 组合风控：`env.riskmon`（SimpleRiskMon 回撤熔断）纯配置；通道合规 `tdtraders.yaml` riskmon
- 回测约束：`WtBtEngine` 单策略上下文 → 多品种须 `run_matrix.py` 逐品种回测后汇总

## 小时级 h1 信号（2026-10-21 一期）

WT 数据底盘只从 tick 合成 min1/min5/day、**不写 h1**，且 his DSB 每小时重建对
运行中进程不可见（WtDataReader 缓存无失效 API）。故 h1 走**策略层 m5→h1 聚合**
（`core/aggregate.py`，backtest/live 同源同函数）：主K=m5，`on_calculate` 每 m5
闭合驱动；桶按 **bar 起始钟点** 归并（10:00 的 m5 覆盖 09:55-10:00 → 属 09 点桶），
夜盘按交易日历映射到下一交易日；尾部桶由 `session_end` flush 闭合（与 d1 的 F3
处置同构）。评估/触发/过滤/风控与 d1 完全同路径（`_eval_core` 统一骨架），锚点
`_h1_key`+`_h1_m5_key` 持久化，重启重放不二次消费；bt 逐 m5 推进 + `check_bar`
触碰仿真，与 live tick 驱动时序等价。

- **数据前提**：该品种 m5（`runBT` 预检/runLive 暖机兜）；实时数据走
  `fetch_data_tqsdk --periods min5` 或新浪分钟线回补管道
- **配置**：`period: h1` 即可（`intrabar_stop` 对 h1 无意义，m5 已是驱动主K）
- **参考成绩**（rb 单品种 2019-06~12，5M，trigger，1tick，S1 20/10）：
  h1 +2.95% / 回撤 8.84% / Sharpe 0.23 / 126 笔 / 胜率 42.7%，同窗 d1
  +11.29% / 5.82% / 1.59 —— h1 换手约 4 倍、单笔期望更低，与速度研究的
  "越快夏普越低"一致；组合层价值看 h1/d1 多速度混合（二期：
  `run_portfolio_bt --period h1`）
- **已知边界**：聚合为 O(窗口²) 纯 Python（全窗口约 10 分钟级，可后续做
  增量 feed）；live 部署前需在生产环境验证 rt m5 的 date/time 口径与 bt DSB
  一致（夜盘日历日假设，见 `core/aggregate.py` docstring）

## 组合回测成绩（共享账本，当前口径）

`run_portfolio_bt.py` 让 12 品种共用**一个** `RiskManager`/账本逐日推进，
新开仓/加仓前真正执行单品种本笔、单品种累计、总敞口、保证金、单位上限
（`run_matrix` 每品种独立账户，这些约束从未参与决策，其组合曲线是**无约束
上界**，不可比）。窗口 2019-01~2026-08（1859 交易日），总资金 60M，单边滑点
1 tick，约束 = 6 单位 / 组 2 / 总风险 10% / 保证金 60%：

| 模式 | 约束后 | 无约束对照（同信号同单笔风险） |
|---|---|---|
| close（收盘确认，名额按突破强度） | **+77.07% / 回撤 29.17% / Sharpe 0.414**（1927 笔，否决 382） | +97.35% / 38.19% / 0.446 |
| trigger（盘中武装-触发，名额按 m5 时间 first-come） | **+85.97% / 回撤 38.77% / Sharpe 0.448**（2589 笔，否决 1053） | +152.53% / 38.41% / 0.558 |

口径注意：
- close 模式结果**依赖名额分配规则**（config 顺序 +61.9% vs 突破强度
  +77.1%）；trigger 按时间 first-come，不存在该自由度。
- trigger 本地仅 rb/IF 有 m5 数据，其余 10 品种退化 d1 触碰（实盘
  `bt_no_simtick` 同口径）——上表是混合口径的参考成绩，全 m5 覆盖需另跑。
- trigger 盘中建仓后 mark 浮盈会放大名义保证金：60M/0.6 预算下仍有 7 天
  超限（峰值 47.4M）——实盘保证金要按峰值而非均值预留。
- 15M 总资金跑同一配置会出现 254 天保证金超限（峰值 19.1M）：12 品种
  满配需约 20M 以上保证金，多策略共享账户时必须显式分配。
- 手续费按 `common/fees.json`，当仓平仓适用 `closetoday` 费率（DCE/CZCE
  平今与隔夜不同）；滑点按 pricetick 单边计。

单品种 rb 基线（2018-01~2023-04）见下「与经典海龟的偏离」§1 口径表。

## 与经典海龟的偏离（框架适配必要妥协）

0.5. **close 模式引擎层跳空止损**（S4-3，2026-10-24）：`on_bar` 收到开盘价
   且开盘越过止损位时，**按开盘价成交**（旧口径按止损价，系统性低估跳空亏损；
   组合回测 sim 层 10-20 起已按 min(open,stop) 处理，引擎层补齐后双层同口径）。
   该口径使 close 组合基线诚实下降 11.8pp（旧口径 +88.9% → 诚实 +77.1%，
   见 matrix_results/caliber_ab.json）——"close 诚实地差于 trigger"的量化。
   实盘 StraTurtle 评估路径已传 open 生效；`base.gap_stop: false` 可回退。
0.6. **trigger 武装位通道归属**（S4-1/S4-1b，2026-10-24）：`trigger_envelope`
   在当前管线是**恒等操作**——传入通道窗口已含武装用 bar，max(通道,
   当根极值) 数学上等于通道（A/B 逐位相同已实证）；严格经典语义由
   `arm_exclude_signal`（**默认 True，已启用**）提供：武装位=突破前 N 日
   通道，排除信号 bar。组合 A/B **+53.9pp 收益 / +0.124 Sharpe**（约束与
   无约束同向改善）；黄金基线已按新口径刷新（rb 5M 单品种口径 35.58%→
   33.37%/Sharpe 0.258→0.239——单品种略降但组合截面显著改善，rb 单品种
   成绩不再代表组合口径）。

0. **主力连续日线需每日续接**（`scripts/update_hot_daily.py`，2026-09-16 实盘故障）：
   数据底盘（WtDt）每日只写**真实合约**日线（`his/day/<EXCHG>/<contract>.dsb`），
   而策略代码是主力连续 `XXX.HOT`（读 `XXX_HOT.dsb` / `XXX_HOT_d.csv`）——两边
   没有自动衔接，`HOT` 文件自 2026-09-01 起冻结，策略的 N(ATR20)/20/55 日通道
   一直用陈旧窗口（硬证据：`INE.sc` 的 armed exit=581.8 恰是 HOT 文件
   2026-08-28 的最低价，而干净的最新 10 日窗口最低是 665.4）。该脚本每日按
   `hots.json` 当前主力、**按日期去重**后把增量并入 `storage/csv/<code>_d.csv`，
   再重建 HOT 日线 DSB；建议 20:40 定时执行。

1. **止损/加仓步长的 N 口径可配**（`stop_n_mode`，P0-2 2026-09-15）：
   `ratchet`（默认）= 用当根 N 但**只收紧不放宽**（多头止损只上移、加仓位只上移，
   两次加仓之间单调；加仓时按新单位重锚）；`entry` = 冻结入/加仓时点 N（纯经典口径，
   与风控 2N 记账严格一致）；`current` = 当根 N（2026-09-14 前行为，N 放大时止损被放宽，
   实际止损亏损可达风控记账的 2 倍 — 仅 A/B/回退用）。
   动机：N 会漂移（rb 日线实测持有 20 根 N 比值 p95=1.40 / 极值 2.14），旧口径下
   `max_risk_per_mkt` 记账的 2N 与实际止损亏损脱节，且加仓位会随 N 缩小时下移造成
   "价格未推进也加仓"。HOT 换月只平移价格（`shift_prices`），与 N 口径无关。
   组合矩阵（12 品种 2019-01~2026-08，等权，500 万/品种；**2026-09-15 重测**）：

   | stop_n_mode | 总收益 | 年化 | 最大回撤 | sharpe | calmar |
   |---|---|---|---|---|---|
   | entry（冻结，纯经典） | 49.91% | 5.37% | 29.54% | 0.288 | 0.182 |
   | current（旧口径，会放宽止损） | **56.85%** | **5.98%** | **27.31%** | **0.329** | **0.219** |
   | **ratchet（默认，只收紧）** | 54.89% | 5.81% | 28.48% | 0.316 | 0.204 |

   > ⚠️ **2026-10-20 口径变更**：`runBT/run_matrix` 的 `--capital` 现在
   > 同时覆盖 `risk.fallback_capital`。上表是旧口径（报告 5M / 风控
   > 15M）的历史结果，**不能与当前配置直接对比**；重跑后以新结果为准。
   > 同时旧的 12 品种矩阵未执行 `max_units_total` / 组上限，属于无约束
   > 上界；组合约束用 `run_portfolio_bt.py`（共享账本）或
   > `run_portfolio.py`（事后暴露诊断）评估。

   口径说明：三者在这段样本里相差 ≤7pp，**排序不稳定**（更早一次测量因当时
   代码处于编辑中而失真，已作废）。默认取 `ratchet` 不是因为它收益最高，而是
   **风险口径唯一自洽**：`current` 的止损会随 N 放大被放宽，单层实际亏损可超
   RiskManager 记账的 2N（rb 日线 N 20 根漂移 p95=1.40/极值 2.14 倍），
   `ratchet` 用 ~2pp 的代价换回"记账 2N == 实际止损亏损"。单品种 rb
   （2018-01~2023-04，ratchet）32.92% / 回撤 35.31% / 314 笔。切换只需改
   `base.stop_n_mode`。
2. **信号确认口径可配**（`entry_mode`）：`close`=收盘确认（无未来函数）；`trigger`=盘中突破即触发（经典海龟原生，生产默认）。trigger 模式回测成交价：有 m5 数据时由 m5 模拟 tick 撮合（≈触发价），**无 m5 数据时自动退化**（`bt_no_simtick`）为 check_bar 触发+当根收盘撮合——因 C++ 回放器 simTicks 只用非日K bar 生成模拟 tick（HisDataReplayer.cpp:1222 跳过 KP_DAY），日K 品种无 m5 则 tick 撮合链断裂；实盘 tick 即时成交于触发位附近——回测/实盘仅信号集合一致，执行价差异归入执行层
3. **avgpx 符号约定**：框架空头 avgpx 为负值（avgpx×pos=成本>0），成交价反推与换月检测均已做符号处理
4. **记账价 = 决策价 + 成交后回写**：confirm 时直接按决策价（触发位/收盘价）记账——**不再从框架 avgpx 反推**（`_implied_fill` 2026-09-15 删除：它读的是信号账面，`set_position` 即变而真实成交在下一个 tick 才落地，反推等于"新信号仓 × 旧成交均价"的混合值，加仓场景甚至等于加仓前的旧均价）；成交齐后自动回写真实成交价（逐笔成交优先，净均价兜底，偏差>0.5tick 才动，stop/add 武装位跟随）。该删除对回测**零影响**（rb 32.92%、12 品种矩阵三口径数字逐位不变）
5. **真实持仓/成交改由交易通道真值源判定**（`core/LiveTruth.py`，P0-3 2026-09-15）：
   框架 CTA 侧 `stra_get_position/avgpx/fund_data(3)` **全是信号口径**——`stra_set_position`
   只写信号（`CtaStraBaseCtx.cpp:1354` → `append_signal`），下一个 tick 由 `on_tick`
   消费信号并直接改写策略持仓簿与累计手续费（`:811` → `do_set_position`，费用累加 `:1469`），
   与券商成交回报无关，故"执行单元卡死但信号仓已达标"的幽灵仓在框架层不可见。
   现按交易通道落盘的 `generated/traders/<tdtraders.id>/rtdata.json`（真实持仓+账户资金，
   每根K线收盘与每笔成交后刷新）做对账/成交确认：真值仓未达目标 → 不回写 avgpx 且
   `ghost_fill` 告警；真值缺失时回退 fees 代理（并超时放行，避免免费品种永久阻塞回写）。
   回测环境无该文件 → 自动降级为原行为。**用途分档（`truth_rebuild_mode`）**：
   `alert`（默认）= 只观测告警（券商持仓是账户级、非策略级，共享账户下同一
   合约含其他策略仓位无法归属 — 实测 CZCE.TA 账户 112 vs 本策略 64），
   `rebuild` = 允许按真值重建/收养（仅独占账户）；成交确认两种模式都用
   **下单窗口内账户持仓增量 == 本层手数**判定（对共享账户免疫）。
   **记账价回写同样优先逐笔成交**
   （`generated/traders/<id>/trades.csv`）：按确认顺序分配成交手数，
   每层各自取真实成交均价（净均价方程在多层待回写时欠定，逐笔成交不依赖
   它）。配套：`account_gate_mode`
   （共享账户可用资金闸门，见组合风控行）与 `WT_EXEC_RECOVER=restart`
   （执行单元卡死的 opt-in 自愈重启，诊断路径见 `docs/DATA_OPS.md` §9）。

## 测试

```bash
python3 -m pytest tests/ -q    # 484 passed (48 个测试文件)
ruff check . --config ruff_cta_turtle.toml    # lint
```

CI (`.github/workflows/cta_turtle.yml`)：无 C++ 动态库环境自动注入 wtpy 桩，
纯逻辑层全量执行、运行时级测试（monitor db）自动跳过；pytest-cov 门槛 70%
（core+filters+scripts+research）。

覆盖：ATR/Donchian/ADX 等价性（对照朴素循环）、S1/S2 入场出场边沿、2N 止损、
加仓层数/步长、S1 盈利跳过/fail-safe/sim 单推进、过滤链（方向闸/ADX/波动率带/
流动性/异常放行）、组合风控（单品种/总敞口/保证金/组单位限制/记账生命周期/
子系统份额）、运行时对账（在途容忍/持续失配重建/外部全平）、**盘中止损**（m5 主K
基线/即时离场/信号日期去重/快照v2恢复/v1兼容）、**双系统 S1+S2**（units 均分/
净仓批量执行/部分平仓/共享预算/快照v3）、VaR 反算、配置校验（filters/risk 段）、
数据预检与 hots 修复、组合权益合成/贡献分解、webhook 推送、walk-forward 纯函数、
**合约日更管道**（过期剔除/session回填/幂等/备份/yaml凭据）、换月 re-anchor、
avgpx 符号、on_calculate 全链路、**N 口径**（冻结/当根/棘轮单调、记账 2N == 实际止损
亏损、旧快照回退）、**脏状态守卫**（无层持仓不抛异常、收养空头取 |avgpx|）、
**通道真值源**（解析/前缀汇总/陈旧/缓存 + 对账/幽灵仓/回写三处接入）、
**账户资金闸门**（warn/veto/off、安全垫、无真值降级）、**同 bar 出场结清
S1 过滤器**（两段式第一阶段）、**双系统 re-anchor/账本口径**（两子系统同持、
反向并存、层冻结 N 汇总）、`--weights` 解析校验（fail-fast 防组合静默清零）、
执行单元卡死自愈（重启模式的一天一次闸门）、**逐笔成交记账价**（解析/撤单剔除/
游标增量/手数分配/同码双笔）、**否决日志逐 key 冷却**（累计次数）、
journal turnover 旁路、`--weights` 之外的矩阵串行模式提醒、**陈旧真值闸门**
（过期 rtdata.json 不得触发对账重建/资金闸门；实测污染回测基线 335%→76%）、
**trigger 组合回测**（m5 触碰成交/当仓平今费率/武装时方向闸撤武装/
无 m5 退化 d1 触碰）、**执行层订单状态机与归因**（生命周期跃迁/at-least-once
去重/订单缺失 stub 兜底/买卖方向滑点符号/时间窗与方向不匹配）、
**h1 显式拒绝**、平今费率区分（closetoday/close 回退）。

回测工具链可靠性：`run_matrix` 子进程失败即 SystemExit；`run_walkforward` /
`run_slippage_sens` 先删旧 summary 再跑并检查 rc；`WtBtAnalyst` 失败时
`runBT` 从 `funds.csv` 写兜底 summary（`fallback=True` 显式标记，
`win_rate=0` 不可比）；`run_portfolio_bt` 打印实际评估区间，请求区间被
数据截断时 WARN 而非静默出数。

## 文档索引

| 文档 | 内容 |
|---|---|
| `docs/GUIDE.md` | 使用指南：监控页面/策略业务逻辑/全配置参数/告警清单 |
| `docs/OPS_MANUAL.md` | **策略配置与运维手册**：配置管理 SOP/巡检/告警处置/应急/变更清单 |
| `docs/DATA_OPS.md` | 历史K线运维体系 + 事故档案(§10) + 残余风险与不变量(§11) |
| `docs/DEPLOY.md` | 生产部署手册 (WatchDog + WtMonSvr 双进程拓扑) |
| `docs/STRATEGY_REVIEW_2026-10-20.md` | 策略/工程复核报告 (§9-10 为落地对账) |
| `docs/OPTIMIZATION_PLAN_2026-10-22.md` | 优化方案 (资金分配/拆分/执行层/过滤器 A/B) |

## 文件结构

```
StraTurtle.py            # 策略壳: 生命周期/图表/装配/持久化/对账 (1742 行,
                         #   执行链路已迁出, 见 execution/)
execution/               # 执行层 (A1 拆分, 2026-10-22; 薄转发保接口兼容)
  router.py              # 门控链+单/双系统执行路由+武装裁决
  fill_tracker.py        # 成交确认证据链/均价回写/幽灵trim/执行单元自愈
  truth_monitor.py       # 真值源/运行时对账/共享账户资金闸
  telemetry.py           # 告警落盘/否决日志冷却/fund_live/journal 旁路
  paths.py               # 组合根目录常量
  models.py              # StrategyMode 模式面 (A2: on_init 单点算定)
filters/
  base.py                # SignalFilterBase 协议 + FilterContext + 注册表
  builtins.py            # DirectionGate/AdxTrend/VolatilityBand/LiquidityFloor
core/
  indicators.py          # Wilder ATR/ADX (lfilter) + Donchian (sliding_window_view)
  TurtleEngine.py        # 信号状态机 (纯逻辑, 无框架依赖)
  PositionSizer.py       # VaR 反算
  RiskManager.py         # 组合风险预算闸门 + 在途持仓记账 (支持子系统份额/账户资金闸)
  LiveTruth.py           # 交易通道真值 (rtdata.json 持仓/资金 + trades.csv 逐笔成交)
  BarJournal.py          # K线旁路日志 (宕机窗口兜底, journal→csv 幂等回补)
  ExecJournal.py         # 执行层订单状态机+归因 (纯逻辑, localid 级滑点/部成率)
  notifier.py            # webhook 推送 (企业微信/钉钉/通用, 冷却去重)
  DataChecker.py         # 数据可用性预检 (零崩溃降级)
  portfolio.py           # 组合权益合成/指标/贡献分解 (纯函数)
  ConfigLoader.py        # yaml 加载校验 (base/risk/instruments+filters, 未知键拒绝)
schemas.py               # 配置 dataclass (唯一权威定义)
runBT.py / run_matrix.py / runLive.py    # 核心工作流入口
runMonSvr.py             # 监控服务 (WtMonSvr + 内置 WatchDog, token 鉴权)
research/                # 研究/回测分析工具 (独立可执行, 不入生产路径)
  run_portfolio.py       # 组合约束回放 (基于 trades/funds 的事后诊断)
  run_portfolio_bt.py    # 共享账本组合回测 (close/trigger 双模式, 真组合约束)
  run_walkforward.py     # 滚动窗网格寻优 (wf_results/ + wf_grid.csv)
  run_slippage_sens.py   # 滑点敏感性矩阵 (outputs_bt/sens/)
  run_speed_study.py     # 多速度结构研究 (7 档速度, 含层数口径日PnL)
  wf_report.py           # walk-forward HTML 分析面板
  slippage_report.py     # 滑点敏感性报告
  golden_bt.py           # 黄金回测 (端到端哈希基线防回归, --update 刷新)
  run_filter_ab.py       # 过滤器 A/B (证据驱动启用, 负结果已留档)
  run_caliber_ab.py      # S4 信号口径 A/B 聚合 (caliber_ab.json 对照表)
  run_h1_mix.py          # h1/d1 混合速度证据 (相关+半资金混合+门槛判定)
scripts/
  prepare_data.py        # 数据一键预检管道 (--fix-hots / --repair-journal)
  fetch_data_sina.py     # 新浪主力连续日线拉取 (幂等合并进 storage/csv)
  fetch_data_tqsdk.py    # TqSdk 数据适配器 (分钟/日线, --fix-hots)
  update_contracts.py    # 合约信息每日更新 (CTPLoader 子进程隔离, 过期剔除)
  update_hots.py         # hots.json 主力映射每日更新 (sina 持仓量, tqsdk 兜底)
  update_hot_daily.py    # HOT 日线每日续接 (真实合约→主力连续, 重建后重启交易进程)
  convert_csv_to_dsb.py  # csv → DSB (当日分区过滤, WtDataLoader 绕行方案之一)
  pick_hot_sina.py       # 主力判定纯逻辑 (update_hots 复用)
  timer_notify.py        # 定时器失败告警共享 helper
  exec_journal.py        # 订单/成交事件旁路侧车 (TRD_ORDER/TRADE → JSONL)
  init_monitor_db.py     # 监控库初始化 (双进程调度)
  ctp_loader/            # CTPLoader 配置 (map_future.ini session 覆盖)
deploy_tpl/              # 生产部署模板 (run.py/runDt.py/dtcfg/解析器/systemd units)
configbt.yaml            # 回测引擎配置
config.yaml + tdparsers/tdtraders/executers/actpolicy/filters/logcfg  # 实盘模板
tests/                   # 48 个测试文件 / 484 例
```
