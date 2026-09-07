# cta_turtle 使用指南：监控页面 · 策略逻辑 · 配置参数

> 适用版本：2026-09 生产部署（12 品种 / trigger 模式 / datakit 双进程拓扑）。
> 相关文档：`DEPLOY.md`（部署）、`DATA_OPS.md`（数据运维）、根 `README.md`（回测）。

---

## 第一部分 监控页面使用

### 1.1 访问与登录

| 项 | 值 |
|---|---|
| 地址 | `http://<服务器IP>:8081/console`（生产：129.211.5.54:8081） |
| 账号 | `admin` / 部署时设定的密码 |
| 服务 | `wt-turtle-monitor.service`（systemd 托管，WatchDog 内置其中） |

密码口径：前端 md5(明文) → 后端 md5(loginid+前端值) 双层哈希。改密码只能走
GUI 或按同口径直改 data.db，**改 db 后必须重启监控服务**（用户缓存在内存）。

### 1.2 页面功能

登录后进入组合视图，核心区域：

- **策略列表**：12 个品种策略实例（`turtle_<交易所>_<品种>_HOT`），
  可单独查看每个实例的实时图表（K线+通道+出入场标记）。
- **持仓**：当前持仓明细（方向/手数/开仓价/浮动盈亏）。注意显示的是
  **账户实际成交口径**（trades.csv 记录真实成交均价），与策略引擎记账价
  可能存在 ≤0.5N 的回写窗口差（见 2.6）。
- **成交**：逐笔成交记录（时间/方向/开平/价格/数量/标签）。
- **资金曲线**：动态权益曲线（dynbalance 为**净盈亏**口径，不含本金；
  真实权益 = 500万基准 + 净盈亏）。
- **日志**：策略运行日志实时滚动（`stra_log_text` 输出，含武装/触发/
  风控缩量/对账事件）。

### 1.3 数据链路（排障基础）

```
策略进程 → generated/outputs/<策略>/*.csv (trades/funds/positions/signals)
        → 监控 DataMgr 轮询加载 → REST API → 前端
策略进程 → WebSocket (8081) → 前端实时区
```

- 页面**字段全部空白**但策略在跑 → WebSocket 通道断开。检查：
  `journalctl -u wt-turtle-monitor | grep -c "WebSocket.*accepted"`，
  若为 0 且有 "Unsupported upgrade request"，是 venv 缺 websockets 库
  （`pip install websockets` + 重启监控）。
- 数据有但**不刷新** → REST 正常 WS 断开，强制刷新页面重连。
- **API 直调**：需 token（部署时设定，兼作 AES key，必须 16/24/32 字节）。

### 1.4 重启的影响

**restart 监控服务会级联重启策略进程**（WatchDog 内置于 monitor service，
同 cgroup 消亡，随后被守护拉起）。策略重启无损：快照恢复持仓与武装状态，
但会触发一次合约同步（启动链 step0，白盘约数分钟）。避免在交易时段
随意 `systemctl restart wt-turtle-monitor`。

---

## 第二部分 策略业务逻辑

### 2.1 总览

经典海龟趋势跟踪，12 品种商品期货主力连续（HOT），每品种一个独立策略
实例。核心分层：

```
TurtleEngine (纯逻辑, 零框架依赖)   ← 信号状态机
StraTurtle   (框架壳)              ← 生命周期/执行/持久化
filters/     (可插拔信号过滤链)     ← 武装时前置裁决
RiskManager  (python 组合风控)      ← 触发时缩量/否决
SimpleRiskMon (C++ 账户级熔断)      ← 引擎内置兜底
```

### 2.2 信号系统

| | S1（短线） | S2（长线） |
|---|---|---|
| 入场通道 | 20 日唐奇安 | 55 日唐奇安 |
| 出场通道 | 10 日反向 | 20 日反向 |
| 过滤器 | 盈利跳过（上次 S1 盈利则跳过本次） | 无 |
| fail-safe | 被跳过后 55 日突破仍入场 | — |

- **通道语义**：唐奇安通道**不含当根**（用前 N-1 根计算，无未来函数）。
- **N**：ATR(20) Wilder 平滑，波动率单位，所有仓位/止损/加仓的标尺。
- **加仓**：每盈利 0.5N 加一层，最多 `pyramid_layers` 层（当前配置 units=1
  **不加仓**，层数=1）。
- **止损**：最后加仓价 ∓ 2N（`stop_atr_mult`）。
- **S1+S2 双系统**：`system: "S1+S2"` 时两引擎各半 units 并行，净仓批量
  执行（当前配置均为单 S1）。

### 2.3 武装-触发状态机（entry_mode=trigger）

经典海龟是**盘中触碰即入场**，与"收盘价确认"不同：

1. **日评估**（session_end / catchup / bt 日线闭合）：用截至昨日收盘的
   数据计算 N 与通道 → **武装**入场位（多/空）、持仓品种武装
   止损/加仓/离场位。过滤器链在此时**前置裁决**，被否决的方向当日
   解除武装。
