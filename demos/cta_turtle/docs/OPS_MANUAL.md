# cta_turtle 策略配置与运维手册

> 适用：2026-09 生产部署（129.211.5.54，12 品种 d1 / trigger 模式 / datakit 双进程拓扑）。
> 配套文档：`README.md`（策略全貌）· `GUIDE.md`（监控页面+业务逻辑+参数细节）·
> `DATA_OPS.md`（数据运维+事故档案+残余风险）· `DEPLOY.md`（部署手册）。
> 本文档聚焦两件其他人没系统写的事：**配置怎么管** 与 **出事了怎么办**。

---

## 0. 一页总览

```
                 ┌────────────────────────────────────────────┐
   CTP 行情 ──▶ │ datakit (runDt.py)                         │   wt-turtle-contracts.timer 20:30
   (mdparsers)  │  ParserCTP → K线合成 → UDP 3997/9001 广播    │   wt-turtle-hots.timer     20:35
                 └───────┬───────────────────────┬────────────┘   wt-turtle-hotdaily.timer 20:40
                         │ UDP                    │ m5/d1 DSB
                 ┌───────▼──────────┐   ┌─────────▼─────────────┐
   CTP 交易 ──▶ │ run.py (交易进程)  │   │ storage/ (his/rt/csv)  │
   (tdparsers)  │  12 × StraTurtle   │   │ common/ (hots/fees/    │
                 │  + TraderAdapter   │   │   contracts/commod.)   │
                 │  + 执行单元        │   └────────────────────────┘
                 └───┬───────┬────────┘
                     │       │
        generated/ (userdata快照/stradata/traders真值+trades.csv/outputs)
                     │
        WtMonSvr (runMonSvr.py :8081, 内嵌 WatchDog span=10s)
                     │
        systemd: wt-turtle-monitor.service (+ 3 个 timer)
```

三条守护闭环：
1. **数据闭环**：contracts→hots→hotdaily 三个 timer 每晚 20:30/20:35/20:40 串行续接，
   hotdaily 重建 DSB 后自动重启交易进程（reader 缓存无失效 API，重启是唯一手段）。
2. **状态闭环**：策略每次触发/日评估落快照（`generated/userdata/`），任何重启从快照恢复；
   孤儿 pending 自动清除，武装位保留可"补票"。
3. **资金/成交闭环**：真值源 `rtdata.json`（券商口径）驱动成交确认/对账/资金闸；
   真值链死亡由每日重启复活（9/17~9/22 实测机制）。

---

## 1. 配置管理

### 1.1 配置文件清单

| 文件 | 位置 | 内容 | 生效方式 |
|---|---|---|---|
| `turtle_config.yaml` | `<deploy>/config/` | 策略参数（base/risk/instruments/notify） | **交易进程重启**生效 |
| `config.yaml` | `<deploy>/` | 引擎配置：traders/executers/actpolicy/**riskmon**/数据目录 | 重启生效 |
| `dtcfg.yaml` | `<deploy>/` | datakit：K线合成/UDP广播/落盘 | 仅 datakit 重启 |
| `tdparsers.yaml` / `mdparsers.yaml` | `<deploy>/` | 交易/行情 CTP 前置与凭据 | 各进程重启生效 |
| `executers.yaml` / `actpolicy.yaml` | `<deploy>/` | 执行单元参数 / 开平规则 | 重启生效 |
| `fees.json` / `commodities.json` / `contracts.json` / `hots.json` | `<deploy>/common/` | 费率/品种/合约/主力映射 | fees 重启生效；contracts/hots 由 timer 更新（hots 变更需重启才生效，脚本自带） |
| `storage/` | `<deploy>/../` | K线数据（his DSB / rt / csv） | 实时写入；hotdaily 重建 HOT DSB |
| systemd units | `/etc/systemd/system/wt-turtle-*` | 托管/定时器（模板 `deploy_tpl/systemd/`） | `systemctl daemon-reload` |
| 环境变量 | systemd drop-in / WatchDog env | `WT_EXEC_RECOVER` `WT_HOTDAILY_RESTART` `WT_CONTRACT_SYNC` `WT_PREFLIGHT` `WT_AUTO_REPAIR` `WT_MON_TOKEN` `TQ_USER/TQ_PASS` | 进程级，改后需重启对应进程 |

