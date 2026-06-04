# WT_INFRA_GUIDE — WonderTrader 基础设施 yaml 字段标注

本文件是 cta_spread_arb 项目根目录 5 份核心 yaml + 2 份相关 yaml 的字段速查表，
用于实盘部署、故障排查和参数调优。

> ⚠️ **凭据安全告警**: `tdtraders.yaml` / `tdparsers.yaml` 当前含 SimNow 测试账号
> 的**明文 user/pass** (broker='9999', user='048363', pass=明文)。SimNow 是公开测试
> 环境，泄露后果可控；**切换实盘前**必须按本文档 §8 改造（推荐 env var 注入或
> 外置 secrets 文件）。

更新时间: 2026-06-04 (R3 落盘)
对应代码版本: cta_spread_arb v9 + R1/R2-C
配套机制: `helpers/preflight.py` 启动期会校验本文档列出的 7 份 yaml 全部存在

---

## 文件清单与作用

| 文件 | 行数 | 职责 | 谁解析 |
|---|---:|---|---|
| `config.yaml` | 36 | WT 引擎根入口，挂接其他 6 份 yaml | C++ `WtCore::WTSCfgLoader` |
| `executers.yaml` | 28 | 执行器策略：报单算法/scale/clear 规则 | `WtExecuter` |
| `tdtraders.yaml` | 23 | 交易通道：CTP front/broker/account/riskmon | `TraderAdapter` |
| `tdparsers.yaml` | 12 | 行情源：CTP front/broker/account | `ParserAdapter` |
| `filters.yaml` | 2 | 代码/策略过滤白名单 (当前空) | `WtFilterMgr` |
| `actpolicy.yaml` | 18 | 开平动作策略：close/closetoday/open 顺序 | `WtActionPolicyMgr` |
| `logcfg.yaml` | 56 | spdlog 多 sink 日志配置 | `WTSLogger` |

---

## 1. config.yaml — 根入口

```yaml
basefiles:                     # 基础数据文件 (preflight 强制存在)
    commodity:  ../common/commodities.json   # 品种定义 (multiplier/pricetick/exchg)
    contract:   ../common/contracts.json     # 合约月份 (CSV 回测需带点 SHFE.rb.2510.dsb)
    holiday:    ../common/holidays.json      # 节假日表 (影响 next_trading_day)
    hot:        ../common/hots.json          # 主力切换映射 (HOT=纯 OI 最大启发)
    session:    ../common/sessions.json      # 交易时段 (TRADING/SHFE_FT/CFFEX_STK ...)

data:
    store:
        module: WtDataStorage                # C++ 存储模块名
        path:   ../storage/                  # k 线/tick/state 数据库根

env:
    name: cta                                # 引擎类型 (cta/hft/uft/sel)
    fees: ../common/fees.json                # 手续费表 (品种粒度)
    filters: filters.yaml                    # 过滤器映射
    product:
        session: TRADING                     # 默认交易时段 ID
    riskmon:                                 # ⚠️ C++ 层组合风控 (与 helpers/RiskMgr.py 不同层)
        active: true
        module: WtRiskMonFact
        name:   SimpleRiskMon                # 内置实现
        base_amount: 5000000                 # 基准资金 (元)
        basic_ratio: 101                     # 触发回撤比 (%, 101=亏 1% 触发)
        calc_span: 5                         # 计算间隔 (秒)
        inner_day_active: true               # 日内回撤开关
        inner_day_fd:    20.0                # 日内回撤阈值 (%)
        multi_day_active: false              # 多日回撤开关
        multi_day_fd:    60.0                # 多日回撤阈值 (%)
        risk_scale: 0.3                      # 触发后仓位缩放系数
        risk_span:  30                       # 风控冷却窗口 (秒)

# 挂接其他 yaml (相对路径相对于本文件)
executers: executers.yaml
parsers:   tdparsers.yaml
traders:   tdtraders.yaml
bspolicy:  actpolicy.yaml

# 套利专用: 推送 risk/event 到 IPC，外部监控可订阅
notifier:
    active: true
    url: ipc:///wtpy/wt_cat_arb.ipc          # nanomsg IPC URL (Linux/WSL2)
                                              # Windows 建议改 tcp://127.0.0.1:port
```

