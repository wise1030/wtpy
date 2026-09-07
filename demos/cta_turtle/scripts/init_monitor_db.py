"""监控 data.db 预置脚本 — 免 GUI 手工注册, 幂等

注册内容 (与 /mgr/addgrp + /mgr/cfgmon 等价):
- groups 表: cta_turtle 组合 (DataMgr.add_group, insert-or-update)
- schedules 表: WatchDog 调度 (applyAppConf, insert-or-update), 双进程:
  - cta_turtle_dt:  datakit 行情进程 (runDt.py, 数据底盘, guard)
  - cta_turtle:     交易引擎进程 (run.py, 组合主应用 isGroup, guard)
  path=python3 绝对路径, folder=组合目录, guard=自动重启

用法:
  python3 scripts/init_monitor_db.py \
      --db ./deploy/data.db --group-dir ./deploy/cta_turtle \
      --mqurl ipc:///tmp/wt_turtle.ipc
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

DEFAULT_MQURL = "ipc:///tmp/wt_turtle.ipc"

EMPTY_TASK = {"active": False, "time": 0, "action": 0}


class _StubLogger:
    """WatchDog/AppInfo 需要 logger (applyConf 更新分支会调 .info)"""
    def info(self, msg):
        print(f"[watchdog] {msg}")

    def error(self, msg):
        print(f"[watchdog:ERROR] {msg}", file=sys.stderr)

    def warn(self, msg):
        print(f"[watchdog:WARN] {msg}")


def init_db(db_path: str, group_dir: str, mqurl: str = DEFAULT_MQURL,
            python_bin: str = None, guard: bool = True,
            admin_pass: str = None):
    from wtpy.monitor.DataMgr import DataMgr
    from wtpy.monitor.WatchDog import WatchDog

    python_bin = python_bin or sys.executable
    group_dir = os.path.abspath(group_dir)

    dm = DataMgr(db_path)   # 建表 (groups/schedules/users/actions)

    # === 首个管理员播种 ===
    # users 表初始为空且无默认账号, GUI 创建用户需先登录(鸡生蛋),
    # 故此处播种。已存在时 add_user 走 UPDATE 分支(不重置密码)。
    #
    # ⚠️ 密码哈希口径 (2026-09-01 实测订正): 前端先 md5(明文) 再提交,
    # 后端/播种再 md5(loginid + 前端值) — 双层 md5。因此播种存的是
    # md5(loginid + md5(明文)); 单层 md5(loginid+明文) 会导致永远登不上。
    if admin_pass is None:
        import secrets as _sec
        admin_pass = _sec.token_urlsafe(12)
        print(f"[init_db] generated admin password: {admin_pass} "
              f"(仅本次显示, 请立即保存)")
    import hashlib as _hl
    front_md5 = _hl.md5(admin_pass.encode("utf-8")).hexdigest()
    dm.add_user({
        "loginid": "admin", "name": "管理员", "role": "superman",
        "passwd": front_md5, "iplist": "", "products": "",
        "remark": "seeded by init_monitor_db.py",
    }, admin="init_monitor_db")
    print("[init_db] admin user seeded (role=superman)")

    grp = {
        "id": "cta_turtle",
        "name": "海龟趋势跟踪",
        "path": group_dir,
        "info": "经典海龟 S1/S2 + VaR仓位 + 方向闸",
        "gtype": "cta",
        "datmod": "mannual",
        "env": "实盘",
        "mqurl": mqurl,
    }
    if not dm.add_group(grp):
        raise RuntimeError("add_group failed")
    print(f"[init_db] group registered: cta_turtle -> {group_dir}")

    dog = WatchDog(db=dm.get_db(), logger=_StubLogger())

    # === 双进程调度: datakit(数据底盘) + 交易引擎 ===
    # 启动顺序无强依赖: ParserUDP 在 datakit 未就绪时静默等待;
    # 两者均 guard 崩溃自动重启
    apps = [
        {"id": "cta_turtle_dt", "param": "runDt.py", "is_group": False},
        {"id": "cta_turtle", "param": "run.py", "is_group": True},
    ]
    for a in apps:
        app_conf = {
            "id": a["id"],
            "path": python_bin,
            "folder": group_dir,
            "param": a["param"],
            "span": 10,
            "guard": guard,
            "redirect": False,
            "mqurl": mqurl,
            "schedule": {
                "active": False,
                "weekflag": "0000000",
                "tasks": [dict(EMPTY_TASK) for _ in range(6)],
            },
        }
        dog.applyAppConf(app_conf, isGroup=a["is_group"])
        print(f"[init_db] schedule registered: {python_bin} {a['param']} "
              f"(appid={a['id']}, cwd={group_dir}, guard={guard})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=str,
                    default=os.path.join(ROOT, "data.db"),
                    help="监控 data.db 路径。注意: WtMonSvr 硬编码 DataMgr('data.db') "
                         "相对其 CWD (WtMonSvr.py:320), 故本路径必须是 "
                         "runMonSvr.py 启动目录下的 data.db")
    ap.add_argument("--group-dir", type=str,
                    default=os.path.join(ROOT, "deploy", "cta_turtle"))
    ap.add_argument("--mqurl", type=str, default=DEFAULT_MQURL,
                    help="须与策略 config.yaml 的 notifier.url 完全一致")
    ap.add_argument("--python", type=str, default="",
                    help="拉起 run.py 的 python 解释器绝对路径")
    ap.add_argument("--no-guard", action="store_true",
                    help="关闭崩溃自动重启")
    ap.add_argument("--admin-pass", type=str,
                    default=os.environ.get("WT_MON_ADMIN_PASS", ""),
                    help="首个管理员 admin 的初始密码; 亦可用环境变量 "
                         "WT_MON_ADMIN_PASS; 都未提供则随机生成并打印一次")
    args = ap.parse_args()

    init_db(args.db, args.group_dir, args.mqurl,
            python_bin=args.python or None,
            guard=not args.no_guard,
            admin_pass=args.admin_pass or None)
    print("[init_db] done")


if __name__ == "__main__":
    main()
