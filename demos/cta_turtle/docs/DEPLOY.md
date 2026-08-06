# cta_turtle 生产部署手册 (方案C: 独立监控 + WatchDog 托管)

## 拓扑

```
[远程服务器]
├── runMonSvr.py          # 独立监控进程 (FastAPI, :8080) — systemd 托管
│   └── data.db           # 组合/调度/用户注册表 (CWD 下, WtMonSvr 硬编码)
└── deploy/cta_turtle/    # 自包含组合目录 — WatchDog 拉起 run.py
    ├── run.py            # 引擎-only 入口 (while-sleep 保活)
    ├── 策略代码/配置      # make_deploy.py 同步
    └── Logs/ generated/  # 运行时产出 (DataMgr 按注册 path 读取)

[本地 Windows]  浏览器 → SSH隧道 → http://127.0.0.1:8080/console
```

## 首次部署

```bash
cd demos/cta_turtle

# 1. 同步组合目录 (重复执行=幂等同步; 已填凭据默认保留)
python3 make_deploy.py --target ./deploy

# 2. 填交易/行情账户 (仅服务器本地, 不进git)
vi deploy/cta_turtle/tdparsers.yaml deploy/cta_turtle/tdtraders.yaml

# 3. 预置监控 db (组合注册 + WatchDog调度 + 首个admin; 幂等)
WT_MON_ADMIN_PASS='<强密码>' python3 scripts/init_monitor_db.py

# 4. 启动监控
python3 runMonSvr.py --port 8080
```

浏览器打开 console → admin 登录 → 组合列表应见 `cta_turtle` → 点启动
(WatchDog 以 `python3 run.py` 拉起, cwd=组合目录, guard=崩溃自动重启)。

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
Description=WonderTrader Monitor (cta_turtle)
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/cta_turtle
Environment=WT_MON_ADMIN_PASS=<强密码>
Environment=WT_MON_TOKEN=<seckey>
ExecStart=/usr/bin/python3 runMonSvr.py --port 8080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
策略进程**不要**另建 systemd — 由 WatchDog 托管 (GUI 启停/guard 自动重启)。

## 关键一致性约束

| 约束 | 说明 |
|---|---|
| mqurl 一致 | 策略 `config.yaml` 的 `notifier.url` 与组合注册的 mqurl 必须**字符串完全一致** (默认 `ipc:///tmp/wt_turtle.ipc`), 否则 GUI 收不到订单/成交/日志推送 |
| data.db 位置 | `WtMonSvr` 硬编码 `DataMgr('data.db')` 相对其 **CWD** (`WtMonSvr.py:320`), init 脚本 `--db` 必须指向 runMonSvr 启动目录下的 data.db |
| 路径绝对化 | `make_deploy.py` 已把 config.yaml 的 basefiles/storage 重写为绝对路径 (`WtEngine.init` 的 folder 参数不传文件名时被忽略, basefiles 按 CWD 解析) |
| 保活方式 | run.py 用 `while-sleep` 而非 `input()` (Popen 无 tty, input 立即 EOF 退出) |

## 日常运维

- **改代码后**: 重跑 `python3 make_deploy.py` 同步 → GUI 重启组合
- **改 turtle_config.yaml**: 可直接在 GUI 文件编辑器改组合目录内副本 → 重启组合
- **交易时段干预**: GUI 改 `filters.yaml` 热生效 (无需重启)
- **日志**: GUI 日志页 / 组合目录 `Logs/`
- **凭据轮换**: 改组合目录 tdparsers/tdtraders.yaml → 重启组合; `make_deploy` 默认不覆盖已填凭据

## 安全清单

- [ ] `WT_MON_ADMIN_PASS` 强密码, 仅环境变量传入 (不入 shell history 可用 `read -s`)
- [ ] tdtraders/tdparsers 凭据只在服务器组合目录, 确认不提交 git
- [ ] 8080 不对公网开放; 跨网访问一律 SSH 隧道
- [ ] data.db 含密码哈希, 备份时注意保密
