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
from core.ConfigLoader import ConfigLoader, register_allocated_capital
from core.RiskManager import RiskManager
from core.profiles import resolve_profiles, describe_profiles

HERE = os.path.dirname(os.path.abspath(__file__))


def _build_notifier(cfg):
    """按 notify 配置段构建 webhook 推送器 (未启用返回 None)"""
    if not getattr(cfg, "notify", None) or not cfg.notify.enabled:
        return None
    try:
        from core.notifier import WebhookNotifier
        return WebhookNotifier(urls=cfg.notify.urls,
                               wtype=cfg.notify.type,
                               cooldown=cfg.notify.cooldown)
    except Exception as e:
        print(f"[runLive] notifier init failed: {e}")
        return None


def _resolve_preflight_mode(value) -> str:
    """解析 WT_PREFLIGHT; 仅缺省值回退 warn, 非法值不得关闭门禁。"""
    if value is None:
        return "warn"
    if not isinstance(value, str):
        raise ValueError(f"invalid WT_PREFLIGHT={value!r}; "
                         "expected off, warn or strict")
    mode = value.strip().lower()
    if mode not in ("off", "warn", "strict"):
        raise ValueError(f"invalid WT_PREFLIGHT={value!r}; "
                         "expected off, warn or strict")
    return mode


def _preflight_failure(mode: str, stage: str, exc: Exception):
    """纯失败策略: 返回 (是否阻断, 说明), 不执行日志或退出操作。"""
    if mode not in ("warn", "strict"):
        raise ValueError(f"no preflight failure policy for mode {mode!r}")
    abort = mode == "strict"
    action = "refusing startup" if abort else "continuing with incomplete checks"
    return abort, (f"{stage} failed: {exc}; WT_PREFLIGHT={mode}, "
                   f"{action}")


def _handle_preflight_failure(mode: str, stage: str, exc: Exception):
    abort, message = _preflight_failure(mode, stage, exc)
    if abort:
        raise SystemExit(f"[preflight] FATAL: {message}") from exc
    print(f"[preflight] WARNING: {message}", file=sys.stderr)


def _run_preflight_step(mode: str, stage: str, operation):
    """执行一个预检阶段; SystemExit 永远原样向上传递。"""
    try:
        return operation()
    except SystemExit:
        raise
    except Exception as exc:
        _handle_preflight_failure(mode, stage, exc)
        return None


def _preflight(cfg, mode: str = "warn", cfg_path: str = ""):
    """启动前数据完整性门禁: hots键/K线健康/暖机深度 (+journal自动回补)

    off 跳过全部预检; warn 降级时输出醒目警告; strict 任一阶段异常即
    阻断启动。cfg_path 是实际生效的配置文件路径, 回补子进程必须复用它。
    """
    mode = _resolve_preflight_mode(mode)
    if mode == "off":
        return
    try:
        def _load_checker():
            from core import DataChecker
            return DataChecker

        DataChecker = _run_preflight_step(
            mode, "import DataChecker", _load_checker)
        if DataChecker is None:  # warn 下导入失败, 后续检查无从执行
            return
        common = os.path.abspath(os.path.join(HERE, "..", "common"))
        storage = os.path.abspath(os.path.join(HERE, "..", "storage"))

        # 自动回补: journal 尾部合并到 storage/csv (WT_AUTO_REPAIR=1)
        jdir = getattr(cfg.base, "journal_dir", "")
        if os.environ.get("WT_AUTO_REPAIR") == "1" and jdir:
            def _repair():
                import subprocess as _sp
                r = _sp.run([sys.executable,
                             os.path.join(HERE, "scripts",
                                          "prepare_data.py"),
                             "--config", cfg_path or _cfg_path(),
                             "--common", common,
                             "--storage", storage,
                             "--repair-journal", jdir],
                            capture_output=True, text=True, timeout=120,
                            check=True)
                print(r.stdout.strip() or "[repair] no output")

            _run_preflight_step(mode, "automatic repair", _repair)

        issues = _run_preflight_step(
            mode, "check_config",
            lambda: DataChecker.check_config(cfg, common, storage, live=True))
        if issues is None:
            issues = []
        stale = _run_preflight_step(
            mode, "check_staleness",
            lambda: DataChecker.check_staleness(cfg, common, storage))
        if stale is not None:
            issues.extend(stale)

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
    except Exception as exc:
        _handle_preflight_failure(mode, "preflight", exc)


def _cfg_path():
    return os.path.join(HERE, "config", "turtle_config.yaml")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=str,
                    default=os.path.join(HERE, "config", "turtle_config.yaml"))
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    preflight_mode = _resolve_preflight_mode(os.environ.get("WT_PREFLIGHT"))
    cfg = ConfigLoader.load(args.config)

    # === 数据完整性预检 (P1 运维) ===
    # off=跳过; warn=异常醒目告警后继续; strict=异常或数据 error 阻断启动
    _preflight(cfg, mode=preflight_mode, cfg_path=args.config)

    engine = WtEngine(EngineType.ET_CTA)
    engine.init('../common/', "config.yaml")

    # 组合级风险预算: 多品种共享同一实例 (单位限制/总敞口跨品种生效)
    risk_mgr = RiskManager(cfg.risk)
    allocations = register_allocated_capital(risk_mgr, cfg)
    if allocations:
        print(f"[runLive] 资金分配: portfolio_capital="
              f"{cfg.risk.portfolio_capital:,.0f} → 每策略权益基数")
        for code, (_name, alloc) in allocations.items():
            print(f"[runLive]   {code}: {alloc:,.0f}")
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
              f"units={inst.units} dir={inst.direction} profiles="
              f"{describe_profiles(resolve_profiles(inst, cfg.base))}")

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
