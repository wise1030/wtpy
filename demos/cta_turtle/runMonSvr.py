"""独立监控进程 — WtMonSvr 管理服务器 (方案C 标准生产拓扑)

与策略进程(run.py, 由 WatchDog 拉起)分离:
- GUI: http://<host>:<port>/console (PC) | /mobile (手机)
- 组合注册: 首次用 scripts/init_monitor_db.py 预置 data.db (幂等),
  或在 GUI 中手工添加 (groupid=cta_turtle, path=组合目录,
  mqurl 须与策略 config.yaml 的 notifier.url 完全一致)
- Windows 访问: ssh -L 8080:127.0.0.1:8080 user@server 后浏览器开
  http://127.0.0.1:8080/console (推荐); 或直开端口 + --token 鉴权

用法:
  python3 runMonSvr.py                          # ./deploy, 端口8080, 无token
  WT_MON_TOKEN=xxx python3 runMonSvr.py --port 8080
"""
import argparse
import os

from wtpy.monitor import WtMonSvr

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", type=str, default="0.0.0.0")
    ap.add_argument("--deploy-dir", type=str,
                    default=os.path.join(HERE, "deploy"),
                    help="组合部署根目录 (GUI 文件浏览器根)")
    ap.add_argument("--token", type=str, default="",
                    help="访问令牌 seckey; 亦可用环境变量 WT_MON_TOKEN")
    args = ap.parse_args()

    token = args.token or os.environ.get("WT_MON_TOKEN", "")

    svr = WtMonSvr(deploy_dir=os.path.abspath(args.deploy_dir))
    if token:
        svr.enable_token(seckey=token)
        print("[runMonSvr] token access enabled")

    print(f"[runMonSvr] console: http://{args.host}:{args.port}/console")
    print(f"[runMonSvr] deploy_dir: {os.path.abspath(args.deploy_dir)}")
    svr.run(port=args.port, host=args.host, bSync=True)


if __name__ == "__main__":
    main()