### 1.2 配置权威源与校验

- **唯一权威 = `schemas.py`**：所有配置字段先定义 dataclass 再在 `ConfigLoader.load`
  校验，**未知字段直接报错**（顶层段与 instruments 项均拒绝未知键，typo 不会静默回默认）。
- 加载时打印敏感行为口径（`entry_mode/stop_n_mode/ghost_auto_trim/account_gate/notify`），
  启动日志里可核对"加载的到底是哪版配置"。
- 本地改完必须过：`python3 -m pytest tests/ -q` + `ruff check . --config ruff_cta_turtle.toml`。

### 1.3 修改流程 SOP

```
本地改 config → pytest+ruff → python3 make_deploy.py --verify <远端目录> (改后校验)
→ scp 单文件到远端对应路径（⚠️ 绝不用 rsync --delete，见 DATA_OPS §10.1）
→ md5 比对 → 按 §1.1"生效方式"重启对应进程
→ 看 10 分钟日志确认无 Traceback + 策略 restored 正常
```

纪律（全部来自真实事故）：
- **部署前先备份** `generated/`（`tar czf ~/wt_turtle_backup_$(date +%Y%m%d_%H%M).tgz <deploy>/generated <deploy>/common <deploy>/config`）。
- 改 `turtle_config.yaml` 的 units/通道窗口 → **回测基线可能漂移**，属"回测口径变更"，
  按 AGENTS 约定同步更新 README「与经典海龟的偏离」小节的数据。
- 手工修状态必须同时改 `generated/stradata` 的 **positions 与 signals 两段**
  （残留信号会被重启消费导致计划外加仓，9/10 实测 MA +4 手）。

### 1.4 参数速查（生产值 vs 默认）

> 完整字段语义见 `GUIDE.md` 第三部分；此处只列"生产改了什么、为什么"。

| 参数 | 默认 | 生产 | 依据 |
|---|---|---|---|
| `base.entry_mode` | close | **trigger** | 经典海龟原生盘中触发；F3 缺口由 session_end m5 合成 + catchup 消解 |
| `base.stop_n_mode` | ratchet | ratchet | 风险口径唯一自洽（current 会放宽止损，entry 收益略低）；矩阵见 README |
| `base.truth_max_age` | 1800 | **86400** | d1 主K不闭合时无成交 rtdata 长时间不刷新但**内容仍准**；24h 用于区分"无成交"与"链路死" |
| `base.truth_rebuild_mode` | alert | alert | 券商持仓是账户级，共享账户无法归属（实测 TA 112 vs 64），禁止按真值重建 |
| `base.ghost_auto_trim` | true | true | CF 34 手幽灵仓事故后默认自动裁剪 |
| `risk.max_total_risk_frac` | 0.06 | **0.10** | 12 品种组合预算；按"当前实际止损位"记账后 2N 虚高问题已修，10% 仍在紧平衡（p 加仓常被缩到个位数） |
| `risk.max_units_total` | 4 | **6** | 12 品种 6 组，组内 2 |
| `risk.account_gate_mode` | warn | warn（未写入 yaml=默认） | 共享账户资金协议未最终定案；升级 veto 前回看日志无误杀 |
| `notify.enabled` | false | **false（待配）** | P0-2：配 webhook 后启用，所有 S 级检测才有声 |
| 12 × `units` | — | 1 | 每层 20 万 VaR；units=1 且 pyramid_layers=4 → **会加仓**（非 1 才不加） |

---

## 2. 日常运维

### 2.1 巡检清单

