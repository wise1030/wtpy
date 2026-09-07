# cta_turtle — 海龟趋势跟踪策略 (wtpy CTA)

基于 WonderTrader/wtpy CTA 框架的经典海龟交易系统，商品期货主力合约趋势跟踪。
支持日K/小时K 双周期（每品种独立配置）、VaR 波动率仓位反算、**可插拔信号过滤器**
（方向闸/ADX/波动率带/流动性下限）、**组合级风险预算**（单品种+总敞口+保证金+
相关性组单位限制）、**双系统 S1+S2 并行**（各半 units 共享品种预算）、经典加仓金字塔、
m5 盘中止损与 walk-forward 滚动寻优。

## 策略逻辑

| 组件 | 规则 |
|---|---|
| N | ATR(20) Wilder 平滑，交易周期上计算 |
| S1 入场 | 突破 20 周期高/低点：`entry_mode: close`=收盘确认（边沿触发）；`trigger`=盘中触及即入场（经典海龟原生，生产默认） |
| S1 出场 | 反向 10 周期突破 或 2N 止损 |
| S1 过滤 | 上次 S1 突破盈利 → 跳过本次；被跳过则武装 fail-safe：价格继续突破 55 周期强制入场；被跳过的模拟单 2N 止损后过滤器解除（`enable_s1_filter` 可关闭） |
| S2 | 55 周期突破入场 / 20 周期出场，无过滤 |
| 双系统 | `system: "S1+S2"` 两子系统并行，各分 ceil/floor 半量 units，独立通道/引擎状态，框架净仓=两者之和，品种内风险/保证金预算按 units 占比切分 |
| 加仓 | 每盈利 0.5N 加一层（可配 `pyramid_step_atr`），上限 `pyramid_layers`（1=不加仓），加仓后整体止损上移至最后加仓价 ∓ 2N |
| 止损 | 2N 硬止损，bar 内 low/high 触碰判定，按止损价离场；`intrabar_stop: true` 时主K切换为分钟线（默认 m5）**盘中即时离场**；`entry_mode: trigger` 时止损/加仓/通道出场全部由 tick 武装位盘中触发 |
| 触发执行 | `entry_mode: trigger`（方案C）：日评估仅武装价位（含当根包络通道），tick 穿越即触发；**过滤器于武装时（收盘）前置裁决，风控于触发时复核当前权益**；否决→当日撤武装防刷屏；实盘日线不闭合（F3）由 session_end 用当日 m5 聚合合成日K评估；回测走 check_bar 触碰仿真（跳空按开盘价/触碰按武装位，填充价=信号bar收盘，与 close 模式同填充模型——纯信号语义 A/B） |
| 过滤链 | 引擎入场决策后、下单前依次判定：隐式方向闸(`direction`) + `filters:` 显式配置链，任一否决即放弃（不触碰引擎状态） |
| 组合风控 | 下单前最后闸门：本笔 2N 潜亏 ≤ equity×`max_risk_per_mkt`；在途总潜亏 ≤ equity×`max_total_risk_frac`；预估保证金占用上限；同组/全组合同时持仓品种数限制。缩量优先，归零才否决 |
| 运行时对账 | 实际持仓与引擎层数据连续 `recon_patience` 根 bar 失配 → 告警并按实际持仓重建单层（防部分成交/手工干预脱管） |

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
python run_matrix.py --mode subprocess --start 201801010900 --end 202304031500

# 滑点敏感性矩阵 (0/1/2/3 tick → outputs_bt/sens/sens_report.csv)
python run_slippage_sens.py --code SHFE.rb.HOT --slippages 0,1,2,3

# Walk-forward 滚动寻优 (训练窗网格寻优 → 验证窗 OOS 汇总)
python run_walkforward.py --code SHFE.rb.HOT \
    --grid '{"stop_atr_mult":[1.5,2.0,2.5],"pyramid_layers":[3,4]}' \
    --start 201901010900 --end 202401010900
# 分析面板: 汇总卡片/参数稳定性/全网格IS热力图/逐窗明细 → wf_results/wf_report.html
python3 scripts/wf_report.py

# TqSdk 数据获取 (需 pip install tqsdk + 快期账户, 凭据走 TQ_USER/TQ_PASS)
python3 scripts/fetch_data_tqsdk.py --periods day min5 --start 20200101
python3 scripts/fetch_data_tqsdk.py --fix-hots   # hots.json 写当前主力映射 (仅空键)

# 实盘 (多品种多实例 + WtMonSvr 监控 + webhook 告警)
# 双进程拓扑: 先起 datakit (行情+K线合成, 凭据填 deploy_tpl/mdparsers.yaml),
# 交易侧 tdparsers.yaml 走 ParserUDP 收行情; 生产部署见 docs/DEPLOY.md
python deploy_tpl/runDt.py &    # 数据底盘 (或经 make_deploy 部署后由 WatchDog 托管)
python runLive.py --port 8080   # 交易引擎 (先填 tdtraders.yaml 交易账户)
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
  enable_s1_filter: true  # S1 盈利跳过开关(false=裸20/10)
  recon_patience: 3       # 对账: 连续失配 N 根 bar 后重建
