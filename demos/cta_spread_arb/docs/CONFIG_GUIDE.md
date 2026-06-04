# 配置体系指南 — V9/V10 二元化配置架构

> 本文档描述 cta_spread_arb 项目的两套配置加载路径，及 V9 分层配置的合并规则。
> 新增实例/合约时请先阅读本文。

## 1. 两套配置路径

### 1.1 V8.1 Legacy 路径（已冻结）

```
  runBT.py --config config/strategy_config.yaml --factor-config config/factor_config.yaml
                                   ↓
                      SpreadArbStrategy.__init__()
                                   ↓
                        ConfigManager(strategy_config_path)
                                   ↓
               从 strategy_config.yaml 解析 strategies[] / signal /
               risk_control / regime / execution / param_profiles
```

适用场景：已有 `strategy_config.yaml` + `factor_config.yaml` 的老工作流。
**特性冻结** — 只为兼容不回退，新功能不在 Legacy 路径上加入。

---

### 1.2 V9/V10 分层路径（推荐）

```
  runBT.py --experiment default
                                   ↓
              SpreadArbStrategy.from_experiment(experiment='default')
                                   ↓
                     ConfigResolver(config_root).resolve('default')
                                   ↓
              ┌── Layer 4: experiments/default.yaml ──────────┐
              │  instances: {contract, contract_month,         │
              │             periods, signal_mode, auto_profile} │
              │  + backtest{} + auto_profile{}                  │
              └────────────────────────────────────────────────┘
                          ↓ (deep merge)
              ┌── Layer 3: profiles/trending_contract.yaml ──┐
              │  overrides: {signal: {entry_threshold, ...}}  │
              └────────────────────────────────────────────────┘
                          ↓ (deep merge)
              ┌── Layer 2: contracts/rb_hc.yaml ──────────────┐
              │  legs_template → 渲染为具体 legs[]            │
              │  + spread_mode, type                           │
              └────────────────────────────────────────────────┘
                          ↓ (deep merge)
              ┌── Layer 1: base/*.yaml ───────────────────────┐
              │  signal.yaml / risk.yaml / regime.yaml /       │
              │  execution.yaml / factors.yaml                  │
              └────────────────────────────────────────────────┘
                                   ↓
                         ResolvedConfig (接口≈ConfigManager)
                                   ↓
                            ArbCore(...)
```

适用场景：所有新实例/回测/实盘。

---

## 2. 目录结构

```
config/
├── base/                       # Layer 1 — 全局默认（必须）
│   ├── signal.yaml             - 信号参数 baseline
│   ├── risk.yaml               - 风控参数 baseline
│   ├── regime.yaml             - 市态检测参数 baseline
│   ├── execution.yaml          - 执行参数 baseline
│   └── factors.yaml            - 因子开关+weight
├── contracts/                  # Layer 2 — 合约对模板（必须）
│   └── rb_hc.yaml              - rb/hc 跨品种 legs_template
├── profiles/                   # Layer 3 — 参数覆盖包（可选）
│   ├── trending_contract.yaml  - 趋势单边: 高 entry_threshold
│   └── mr_contract.yaml        - 均值回归: 低 entry_threshold
├── experiments/                # Layer 4 — 实验/实例定义（必须）
│   ├── default.yaml            - rb/hc 2510 auto_profile
│   └── rb_hc_2601.yaml         - rb/hc 2601 auto_profile
│
├── strategy_config.yaml        # [Legacy] v8.1 单文件配置(runLive 仍引用)
└── factor_config.yaml          # [Legacy] v8.1 因子配置(runLive 仍引用)
# 注: *.legacy.yaml (v8 备份) 已于 2026-06-04 P2-clean 退役至 _trash/p2_clean_20260604/
```

### 2.1 base/ — 全局默认（Layer 1）

每个 `.yaml` 文件对应一个顶层 key，结构与 `strategy_config.yaml` 同名节完全一致：

| 文件 | Key | 用途 |
|---|---|---|
| `signal.yaml` | `signal` | 开平仓阈值、trailing 参数 |
| `risk.yaml` | `risk_control` | 持仓时长、最大亏损、止盈 |
| `regime.yaml` | `regime` | Hurst/ADX 窗口、权重 |
| `execution.yaml` | `execution` | Bollinger 参数、leg_timeout |
| `factors.yaml` | (顶层) | 启用的因子列表及 weight |

### 2.2 contracts/ — 合约模板（Layer 2）

描述一对品种的对冲结构和价差模式，**不含具体合约月份**。

```yaml
# contracts/rb_hc.yaml
product_pair: rb_hc
type: cross_commodity
spread_mode: coint

legs_template:
  - symbol: rb
    exchange: SHFE
    stdCode_template: "SHFE.rb.{month}"    # {month} 由 instance.contract_month 填充
    ratio: 1
    lots: 1
    multiplier: 10
  - symbol: hc
    exchange: SHFE
    stdCode_template: "SHFE.hc.{month}"
    ratio: -1
    lots: 1
    multiplier: 10
```

新增合约对 → 新建 `contracts/<product_pair>.yaml`。

### 2.3 profiles/ — 参数覆盖包（Layer 3）

按 `strategy_config.yaml` 的 `param_profiles` 节语义拆出的独立文件。

```yaml
# profiles/trending_contract.yaml
name: trending_contract
description: "趋势单边市场: 提高入场门槛, 偏向顺势 SHORT_SPREAD"
overrides:
  signal:
    entry_threshold: 0.60        # 覆盖 base/signal.yaml 的 0.45
    short_entry_multiplier: 0.7  # 覆盖 1.0 → 0.7
```