**调优建议**：
- `riskmon.base_amount` 必须匹配账户实际可用资金，否则 `basic_ratio`/`fd` 阈值都失效
- `calc_span=5` 秒在套利策略下可放宽到 10-15 秒（套利持仓变化慢）
- `notifier.url`: 多策略实例并存时按策略名分 IPC 文件，避免事件混淆

---

## 2. executers.yaml — 执行器

```yaml
executers:
-   active: true                  # 关闭后该执行器空跑
    id: exec                      # 唯一 ID，logcfg.yaml 中 %s 替换
    trader: simnow                # 关联 tdtraders.yaml 的 id (必须匹配)
    scale: 1                      # 全局倍率 (策略下 N 手 → 实际 N*scale)
    local: true                   # 本地执行 (false=分布式)
    strict_sync: false            # true=严格仓位同步 (慢但安全)

    policy:
        default:                  # 品种粒度覆盖：写品种 code 例如 SHFE.rb 覆盖 default
            name: WtExeFact.WtMinImpactExeUnit   # 最小冲击算法 (其他: WtSimpleExeUnit/WtTWapExeUnit/WtVwapExeUnit)
            offset: 0             # 报价偏移 tick 数 (限价单)
            expire: 5             # 单笔订单超时 (秒)，超时撤单重报
            pricemode: 1          # 0=对手价 1=最新价 2=自定义
            span: 500             # 拆单间隔 (毫秒)
            byrate: false         # true=按比例拆单 false=按 lots
            lots: 1               # 每笔最小手数
            rate: 0               # 拆单比例 (byrate=true 时生效)

    clear:                        # 收盘清仓策略
        active: false             # ⚠️ 套利策略禁用 (套利持仓需跨日)
        excludes:                 # 排除清仓的品种
        - CFFEX.IF
        - CFFEX.IC
        includes:                 # 强制清仓的品种 (与 excludes 互斥)
        - SHFE.rb
        - DCE.i
```

**关键约束**:
- `policy` 的算法名称必须存在于 WT 编译时链接的 ExeUnit 工厂；`WtMinImpactExeUnit` 是 WtExeFact 内置
- `expire=5` 秒对套利偏短：套利双腿同步要求高，建议 8-15 秒
- 套利场景下 `clear.active=false` 是硬约束，否则跨日开仓被自动平掉
- 多账户分账时新增 executers 数组项，每项关联不同 `trader` id

---

## 3. tdtraders.yaml — 交易通道

> ⚠️ **本节字段都不展示明文 user/pass**，凭据用 `[REDACTED]` 代替。

```yaml
traders:
-   active: true
    id: simnow                    # 唯一 ID，被 executers.yaml 的 trader 字段引用
    module: TraderCTP             # CTP 接口模块 (其他: TraderXTP/TraderCTPOpt/TraderCTPMini)
    savedata: true                # 持久化成交/委托到 storage/
    riskmon:                      # ⚠️ 通道级风控 (与 config.yaml.env.riskmon 不同)
        active: true
        policy:
            default:              # 品种粒度覆盖：default → SHFE.rb 等
                cancel_stat_timespan: 10      # 撤单统计窗口 (秒)
                cancel_times_boundary: 200    # 单合约撤单次数上限 (窗口内)
                cancel_total_limits: 470      # 全账户撤单总次数上限 (单日)
                order_stat_timespan: 10       # 报单统计窗口 (秒)
                order_times_boundary: 20      # 单合约报单次数上限 (窗口内)
    # CTP 接口配置
    front:    tcp://[REDACTED]:30002         # 交易前置 (SimNow 7x24: 182.254.243.31:30002)
    broker:   '[REDACTED]'                   # 经纪商代码 (SimNow='9999')
    appid:    '[REDACTED]'                   # 上期所授权 AppID
    authcode: '[REDACTED]'                   # 上期所授权码
    user:     "[REDACTED]"                   # 资金账号
    pass:     "[REDACTED]"                   # 资金账号密码
    quick:    true                           # 极速模式 (跳过部分握手, CTP-Mini 不支持)
```

**riskmon 限制说明** (CTP 交易所硬限制)：
- 中金所 `cancel_times_boundary` 上限通常 ≤500/窗口
- 上期所/大商所/郑商所 `cancel_total_limits` 当日累计撤单 ≤999 (各家不同)
- `order_times_boundary` 防自成交 + 防恶意刷单，套利建议 ≤30
- **本字段一旦触发，整个账户被风控暂停，必须 manual reset**