**盘前（08:45 前，5 分钟）**：
```bash
ssh ubuntu@129.211.5.54 '
D=/home/ubuntu/projects/cta_turtle/deploy/cta_turtle
systemctl is-active wt-turtle-monitor cta_turtle_dt cta_turtle   # 3 进程活着
pgrep -fc "run.py$"                                              # =12 策略
tail -5 $D/Logs/alerts.log                                       # 隔夜无 S 级事件
stat -c "%y" $D/generated/traders/simnow/rtdata.json             # 真值新鲜（隔夜无成交会停在昨收, 看日期即可）
journalctl -u wt-turtle-hotdaily --since today 2>/dev/null | grep -c "已重启"  # 1=昨晚链完整
'
```
**盘后（15:30，10 分钟）**：
- 监控页面核对：12 策略持仓 vs 引擎层数一致、`session end` 日志 12 条（有持仓的打）、
  当日成交与 `trades.csv` 一致。
- `journalctl -u wt-turtle-contracts/hots/hotdaily --since today` 三个 timer 全 `Finished`。
-  alerts.log 当日新增事件逐条过一遍（见 §3 处置表）。

**每周**：磁盘 `df -h`（Logs 增长 ~5M/日）、`git log` 对齐本地与远端代码版本、
核对 `hots_roll` 换月事件与 re-anchor 日志是否跟上。

### 2.2 关键日志位置

| 日志 | 位置 | 内容 |
|---|---|---|
| 策略运行 | `<deploy>/Logs/Runner_*.log` | 触发/风控/对账/恢复（按日切分） |
| 交易回报 | `<deploy>/Logs/Trader/simnow_*.log` | 委托/成交/资金 |
| 告警留痕 | `<deploy>/Logs/alerts.log` | `_alert` 无条件落盘（webhook 未配时的最后防线） |
| 定时器 | `journalctl -u wt-turtle-*` | contracts/hots/hotdaily 输出 |
| 监控 | `journalctl -u wt-turtle-monitor` | WebSocket/REST 异常 |
| 资金旁路 | `generated/outputs/*/fund_live.csv` | 盘中资金（框架 fund.csv 仅 session_end 写） |
| 逐笔成交 | `generated/traders/simnow/trades.csv` | 真实成交（记账价回写依据） |

### 2.3 备份

现状：**手动**（最后一次 2026-09-20）——风险项，建议补自动化（每日 20:45，
晚于 hotdaily、早于夜盘）：
```bash
tar czf ~/bak/wt_turtle_$(date +%Y%m%d_%H%M).tgz \
    --exclude=Logs <deploy>/generated <deploy>/common <deploy>/config
# 保留 14 天滚动; 关键时点(换月/大改)另存异地
```
恢复要点（DATA_OPS §10.1）：备份→修状态→杀进程三步必须在一个会话内完成
（WatchDog ~12s 拉起会覆盖手工修改后的文件）。

### 2.4 容量与性能基线（2026-09 实测）

| 项 | 基线 | 备注 |
|---|---|---|
| 交易进程内存 | 12 策略 + 848 合约 reader | 需 `LimitNOFILE=1048576`（fd 坑②） |
| datakit | 全市场 tick 订阅 → min1/min5/d1 合成 | 僵尸合约已由 contracts 每日剔除根治 |
| Logs | ~80M 累计，日增 ~5M | 暂无 logrotate，磁盘 47% 健康 |
| 20:40 重启全程 | hotdaily 3s + WatchDog 拉起 ~9s | 重启窗口内无行情（15:00 收盘后），零交易影响 |

---

## 3. 告警处置 SOP

> 完整事件清单见 `GUIDE.md` 告警表（含冷却时间）。下表补"处置动作"。
> 前置：webhook 未配时全部以 `Logs/alerts.log` 为准（grep 时间戳）。