覆盖规则（deep merge）：
- dict 递归合并
- 标量 / list 整体替换
- `None` 值跳过（保留 base）

### 2.4 experiments/ — 实例定义（Layer 4）

定义一次回测/实盘的实例装配关系。

```yaml
# experiments/default.yaml
name: default
description: "rb/hc 跨品种套利 - 2510 合约 auto profile"

backtest:
  start: 202501200900
  end: 202510151500
  capital: 1000000
  slippage: 0

auto_profile:
  enable: true
  trend_hurst: 0.55
  mr_hurst: 0.45
  trend_r2: 0.55
  lookback_days: 120

instances:
  - name: rb_hc_2510_m5
    contract: rb_hc                 # → contracts/rb_hc.yaml
    contract_month: "2510"          # → 渲染 legs: SHFE.rb.2510 + SHFE.hc.2510
    periods:
      main_period: m5
      main_bar_cnt: 200
      aux_periods:
        - period: d1
          bar_cnt: 15
    signal_mode: adaptive
    auto_profile: true              # → runtime 按 Hurst 选 profile
```

一个 `experiments/*.yaml` 对应 **1 个实例**（v10 单 pipeline 约束）。

---

## 3. 覆盖优先级（从高到低）

| 优先级 | 来源 | 覆盖范围 |
|---|---|---|
| 最高 | `experiments/*.yaml.execution` | 执行参数（如 router on/off） |
| ↑ | `profiles/*.yaml.overrides.signal` | 信号参数（auto_profile 选中） |
| ↑ | `experiments/*.yaml.instances[].*` | instance 字段（name/periods/signal_mode） |
| ↑ | `contracts/*.yaml` | legs_template / spread_mode / type |
| 最低 | `base/*.yaml` | 全局默认 |

**规则**：
1. 仅 deep merge 到 `base/` 层
2. `profiles/` 的 overrides 只影响**运行时 signal 参数**，不影响 risk/regime/execution
3. `experiments/*.yaml` 的 auto_profile 段是**全局开关**，各 instance 的 auto_profile 是**独立开关**

---

## 4. 如何新增一个品种

### 场景 1：已有合约对 + 新合约月份（如 rb/hc 2605）

```yaml
# config/experiments/rb_hc_2605.yaml
name: rb_hc_2605
instances:
  - name: rb_hc_2605_m5
    contract: rb_hc             # 复用 contracts/rb_hc.yaml
    contract_month: "2605"
    ...rest defaults...
```

运行：
```
python runBT.py --experiment rb_hc_2605
```

### 场景 2：全新合约对（如 cs/sf 双硅套利）

1. 新建 `config/contracts/cs_sf.yaml` — 定义 legs_template
2. 新建 `config/profiles/cs_sf_trending.yaml`（可选）
3. 新建 `config/experiments/cs_sf_2510.yaml`
4. `python runBT.py --experiment cs_sf_2510`

---

## 5. 如何新增一个 profile

1. 分析历史价差 Husrt / ADX，确定趋势或均值回归特征
2. 新建 `config/profiles/<name>.yaml`，参照 `trending_contract.yaml` 格式
3. 在 instance 中设置 `param_profile: <name>`（强制）或调整 auto_profile 阈值使其自动选中

---

## 6. map: 新旧配置字段对照

| v8.1 字段 | v9 位置 | 备注 |
|---|---|---|
| `strategies[].legs[].code` | `contracts/` + instance.contract_month | 模板渲染 |
| `strategies[].legs[].lots` | `contracts/` legs_template | 模板继承 |
| `strategies[].periods` | `experiments/` instance.periods | 直移 |
| `strategies[].signal_mode` | `experiments/` instance.signal_mode | 直移 |
| `signal.*` | `base/signal.yaml` | 全局 |
| `risk_control.*` | `base/risk.yaml` | 全局 |
| `regime.*` | `base/regime.yaml` | 全局 |
| `execution.*` | `base/execution.yaml` | 全局 |
| `param_profiles.*` | `profiles/*.yaml` | 拆文件 |
| `param_profile_auto.*` | `experiments/` auto_profile{} | 实验级 |
| `factors[].enabled` | `base/factors.yaml` | 直移 |
| `signal_aggregation.*` | `base/factors.yaml` | 直移 |

---

## 7. 测试与验证

新配置引入后验证手段：

```
# 1. 解析检查（不跑回测）
python -c "
from helpers.ConfigResolver import resolve_experiment
rc = resolve_experiment('./config', 'default')
print(f'strategies: {len(rc.strategies)}')
print(f'signal entry_threshold: {rc.signal_cfg.entry_threshold}')
print(f'legs: {rc.strategies[0][\"legs\"]}')
"

# 2. 基线回测
python runBT.py --experiment default

# 3. 基线验证
pytest tests/test_p1_9_baseline_metrics.py -v
```

---

## 8. 已知问题 / TODO

- `runLive.py` 仍走 Legacy 路径，未接入 V9 分层配置（`runBT.py` 已支持）
- Legacy 主文件（`strategy_config.yaml` / `factor_config.yaml`）仍服务于 runLive，
  待 runLive 迁移到 ConfigResolver 后一并清理
- 2026-06-04 P2-clean: 已退役 *.legacy.yaml / grid_search / optimize_v6 /
  diagnose_* / GridAggregator / GridGenerator / migrate_v8_to_v9 等 13 项历史脚本