2. **盘中触发**：每个 tick（实盘）或 bar 仿真（回测）检查价格是否触及
   武装位 → 触及即下单。触发时**风控复核**（缩量或否决；否决则当日
   撤武装，次日重武装）。
3. **当日有效**：武装位当日不随价格波动；次一交易日重新评估。

### 2.4 信号过滤链（filters/）

引擎决策之后、执行之前链式调用，只读不改引擎状态，逐级记录否决原因：

| 过滤器 | 作用 | 关键参数 |
|---|---|---|
| DirectionGate | 方向闸（long/short/both） | direction |
| AdxTrend | ADX≥阈值才放行（趋势强度） | period=14, threshold=25 |
| VolatilityBand | N 在历史分位带内才放行 | window=250, min/max_pct |
| LiquidityFloor | 均量/持仓量下限 | k=5, min_volume, min_oi |

S1 盈利跳过保留在引擎内（与 sim 单推进深度耦合），可用
`enable_s1_filter: false` 整体关闭。

### 2.5 双层风控

**python RiskManager（下单前最后闸门，缩量优先于否决）**：
- 单品种预算：本笔 2N 潜亏 ≤ 权益 × `max_risk_per_mkt`(2%)
- 组合总敞口：Σ在途 2N 潜亏 ≤ 权益 × `max_total_risk_frac`(10%)
- 相关性组/全组合单位数上限（`max_units_per_group`/`max_units_total`）
- 保证金预估：占用 ≤ 权益 × `max_margin_usage`(60%)
- **权益口径 = fallback_capital(500万) + `stra_get_fund_data(0)` 净盈亏**
  （该接口在 bt/实盘均不含本金，动态权益；接口失效退回 500 万）

**C++ SimpleRiskMon（账户级熔断，config.yaml riskmon 段）**：
- 口径：当日**盈利部分**从高点回撤 ≥20% 且高点在 30 分钟窗口内
  → `setVolScale` 全局缩量。注意是**账户级**（SimNow 共享账户，含其他
  策略盈亏），且慢速阴跌不触发（出窗）——定位为极端闪崩兜底，
  常规风险控制靠 python 层。

### 2.6 执行与记账

- **目标仓位制**：`stra_set_position(目标净仓)`，执行单元
  （MiniImpactExecUnit）分批追单至目标（MA 曾 53 手分 50 分钟成交）。
- **记账价**：confirm 时从 avgpx 增量反推（`_implied_fill`）；分批窗口
  未闭合时 fallback 触发价，**待成交齐后自动用实际成交均价回写**
  （`_reanchor_actual_fill`，偏差>0.5tick 才动，stop/add 武装位跟随）。
  回写期间监控显示的浮盈基于框架真实成本，引擎记账价收敛有最多一个
  评估窗口的延迟。
- **换月 re-anchor**：HOT 切换导致 avgpx 整体漂移时，各层价格平移对齐
  （与 fill 回写协同：先精确修最后一层，换月检测不误报）。

### 2.7 状态持久化与自愈

- **快照 v4**：引擎状态（方向/层/S1 过滤器/武装位/pending/N/通道）+
  去重键（`_d1_date`/`_m5_key`），触发事件即时落盘，重启逐字节恢复。
- **运行时对账**：每评估周期比对引擎层量 vs 框架持仓，连续 3 根失配
  → 告警并按实际持仓重建引擎。
- **catchup 自愈**：新交易日首个 tick 检测 his 末根未评估（session_end
  未触发或盘后启动）→ 立即评估武装，每日至多一次。与 session_end
  互补（session_end 依赖行情时间推进过收盘点，848 全订阅后国债 tick
  覆盖 15:15 使白盘正常触发）。
- **触发价"补票"特征**：catchup 时若价格已穿越昨日通道，首 tick 立即
  追入，成交价比武装位高（绩效归因需知悉）。

---

## 第三部分 配置参数说明

### 3.1 config/turtle_config.yaml — base 段（全局）

| 参数 | 默认 | 说明 |
|---|---|---|
| `entry_mode` | close | `trigger`=盘中触发（当前生产）；`close`=收盘确认 |
| `unit_var` | 200000 | 1 个交易单位的 VaR 预算（CNY，按 1N 口径） |
| `atr_period` | 20 | N = ATR(20)，Wilder 平滑 |
| `stop_atr_mult` | 2.0 | 止损 = 最后加仓价 ∓ 2N |
| `pyramid_layers` | 4 | 最大加仓层数（1=不加仓） |
| `pyramid_step_atr` | 0.5 | 每盈利 0.5N 加一层 |
| `bar_cnt` | 120 | 订阅 K 线数（需 > 55+20） |
| `s1_entry/s1_exit` | 20/10 | S1 入场/出场通道 |
| `s2_entry/s2_exit` | 55/20 | S2 通道（55 兼 S1 fail-safe） |
| `enable_s1_filter` | true | S1 盈利跳过开关（false=裸 20/10） |
| `recon_patience` | 3 | 对账连续失配 N 根后重建 |
| `intrabar_period/cnt` | m5/300 | 盘中止损主K（配合 intrabar_stop） |
| `journal_dir` | "" | K线旁路日志目录（空=关闭） |

