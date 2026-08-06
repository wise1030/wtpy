# cta_spread_arb 生产部署手册

## 拓扑 (多组合共管)

单一 `runMonSvr.py` 进程托管多个组合, 各组合独立 groupid/mqurl/path,
注册到同一 `data.db`。WatchDog 后台线程轮询所有组合, 前端 console 按
组合切换展示。

```
demos/
├── runMonSvr.py             # 统一监控进程 (FastAPI, :8080) - systemd 托管
│   └── data.db              # 组合/调度/用户注册表 (CWD 下, WtMonSvr 硬编码)
└── deploy/                  # 统一部署根
    ├── cta_turtle/          # cta_turtle/make_deploy.py 同步 (另一组合)
    └── cta_spread_arb/      # 自包含组合目录 - WatchDog 拉起 run.py
        ├── run.py           # 引擎-only 入口 (while-sleep 保活)
        ├── strategies/ helpers/ factors/   # 策略代码 (make_deploy.py 同步)
        ├── config/          # 整棵配置树 (experiments/base/profiles/...)
        └── config.yaml      # 路径已重写为绝对, notifier.url=/tmp/wt_spread_arb.ipc

[本地 Windows]  浏览器 -> SSH隧道 -> http://127.0.0.1:8080/console
```

## 首次部署

```bash
cd demos/cta_spread_arb

# 1. 同步组合目录 (重复执行=幂等同步; 已填凭据默认保留)
#    --target ../deploy 同步到统一部署根 (与 cta_turtle 共管)
python3 make_deploy.py --target ../deploy

# 2. 填交易/行情账户 (仅服务器本地, 不进git)
vi ../deploy/cta_spread_arb/tdparsers.yaml ../deploy/cta_spread_arb/tdtraders.yaml

# 3. 预置监控 db (组合注册 + WatchDog调度 + 首个admin; 幂等)
#    多组合共管: 若 cta_turtle 已播种 admin, 此处不重置密码
#    --experiment 指定实验列表, 写入 schedules.param
WT_MON_ADMIN_PASS='<强密码>' python3 scripts/init_monitor_db.py \
    --db ../deploy/data.db --group-dir ../deploy/cta_spread_arb \
    --experiment l_pp_2609,rb_hc_2601

# 4. 启动统一监控 (在 demos/ 目录)
cd ..
python3 runMonSvr.py --port 8080
```

浏览器打开 console -> admin 登录 -> 组合列表应见 `cta_spread_arb`
(及共管的 `cta_turtle`) -> 点启动 (WatchDog 以
`python3 run.py --experiment l_pp_2609,rb_hc_2601` 拉起,
cwd=组合目录, guard=崩溃自动重启)。

## 多实验

`run.py --experiment <list>` 支持逗号分隔多实验, 每个实验一个
`SpreadArbStrategy` 实例 (WT 框架天然支持多策略)。策略名规则:
- 单实例: `spread_arb`
- 多实例: `spread_arb_<experiment>` (保证唯一)

`ArbIndexWriter.write_arb_config_multi` 合并写 `generated/marker.json`,
监控前端据此展示各 sub。

切换实验列表: 重跑 `init_monitor_db.py --experiment <new_list>` (幂等
UPDATE schedules.param), 然后 GUI 重启组合。

## Windows 访问

**SSH 隧道 (推荐, 零防火墙改动)**:
```bash
ssh -L 8080:127.0.0.1:8080 user@server
# 浏览器: http://127.0.0.1:8080/console
```

**直开端口 (内网)**: 防火墙放行 8080 + 令牌鉴权:
```bash
WT_MON_TOKEN='<seckey>' python3 runMonSvr.py --port 8080
```
切勿无鉴权暴露公网。

## systemd 托管 (监控进程)

`/etc/systemd/system/wt-monitor.service`:
```ini
[Unit]
Description=WonderTrader Monitor (multi-group)
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/wt_deploy
Environment=WT_MON_ADMIN_PASS=<强密码>
Environment=WT_MON_TOKEN=<seckey>
ExecStart=/usr/bin/python3 /path/to/demos/runMonSvr.py --deploy-dir /opt/wt_deploy --port 8080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
策略进程**不要**另建 systemd - 由 WatchDog 托管 (GUI 启停/guard 自动重启)。

## 关键一致性约束

| 约束 | 说明 |
|---|---|
| mqurl 一致 | 策略 `config.yaml` 的 `notifier.url` 与组合注册的 mqurl 必须**字符串完全一致** (默认 `ipc:///tmp/wt_spread_arb.ipc`), 否则 GUI 收不到订单/成交/日志推送 |
| mqurl 唯一 | 多组合共管时, 各组合 mqurl 必须不同 (cta_turtle 用 `/tmp/wt_turtle.ipc`, 本组合用 `/tmp/wt_spread_arb.ipc`), 同一进程可同时订阅多个 ipc |
| groupid 唯一 | 多组合共管时 groupid 必须不同 (`cta_turtle` vs `cta_spread_arb`) |
| data.db 位置 | `WtMonSvr` 硬编码 `DataMgr('data.db')` 相对其 **CWD** (`WtMonSvr.py:320`), init 脚本 `--db` 必须指向 runMonSvr.py 启动目录下的 data.db |
| 路径绝对化 | `make_deploy.py` 已把 config.yaml 的 basefiles/storage 重写为绝对路径 (`WtEngine.init` 的 folder 参数不传文件名时被忽略, basefiles 按 CWD 解析) |
| 保活方式 | run.py 用 `while-sleep` 而非 `input()` (Popen 无 tty, input 立即 EOF 退出) |

## 日常运维

- **改代码后**: 重跑 `python3 make_deploy.py --target ../deploy` 同步 -> GUI 重启组合
- **改实验配置**: 改组合目录内 `config/experiments/<name>.yaml` -> 重启组合
- **切换实验列表**: 重跑 `init_monitor_db.py --experiment <new_list>` -> GUI 重启组合
- **交易时段干预**: GUI 改 `filters.yaml` 热生效 (无需重启)
- **日志**: GUI 日志页 / 组合目录 `Logs/`
- **凭据轮换**: 改组合目录 tdparsers/tdtraders.yaml -> 重启组合; `make_deploy` 默认不覆盖已填凭据

## 安全清单

- [ ] `WT_MON_ADMIN_PASS` 强密码, 仅环境变量传入 (不入 shell history 可用 `read -s`)
- [ ] tdtraders/tdparsers 凭据只在服务器组合目录, 确认不提交 git
- [ ] 8080 不对公网开放; 跨网访问一律 SSH 隧道
- [ ] data.db 含密码哈希, 备份时注意保密
