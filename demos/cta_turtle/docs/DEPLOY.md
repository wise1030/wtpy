# cta_turtle 生产部署手册 (方案C: 独立监控 + WatchDog 托管)

## 拓扑

```
[远程服务器]
├── runMonSvr.py          # 独立监控进程 (FastAPI, :8080) — systemd 托管
│   └── data.db           # 组合/调度/用户注册表 (CWD 下, WtMonSvr 硬编码)
└── deploy/cta_turtle/    # 自包含组合目录 — WatchDog 托管两个进程:
    ├── runDt.py          #   datakit 行情进程 (ParserCTP→K线合成/落盘→UDP广播)
    ├── run.py            #   交易引擎进程 (ParserUDP 收行情, while-sleep 保活)
    ├── 策略代码/配置      # make_deploy.py 同步
    └── Logs/ DtLogs/ generated/  # 运行时产出 (DataMgr 按注册 path 读取)

数据流: CTP ─→ runDt (datakit, WtDataWriter 写 rt mmap + his DSB 轮转)
              ─UDP :9001/查询:3997─→ run.py (交易引擎)
        两进程共享同一 storage 目录 (DATA_OPS.md §2 标准双进程拓扑)
启动顺序无强依赖: ParserUDP 在 datakit 未就绪时静默等待; 双进程各自
guard 崩溃自动重启。

[本地 Windows]  浏览器 → SSH隧道 → http://127.0.0.1:8080/console
```

## 首次部署

```bash
cd demos/cta_turtle

# 1. 同步组合目录 (重复执行=幂等同步; 已填凭据默认保留)
python3 make_deploy.py --target ./deploy

# 2. 填账户凭据 (仅服务器本地, 不进git)
#    行情凭据 → mdparsers.yaml (datakit 侧); 交易凭据 → tdtraders.yaml
vi deploy/cta_turtle/mdparsers.yaml deploy/cta_turtle/tdtraders.yaml

# 3. 预置监控 db (组合注册 + WatchDog双进程调度 + 首个admin; 幂等)
WT_MON_ADMIN_PASS='<强密码>' python3 scripts/init_monitor_db.py

# 4. 启动监控
python3 runMonSvr.py --port 8080
```

浏览器打开 console → admin 登录 → 组合列表应见 `cta_turtle` → 点启动
(WatchDog 以 `python3 run.py` / `python3 runDt.py` 拉起, cwd=组合目录,
guard=崩溃自动重启)。

## Windows 访问

**SSH 隧道 (推荐, 零防火墙改动)**:
```bash
ssh -L 8080:127.0.0.1:8080 user@server
# 浏览器: http://127.0.0.1:8080/console
```

**直开端口 (内网/公网)**: 腾讯云安全组放行 TCP:8081 (控制台→实例→安全组
→入站规则; 建议来源收窄为办公 IP) + 令牌鉴权:
```bash
WT_MON_TOKEN='<16/24/32字节seckey>' python3 runMonSvr.py --port 8081
```
浏览器: http://129.211.5.54:8081/console → admin 登录。
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
| 密码哈希 | **双层 md5**: 前端 md5(明文) → 后端 md5(loginid+前端值); init_monitor_db 播种即按此口径。改密码只能走 GUI 或同口径直改 db, 且**改 db 后必须重启监控** (DataMgr.get_user 读启动时内存缓存) |
| token 长度 | `WT_MON_TOKEN` 兼作 AES 密钥, 必须 **16/24/32 字节** (runMonSvr 启动时校验); 登录页用 admin 密码, token 用于 API 直调 |

## 日常运维

- **改代码后**: 重跑 `python3 make_deploy.py` 同步 → GUI 重启组合
- **改 turtle_config.yaml**: 可直接在 GUI 文件编辑器改组合目录内副本 → 重启组合
- **交易时段干预**: GUI 改 `filters.yaml` 热生效 (无需重启)
- **日志**: GUI 日志页 / 组合目录 `Logs/`(交易) + `DtLogs/`(datakit)
- **凭据轮换**: 改组合目录 mdparsers(行情)/tdtraders(交易).yaml → 重启对应进程; `make_deploy` 默认不覆盖已填凭据

## 安全清单

- [ ] `WT_MON_ADMIN_PASS` 强密码, 仅环境变量传入 (不入 shell history 可用 `read -s`)
- [ ] mdparsers/tdtraders 凭据只在服务器组合目录, 确认不提交 git
- [ ] 8080 不对公网开放; 跨网访问一律 SSH 隧道
- [ ] data.db 含密码哈希, 备份时注意保密