### 3.2 instruments[] 段（每品种）

| 参数 | 默认 | 说明 |
|---|---|---|
| `code` | — | `SHFE.rb.HOT` 格式（HOT=主力连续） |
| `period` | d1 | 信号周期 d1｜h1 |
| `system` | S1 | S1｜S2｜S1+S2（双系统各半 units） |
| `units` | 1 | 交易单位数；每层 VaR = units×unit_var |
| `direction` | both | both｜long｜short（方向闸） |
| `group` | "" | 相关性组（black/metal/energy/cheme/oils/soft/equity） |
| `filters` | [] | 附加过滤器链（type+params，见 2.4） |
| `intrabar_stop` | false | 盘中止损（主K切 m5/m1，信号仍取 d1/h1） |

当前生产：12 品种（rb/cu/al/sc/TA/MA/CF/pp/l/m/p/IF）6 组，全部
`units=1, system=S1, entry_mode=trigger`。

### 3.3 risk 段（组合风控预算）

| 参数 | 默认 | 生产值 | 说明 |
|---|---|---|---|
| `max_risk_per_mkt` | 0.02 | 0.02 | 单品种本笔 2N 潜亏/权益 |
| `max_total_risk_frac` | 0.06 | **0.10** | 组合在途 2N 总潜亏/权益 |
| `max_margin_usage` | 0.60 | 0.60 | 保证金占用/权益上限 |
| `margin_rate` | 0.12 | 0.12 | 保证金率预估 |
| `max_units_per_group` | 2 | 2 | 相关性组单位数上限（0=不限） |
| `max_units_total` | 4 | **6** | 全组合单位数上限（0=不限） |
| `fallback_capital` | 500万 | 500万 | 权益基准/接口失效回退 |

### 3.4 config.yaml — riskmon 段（C++ 账户熔断）

**参数必须平铺在 riskmon 下**（嵌套 params 层 C++ 读不到，会全 0 空转）：

```yaml
riskmon:
    active: true
    module: WtRiskMonFact
    name: SimpleRiskMon
    base_amount: 5000000      # 组合基础资金
    basic_ratio: 101          # 当日权益须创新高 101% 才启动跟踪
    calc_span: 5              # 检查间隔(s)
    inner_day_active: true    # 日内熔断开关
    inner_day_fd: 20.0        # 日内盈利回撤阈值(%, 数字不带%)
    multi_day_active: false   # 多日熔断(关闭, python层已覆盖)
    multi_day_fd: 60.0
    risk_scale: 1.0           # 触发后目标仓位缩放
    risk_span: 30             # 高点有效窗口(min)
```

### 3.5 参数调整的影响速查

| 想要 | 调什么 |
|---|---|
| 更大/更小仓位 | `unit_var`（线性）或 `units` |
| 更紧/更松风控 | `max_risk_per_mkt` / `max_total_risk_frac` |
| 允许加仓 | `instruments[].units>1` 且 `pyramid_layers>1` |
| 只做多/空 | `instruments[].direction` |
| 更多品种 | instruments 增加 + hots.json/contracts.json/commodities.json 配套 |
| 关闭 S1 跳过 | `enable_s1_filter: false` |
| 账户级熔断阈值 | config.yaml riskmon `inner_day_fd` |

---

## 第四部分 自动化运维链路（systemd timers）

| 定时任务 | 时间 | 职责 |
|---|---|---|
| wt-turtle-contracts.timer | 周一~五 20:30 | contracts.json 全量更新（CTPLoader，过期合约剔除） |
| wt-turtle-hots.timer | 周一~五 20:35 | hots.json 主力映射更新（新浪持仓量判定）；**有变更才写盘+重启 datakit/run.py**（WatchDog 拉起）并 webhook 告警 |

换月告警示例：`海龟主力换月 20260908: SHFE.rb: rb2701→rb2705, 已重启 2 进程`。

### 告警事件清单（notify 启用后推送）

| key | 触发 | 冷却 |
|---|---|---|
| veto_* | 风控否决/缩量、手数为 0（首见，同类去重） | 300s |
| fill_timeout | 下单 10 分钟未达目标仓位（涨跌停/执行失败的代理信号） | 300s |
| reconcile | 引擎与券商持仓持续失配→重建 / 外部全平 | 300s |
| session | 收盘持仓汇总 | 每日 |
| hots_roll | 主力换月（update_hots.py） | 每次变更 |
| trigger_drop | 触发决策因 N 缺失被丢弃（不应发生，发生即 bug） | 300s |

启用方式：turtle_config.yaml `notify.enabled: true` + 填入企业微信/钉钉
webhook URL（`type: wechat|dingtalk`）。
