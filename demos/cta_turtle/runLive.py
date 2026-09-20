"""海龟策略实盘运行器 (多品种多实例)

每品种一个 StraTurtle 实例 (CtaMocker 单主K限制), WtEngine.add_cta_strategy
天然支持多策略。GUI: WtMonSvr console + notifier 推送, 图表数据由策略内
register_index/set_index_value/add_chart_mark 喂给 generated/ 供监控读取。

前置依赖 (双进程拓扑, DATA_OPS.md §2): tdparsers.yaml 走 ParserUDP,
须先启动 datakit 行情进程 (deploy_tpl/runDt.py) 才有 tick/bar 事件;
单进程调试可临时启用 tdparsers.yaml 中的 ParserCTP 回退块 (无K线合成,
仅连通性验证)。

用法: python runLive.py [--config ./config/turtle_config.yaml] [--port 8080]
"""
import argparse
import os
import sys

from wtpy import WtEngine, EngineType
from wtpy.monitor import WtMonSvr

from StraTurtle import StraTurtle
from helpers.ConfigLoader import ConfigLoader
from helpers.RiskManager import RiskManager

HERE = os.path.dirname(os.path.abspath(__file__))


def _build_notifier(cfg):
    """按 notify 配置段构建 webhook 推送器 (未启用返回 None)"""
    if not getattr(cfg, "notify", None) or not cfg.notify.enabled:
        return None
    try:
        from helpers.notifier import WebhookNotifier
        return WebhookNotifier(urls=cfg.notify.urls,
                               wtype=cfg.notify.type,
                               cooldown=cfg.notify.cooldown)
    except Exception as e:
        print(f"[runLive] notifier init failed: {e}")
        return None


def _preflight(cfg, mode: str = "warn", cfg_path: str = ""):
    """启动前数据完整性门禁: hots键/K线健康/暖机深度 (+journal自动回补)

    cfg_path: 实际生效的配置文件路径 (默认 config/turtle_config.yaml)。
    回补子进程必须用同一个配置, 否则 --config 指定的非默认配置会被忽略
    (P2 修复)。
    """
    if mode not in ("warn", "strict"):
        return
    try:
        from helpers import DataChecker
        common = os.path.abspath(os.path.join(HERE, "..", "common"))
        storage = os.path.abspath(os.path.join(HERE, "..", "storage"))
        # 自动回补: journal 尾部合并到 storage/csv (WT_AUTO_REPAIR=1)
        jdir = getattr(cfg.base, "journal_dir", "")
        if os.environ.get("WT_AUTO_REPAIR") == "1" and jdir:
            try:
                import subprocess as _sp
                r = _sp.run([sys.executable,
                             os.path.join(HERE, "scripts",
                                          "prepare_data.py"),
                             "--config", cfg_path or _cfg_path(),
                             "--common", common,
                             "--storage", storage,
                             "--repair-journal", jdir],
                            capture_output=True, text=True, timeout=120)
                print(r.stdout.strip() or "[repair] no output")
            except Exception as e:
                print(f"[repair] skipped: {e}")
        issues = DataChecker.check_config(cfg, common, storage,
                                          live=True)
        n_err = 0
        for i in issues:
            if i.level == "info":
                continue
            print(f"[precheck][{i.level.upper()}] {i.code}: {i.msg}")
            if i.level == "error":
                n_err += 1
        if mode == "strict" and n_err:
            raise SystemExit(
                f"[preflight] {n_err} data errors, abort (WT_PREFLIGHT"
                f"=strict); 先运行 scripts/prepare_data.py 修复")
    except SystemExit:
        raise
    except Exception as e:
        print(f"[precheck] skipped: {e}")


def _cfg_path():
    return os.path.join(HERE, "config", "turtle_config.yaml")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=str,
                    default=os.path.join(HERE, "config", "turtle_config.yaml"))
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    cfg = ConfigLoader.load(args.config)

    # === 数据完整性预检 (P1 运维) ===
    # WT_PREFLIGHT=off|warn|strict (默认 warn); strict 时 error 阻断启动
    _preflight(cfg, mode=os.environ.get("WT_PREFLIGHT", "warn").lower(),
               cfg_path=args.config)

    engine = WtEngine(EngineType.ET_CTA)
    engine.init('../common/', "config.yaml")

    # 组合级风险预算: 多品种共享同一实例 (单位限制/总敞口跨品种生效)
    risk_mgr = RiskManager(cfg.risk)
    notifier = _build_notifier(cfg)
    if notifier is not None:
        print(f"[runLive] notify: {cfg.notify.type} "
              f"x{len(cfg.notify.urls)} urls")
    for inst in cfg.instruments:
        name = f"turtle_{inst.code.replace('.', '_')}"
        stra = StraTurtle(name, inst, cfg.base, risk_mgr=risk_mgr,
                          notify=notifier)
        engine.add_cta_strategy(stra)
        print(f"[runLive] add strategy {name}: {inst.period} "
              f"{inst.system} units={inst.units} dir={inst.direction}")

    engine.commitConfig()
    engine.run(bAsync=True)

    svr = WtMonSvr()
    svr.run(port=args.port, bSync=False)
    print(f"[runLive] monitor at http://127.0.0.1:{args.port}/console")

    try:
        input('press enter key to exit\n')
    except (KeyboardInterrupt, EOFError):
        pass
    engine.release()


if __name__ == "__main__":
    main()