**实盘部署清单**:
1. 切换 `front` 为实盘 CTP 前置 (从券商获取)
2. 切换 `broker/user/pass` 为实盘账号
3. `appid/authcode` 必须重新向上期所/券商申请（每账号绑定）
4. 凭据加固方案见 §8

---

## 4. tdparsers.yaml — 行情源

```yaml
parsers:
-   active: true
    id: parser1                   # 唯一 ID
    module: ParserCTP             # CTP 行情模块 (其他: ParserXTP/ParserUDP/ParserShm)
    front:    tcp://[REDACTED]:30011         # 行情前置 (SimNow 7x24: 30011 端口)
    broker:   '[REDACTED]'
    appid:    '[REDACTED]'
    authcode: '[REDACTED]'
    user:     "[REDACTED]"
    pass:     "[REDACTED]"
```

**实盘说明**:
- 行情账号通常与交易账号同账号（同 user/pass）
- 多源行情冗余：新增 parsers 数组项 (`id: parser2` 不同 module/front)，WT 内部自动去重
- 行情前置故障时 `ParserCTP` 自动重连，间隔由 C++ 层硬编码

---

## 5. filters.yaml — 过滤器

```yaml
code_filters:     {}              # 单合约过滤：key=stdCode, val={action, target}
strategy_filters: {}              # 策略级过滤：key=策略 ID, val={action, target}
```

**用途**（当前空）：
- `code_filters`: 临时屏蔽某合约下单 (例如 `SHFE.rb.2510: {action: ignore}`)
- `strategy_filters`: 临时屏蔽某策略整体下单 (例如手动停用 spread_arb)

实盘故障应急快速止损时填本文件最有效（不需要重启 WtEngine，下个 tick 生效）。

---

## 6. actpolicy.yaml — 开平策略

```yaml
default:                          # 默认策略 (所有未匹配 filters 的合约走这套)
    order:
    -   action: close             # 优先平昨
        limit: 0                  # 0=无上限，N=本次最多平 N 手
    -   action: open              # 再开新仓
        limit: 0

lowinnerdayfee:                   # 低日内佣金策略 (上海期货品种平今手续费低于平昨时用)
    filters:
    - DCE.i                       # 应用品种
    order:
    -   action: closetoday        # 先平今 (上期所/能源所专用动作)
        limit: 0
    -   action: closeyestoday     # 再平昨
        limit: 0
    -   action: open              # 最后开新仓
        limit: 0
```

**支持的 action**:
| action | 适用交易所 | 用途 |
|---|---|---|
| `close` | 全部 | 通用平仓（系统自动选今/昨） |
| `closetoday` | 上期所/能源所/INE | 平今仓（手续费低于平昨时优先） |
| `closeyestoday` | 上期所/能源所/INE | 平昨仓 |
| `open` | 全部 | 开新仓 |

**SHFE/INE 套利专属优化**：
- `rb/hc/cu/ni/au` 等 SHFE 品种**强烈建议**配 `lowinnerdayfee`
- 平今免费 vs 平昨 ¥3-10/手（差距随品种）
- 套利日内换月时累计省下手续费可观（按一年 200 调仓日 × 4 腿 × ¥5 = ¥4000+）

**filters 字段语法**：
- 写 `SHFE.rb` 匹配品种粒度
- 写 `SHFE.rb.2510` 匹配单合约
- 多项 list：任一匹配即生效

---

## 7. logcfg.yaml — 日志配置

```yaml
root:                             # 根 logger (引擎/runner 全局日志)
    async: false                  # 异步开关 (高频时建议 true，套利 false 即可)
    level: debug                  # trace/debug/info/warn/err/critical
    sinks:
    -   type: daily_file_sink     # 每日切分
        filename: Logs/Runner.log
        pattern: '[%Y.%m.%d %H:%M:%S - %-5l] %v'   # spdlog 模板
        truncate: true            # 启动时清空 (建议 false 在实盘)
    -   type: console_sink        # stdout
        pattern: '[%m.%d %H:%M:%S - %^%-5l%$] %v'  # %^%$=ANSI 着色

risk:                             # C++ riskmon 模块的独立 logger
    async: false
    level: debug
    sinks:
    -   type: daily_file_sink
        filename: Logs/Riskmon/Riskmon.log
        pattern: '[%Y.%m.%d %H:%M:%S - %-5l] %v'
        truncate: true

dyn_pattern:                      # 动态 logger 模板 (%s 替换为实例 id)
    executer: { ... → Logs/Executer/%s.log }    # %s = executers.yaml.id
    parser:   { ... → Logs/Parser/%s.log }      # %s = tdparsers.yaml.id
    strategy: { ... → Logs/Strategy/%s.log }    # %s = 策略名 (= ArbCore.name)
    trader:   { ... → Logs/Trader/%s.log }      # %s = tdtraders.yaml.id
```