risk:                     # 组合风险预算 (RiskManager)
  max_risk_per_mkt: 0.02  # 单品种本笔 2N 潜亏/权益上限
  max_total_risk_frac: 0.06
  max_margin_usage: 0.60  # 预估保证金占用上限
  margin_rate: 0.12
  max_units_per_group: 2  # 相关性组同时持仓品种数(0=不限)
  max_units_total: 4      # 全组合(0=不限)
instruments:
  - code: SHFE.rb.HOT
    period: d1            # d1 | h1 (每品种独立)
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
2. **信号确认口径可配**（`entry_mode`）：`close`=收盘确认（无未来函数）；`trigger`=盘中突破即触发（经典海龟原生，生产默认）。trigger 模式回测成交价：有 m5 数据时由 m5 模拟 tick 撮合（≈触发价），**无 m5 数据时自动退化**（`bt_no_simtick`）为 check_bar 触发+当根收盘撮合——因 C++ 回放器 simTicks 只用非日K bar 生成模拟 tick（HisDataReplayer.cpp:1222 跳过 KP_DAY），日K 品种无 m5 则 tick 撮合链断裂；实盘 tick 即时成交于触发位附近——回测/实盘仅信号集合一致，执行价差异归入执行层
3. **avgpx 符号约定**：框架空头 avgpx 为负值（avgpx×pos=成本>0），成交价反推与换月检测均已做符号处理
4. **记账价 = 实际成交均价回写**：confirm 时若分批窗口未闭合先按触发价记账，成交齐后自动从 avgpx 增量反推修正最后一层（stop/add 武装位跟随，偏差>0.5tick 才动）；bt 撮合同为延迟成交，回写使记账价=收盘价口径一致（rb 基线因此从 32.8% 漂移为 26.9%，属口径修正）

## 测试

```bash
python3 -m pytest tests/ -q    # 240 passed
ruff check . --config ruff_cta_turtle.toml    # lint
```

CI (`.github/workflows/cta_turtle.yml`)：无 C++ 动态库环境自动注入 wtpy 桩，
纯逻辑层全量执行、运行时级测试（monitor db）自动跳过。

覆盖：ATR/Donchian/ADX 等价性（对照朴素循环）、S1/S2 入场出场边沿、2N 止损、
加仓层数/步长、S1 盈利跳过/fail-safe/sim 单推进、过滤链（方向闸/ADX/波动率带/
流动性/异常放行）、组合风控（单品种/总敞口/保证金/组单位限制/记账生命周期/
子系统份额）、运行时对账（在途容忍/持续失配重建/外部全平）、**盘中止损**（m5 主K
基线/即时离场/信号日期去重/快照v2恢复/v1兼容）、**双系统 S1+S2**（units 均分/
净仓批量执行/部分平仓/共享预算/快照v3）、VaR 反算、配置校验（filters/risk 段）、
数据预检与 hots 修复、组合权益合成/贡献分解、webhook 推送、walk-forward 纯函数、
**合约日更管道**（过期剔除/session回填/幂等/备份/yaml凭据）、换月 re-anchor、
avgpx 符号、on_calculate 全链路。

## 文件结构

```
StraTurtle.py            # 策略壳: 生命周期/图表/执行/持久化/对账/盘中止损 (框架对接层)
filters/
  base.py                # SignalFilterBase 协议 + FilterContext + 注册表
  builtins.py            # DirectionGate/AdxTrend/VolatilityBand/LiquidityFloor
docs/
  DEPLOY.md              # 生产部署手册 (WatchDog + WtMonSvr)
  DATA_OPS.md            # 历史K线运维: 初始化加载/宕机重启/完整性保障
helpers/
  indicators.py          # Wilder ATR/ADX (lfilter) + Donchian (sliding_window_view)
  TurtleEngine.py        # 信号状态机 (纯逻辑, 无框架依赖)
  PositionSizer.py       # VaR 反算
  RiskManager.py         # 组合风险预算闸门 + 在途持仓记账 (支持子系统份额)
  BarJournal.py          # K线旁路日志 (宕机窗口兜底, journal→csv 幂等回补)
  notifier.py            # webhook 推送 (企业微信/钉钉/通用, 冷却去重)
  DataChecker.py         # 数据可用性预检 (零崩溃降级)
  portfolio.py           # 组合权益合成/指标/贡献分解 (纯函数)
  ConfigLoader.py        # yaml 加载校验 (base/risk/instruments+filters)
schemas.py               # 配置 dataclass (含 RiskConfig/FilterConfig/NotifyConfig)
runBT.py / run_matrix.py / runLive.py / run_slippage_sens.py / run_walkforward.py
scripts/prepare_data.py     # 数据一键预检管道 (--fix-hots)
scripts/fetch_data_tqsdk.py # TqSdk 数据适配器 (K线→storage/csv; --fix-hots 写真实主力码)
scripts/wf_report.py        # walk-forward HTML 面板 (汇总/参数稳定性/热力图/窗明细)
configbt.yaml            # 回测引擎配置
config.yaml + tdparsers/tdtraders/executers/actpolicy/filters/logcfg  # 实盘模板
tests/                   # 220 个单测
```