| 事件 | 含义 | 处置 | 升级条件 |
|---|---|---|---|
| `ghost_fill` | 真值显示券商侧**没有**这笔仓位（执行单元卡死/订单被拒） | ①看 Trader 日志有无对应委托回报；②ghost_trim 已自动裁层则核对裁剪量；③`WT_EXEC_RECOVER=restart` 生效中会自愈重启 | 同品种 24h 内 ≥2 次 → 查执行单元根因（DATA_OPS §9） |
| `ghost_trim` | 自愈动作留痕：信号层已裁到券商同向手数 | 核对 `restored` 日志与监控持仓一致即可 | 裁剪量 > 本层 50% → 人工复盘该时段执行链 |
| `fill_timeout` | 下单 600s 未达目标（真值不可用的弱证据） | 多数=分批慢（MA 53 手分 50 分钟属正常）；看 trades.csv 是否在增长 | 1200s 后 `fills unverified` 放行 + 无成交 → 按 ghost_fill 流程 |
| `truth_mismatch` | 账户持仓与本策略信号仓有差额 | **alert 模式不改仓**。共享账户差额可能含别家仓位——先核对券商 App 本合约总持仓；确认是本策略未成交 → 等执行单元收敛或次日重启 | 连续 2 个交易日存在 → DATA_OPS §11 升级 SOP（人工比对→ghost_trim/重建/忽略） |
| `truth_stale` | rtdata 超过 `truth_max_age` 未刷新 | 对账/资金闸已自动停用（回退框架口径）。当日不再处理；观察次日 20:40 重启后是否复活（9/17 链路死亡即靠重启复活） | 连续 2 天 stale → 查 CTP 登录日志（错误 90/4097） |
| `veto_*` | 风控缩量/否决（per_market/total_risk/margin/addscale…） | 缩量是**正常工作**（9/23 p 加仓 118→8）；`cap=0` 连续出现 → 检查 `max_total_risk_frac` 与在途记账 | 某品种连续 3 日 cap=0 → 评估预算或资金协议 |
| `account_gate`（warn 模式） | 本笔预估保证金接近可用资金×(1−reserve) | warn 不改手数；记录当日频次 | 频次上升 → 资金协议定案（手册 §1.4），或临时降 units |
| `limit_lock` | 价格钉住 >120s 且有未闭合窗口（疑似涨跌停） | 加仓已自动暂停；出场/止损不受影响——等价格恢复移动自动解除 | 锁死跨日 → 手工核对交易所公告 |
| `exec_recover` / `exec_recover_skip` | 执行单元卡死自愈重启 / 今日已重启过 | 重启后看 `restored` 正常即可 | 每日触发 → 根因未除，提 wtpy issue |
| `reconcile` | 引擎层量与持仓连续失配→重建 / 外部全平 | 重建后核对止损位合理；外部全平=人工干预，策略已收编为 flat | 频繁触发 → 查是否有第二个系统在同账户动仓 |
| `stale_pending` / `orphan_pending` | 日评估/on_init 清除挂起的在途决策 | 留痕即可；武装位保留，价格仍在位上方会立即补票 | 高频出现 → 有决策丢弃路径未清 pending（bug 信号） |
| `trigger_drop` | 触发决策因 N 缺失被丢弃 | **不应发生**；记录当时的 `eval window` 日志 | 出现即提 bug |
| `hots_roll` / `hots_fail` | 主力换月（含重启）/ 双源判定失败 | roll：核对次日图表交易的是新主力；fail：沿用旧映射，次日自动重试 | fail 连续 2 天 → 查 sina/tqsdk 凭据 |
| `hotdaily_restart` / `hotdaily_fail` | 20:40 重建+重启成功（留痕）/ 续接失败 | restart=每日正常；fail=策略在用陈旧窗口，**当日必须人工处理**（手动跑 `update_hot_daily.py` 看报错） | fail 当日未处理 → 夜盘通道缺 bar |
| `contracts_fail` | 合约同步失败 | 沿用旧文件；新上市合约滞后一天可接受 | 临近换月窗口失败 → 手动 `--force` |