**关键 sink 类型**:
- `daily_file_sink`: 每日切分，文件名加日期
- `rotating_file_sink`: 按大小轮转 (需要 max_size + max_files)
- `console_sink`: stdout 同步输出
- `null_sink`: 丢弃（关闭某 logger）

**实盘建议**:
- `root.truncate: true` 改为 `false`（启动清空日志会丢失上次崩溃现场）
- `level: debug` 改为 `info` 减少 IO（debug 日志单日可达数百 MB）
- 套利策略下 `dyn_pattern.executer.level=warn`，避免每笔 tick 都打 trace

---

## 8. 凭据安全加固 (实盘前必做)

**问题现状** (R2-B preflight 启动期发现):
- `tdtraders.yaml` 和 `tdparsers.yaml` 明文存 `user/pass/authcode/appid`
- 文件直接 commit 到 git 仓库 → **任何 clone 仓库的人都能看到账号密码**
- SimNow 测试账号尚可（公开），实盘账号 = 资金安全事故

**推荐方案对比**:

| 方案 | 实施难度 | 安全性 | 维护性 |
|---|---|---|---|
| A. 环境变量注入 | 低 | 中 (env 仍可见) | 高 |
| B. yaml 占位符 + 启动期替换 | 中 | 高 | 中 |
| C. 外置 secrets.yaml + .gitignore | 低 | 高 | 高 |
| D. HashiCorp Vault / AWS Secrets | 高 | 最高 | 低 |

**方案 C 落地步骤** (推荐):

```bash
# 1. 新建 secrets/ 目录 + .gitignore
mkdir -p secrets
cat > .gitignore <<'EOF'
secrets/
*.secrets.yaml
EOF

# 2. 拆出凭据到 secrets/ctp_secrets.yaml
#    (本步骤手动做，不要让 AI 读取实盘凭据)

# 3. tdtraders.yaml / tdparsers.yaml 改为模板
#    把 user/pass/authcode/appid 改为 ${CTP_USER} 占位

# 4. 启动期由 runLive.py 注入 (在 preflight 之后, engine.init 之前)
```

**runLive.py 注入示例** (R3 不实现，留给用户决策实盘部署方式):

```python
# 启动期注入凭据 (preflight 之后)
import yaml
from pathlib import Path

def inject_credentials(project_root):
    secrets_path = Path(project_root) / 'secrets' / 'ctp_secrets.yaml'
    if not secrets_path.exists():
        return  # 回测/SimNow 跳过
    with open(secrets_path) as f:
        secrets = yaml.safe_load(f)

    # 渲染 tdtraders/tdparsers 模板
    for fn in ['tdtraders.yaml', 'tdparsers.yaml']:
        tpl_path = Path(project_root) / fn
        text = tpl_path.read_text()
        for k, v in secrets.items():
            text = text.replace(f'${{{k}}}', str(v))
        # 写回临时文件供 WT 读取
        out_path = Path(project_root) / f'.runtime_{fn}'
        out_path.write_text(text)
    return True  # 提示 engine.init 改用 .runtime_*.yaml
```

**当前 R3 范围**：仅文档化告警 + 提供方案；**不修改** tdtraders/tdparsers
（避免破坏现有 SimNow 7x24 测试）。实盘切换时由用户拍板方案。

---

## 9. 引用关系图

```
config.yaml ─────────┬─→ basefiles/* (../common/*.json)
                     ├─→ data.store.path (../storage/)
                     ├─→ env.fees (../common/fees.json)
                     ├─→ env.filters → filters.yaml
                     ├─→ env.riskmon (C++ 内嵌组合风控)
                     ├─→ executers → executers.yaml
                     │              └─→ trader id → tdtraders.yaml.id
                     ├─→ parsers → tdparsers.yaml
                     ├─→ traders → tdtraders.yaml
                     ├─→ bspolicy → actpolicy.yaml
                     │              └─→ filters → 品种/合约 stdCode
                     └─→ notifier.url (套利专用 IPC)

logcfg.yaml (独立加载, 不被 config.yaml 引用)
        └─→ dyn_pattern.%s ← executers/parsers/traders/strategy 的 id
```