---

## 4. 应急手册

### 4.1 紧急全平（kill switch）

```bash
# 1) 停 WatchDog 托管（否则杀进程会被拉起）
systemctl stop wt-turtle-monitor
# 2) 停交易进程（datakit 可留——只影响行情落盘）
pkill -f "run.py$"
# 3) 用券商终端/CTP 手工平掉全部持仓（12 品种，监控页面持仓列表对照）
# 4) 事后: 清理 generated/stradata 各策略 positions+signals 两段为 0
#    （否则重启会按旧信号追单）→ systemctl start wt-turtle-monitor
```

### 4.2 单品种异常处置（以 CF 为例）

```bash
# 查: 该策略日志
grep turtle_CZCE_CF_HOT $D/Logs/Runner_$(date +%F).log | tail -50
# 杀单策略: WatchDog 按组托管, 整组重启影响 12 品种 —— 优先用"改状态+整组重启"
# 手工修状态三步（一个会话内完成, 见 §2.3）:
cp $D/generated/userdata/ud_turtle_CZCE_CF_HOT.json{,.bak}
cp $D/generated/stradata/turtle_CZCE_CF_HOT.json{,.bak}
#   → 编辑 layers/stop_level/positions/signals → pkill -f "run.py$" → 看 restored 日志
```

### 4.3 常见故障速查

| 现象 | 第一反应 |
|---|---|
| 页面全空白 | venv 缺 websockets（`pip install websockets` + 重启监控；GUIDE §1.3） |
| 商品策略整日零活动 | 看 09:0x 首 tick 有无 `catchup eval` / `eval window` 日志；查 hotdaily 昨晚是否 `已重启` |
| 某品种触发无响应 | grep `trigger_drop` / `No lastest tick data`（执行单元卡死 → exec_recover） |
| 止损没触发 | 查 `_last_n` 同步日志（重启后 NaN 静默丢触发，9/5 已修，出现即回归） |
| 数据看起来陈旧 | `eval window: N 根 [首~末]` 日志直接给出窗口日期，与当日比对 |
| 20:40 后进程没起来 | `systemctl status wt-turtle-monitor`（WatchDog 载体）；`journalctl -u wt-turtle-hotdaily` 看 kill 是否执行 |

### 4.4 换机/重建

`DEPLOY.md` + `deploy_tpl/systemd/install_units.sh <prefix> <mon_token> [tq_user tq_pass]` +
`python3 make_deploy.py --verify <deploy_dir>`（manifest 漂移校验）+ `scripts/init_monitor_db.py`。

---

## 5. 变更检查清单

**新上品种**：hots.json 键 → contracts/commodities 含该品种 → storage csv 数据 →
`turtle_config.yaml` instruments 加项 → prepare_data 预检 → 重启 → 核对图表与 `restored`。

**改配置**（§1.3 SOP）+ 改 units/通道 → 跑矩阵回测确认基线漂移可接受。

**切真实券商（SimNow→生产）**：真实费率入 fees.json；首月 units 减半；
`account_gate_mode` 从 warn 复核升级；tdparsers/mdparsers 凭据切换（keep-secrets 纪律）；
切换窗口选周末；切换后首个交易日盘前全量巡检。

**升级 wtpy**：先在备机跑 383 测试 + 单品种 SimNow 冒烟；执行单元卡死根因
（0.9.9.3 libWtCore 与源码差异）若在新版修复则移除每日重启依赖的备注。

---

## 6. 风险登记（指针）

完整残余风险与关键不变量见 `DATA_OPS.md §11`（2x 超时放行 fail-open、
pending 同 tick 不变量、fees 信号口径、reader 缓存须重启）。
**当前最高优先级遗留**：webhook URL 未配（`notify.enabled=false`）——
所有 S 级检测无声，配 URL 即接线，无需改代码。