**preflight 覆盖**：以上所有箭头起点 yaml + `../common/*.json` 全部强制存在性检查。
JSON 文件额外校验合法性 (R2-B `helpers/preflight.py`)。

---

## 10. 故障排查速查

| 症状 | 检查项 | 排查命令 |
|---|---|---|
| 启动崩溃，preflight 通过 | logcfg.yaml 路径错 / sink 类型未编译 | `grep type Logs/Runner.log` |
| 报单不发出 | executers.active / trader id / clear.includes | `grep -E 'active\|trader\|clear' executers.yaml` |
| 行情无数据 | tdparsers.active / front 连通性 | `nc -vz <front_ip> <port>` |
| 撤单被风控 | tdtraders.riskmon.cancel_* 阈值 | 查 `Logs/Trader/simnow.log` |
| 平仓选错今/昨 | actpolicy.yaml 当前品种命中哪个策略 | 查 `Logs/Executer/exec.log` |
| 套利持仓被自动清掉 | executers.clear.active 是否为 false | `grep clear executers.yaml` |
| IPC notifier 无事件 | url 路径 + 订阅端是否启动 | `ls -la /wtpy/wt_cat_arb.ipc` |
| EMERGENCY banner 启动出现 | StateManager.exec_state 中断 (R2-A) | 查 `stra_log_text` 输出 |

---

## 附录 A. preflight 强制存在性矩阵

R2-B 启动期自检列表 (`helpers/preflight.py`)：

| 项 | v9 模式 | legacy 模式 | 校验类型 |
|---|:-:|:-:|---|
| config.yaml | ✓ | ✓ | 文件存在 |
| executers.yaml | ✓ | ✓ | 文件存在 |
| tdtraders.yaml | ✓ | ✓ | 文件存在 |
| tdparsers.yaml | ✓ | ✓ | 文件存在 |
| filters.yaml | ✓ | ✓ | 文件存在 |
| actpolicy.yaml | ✓ | ✓ | 文件存在 |
| logcfg.yaml | ✓ | ✓ | 文件存在 |
| ../common/commodities.json | ✓ | ✓ | JSON 合法 |
| ../common/contracts.json | ✓ | ✓ | JSON 合法 |
| ../common/holidays.json | ✓ | ✓ | JSON 合法 |
| ../common/hots.json | ✓ | ✓ | JSON 合法 |
| ../common/sessions.json | ✓ | ✓ | JSON 合法 (兼容 GBK) |
| ../common/fees.json | ✓ | ✓ | JSON 合法 |
| storage/ | ✓ | ✓ | 目录可写 (自动 mkdir) |
| logs/ | ✓ | ✓ | 目录可写 (自动 mkdir) |
| config/experiments/<name>.yaml | ✓ | ✗ | 文件存在 |
| config/base/signal.yaml | ✓ | ✗ | 文件存在 |
| config/base/risk.yaml | ✓ | ✗ | 文件存在 |
| config/base/execution.yaml | ✓ | ✗ | 文件存在 |
| config/base/regime.yaml | ✓ | ✗ | 文件存在 |
| config/base/factors.yaml | ✓ | ✗ | 文件存在 |
| strategy_config.yaml | ✗ | ✓ | 文件存在 |
| factor_config.yaml | ✗ | ✓ | 文件存在 |

合计：v9 21 项 / legacy 17 项

跳过方式：`python runLive.py --skip-preflight`（不推荐，仅排障）

---

## 附录 B. 相关代码位置

| 关注点 | 文件 | 行 |
|---|---|---|
| WT 启动序列 | runLive.py | 1-100 |
| Preflight 实现 | helpers/preflight.py | 全文 208 行 |
| EMERGENCY banner | strategies/ArbCore.py | on_init 末尾 (155-170) |
| StateManager EMERGENCY payload | helpers/StateManager.py | load_exec_state (103-160) |
| IPC notifier 订阅 (策略侧) | helpers/DiagLogger.py | risk() 调用 |
| C++ riskmon 配置 | config.yaml | env.riskmon |
| 套利策略禁清仓 | executers.yaml | clear.active: false |

---

*文档维护：每次新增 yaml / 字段含义变更 / 凭据方案演进时同步更新本文件*
