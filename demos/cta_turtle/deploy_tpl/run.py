"""海龟策略生产入口 (WatchDog 拉起, 引擎-only, 无监控进程)

与 runLive.py (开发/手工模式) 的区别:
- 无 WtMonSvr: 监控由独立进程 runMonSvr.py 提供
- 无 input(): WatchDog 以 Popen 拉起, 无 tty, input 会立即 EOF 退出,
  改用 while-sleep 保活 (标准 WT 部署模式, 见 demos/cta_fut/run.py)
- 目录自包含: 策略代码/配置均在本组合目录内 (由 make_deploy.py 同步)

启动顺序 (数据完整性保障, 见 docs/DATA_OPS.md):
  0) 合约信息更新 (WT_CONTRACT_SYNC=0 关闭, 默认开): CTPLoader 从
     CTP 交易前置拉取最新 contracts/commodities.json (当日幂等+
     过期合约剔除), 凭据自动取同目录 tdtraders.yaml;
  1) journal 自动回补 (WT_AUTO_REPAIR=1): 旁路日志尾部合并回
     storage/csv — 崩溃窗口内未转存的K线由此恢复;
  2) 数据预检门禁 (WT_PREFLIGHT=warn|strict|off, 默认 warn):
     hots键/K线健康/暖机深度检查, strict 时 error 阻断退出
     (WatchDog guard 会按 span 重试, 数据修复后自动通过);
  3) 引擎初始化: CsvDataLoader 从 storage/csv 提供暖机历史。

事件推送: config.yaml 的 notifier.url 与监控侧注册的组合 mqurl
完全一致 (ipc:///tmp/wt_turtle.ipc), WatchDog 据此接收订单/成交/日志事件。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.join(HERE, 'helpers')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from wtpy import WtEngine, EngineType

from StraTurtle import StraTurtle
from helpers.ConfigLoader import ConfigLoader
from helpers.CsvDataLoader import CsvDataLoader


def _storage_csv_dir() -> str:
    import yaml as _yaml
    with open(os.path.join(HERE, 'config.yaml'), encoding='utf-8') as _f:
        _cfg = _yaml.safe_load(_f)
    _storage_path = _cfg.get('data', {}).get('store', {}).get('path', '')
    return os.path.join(_storage_path, 'csv')


def _auto_repair(cfg, csv_dir: str):
    """journal → storage/csv 增量回补 (幂等; 兜底宕机丢失的会话内K线)"""
    try:
        from helpers.BarJournal import BarJournal
        jdir = getattr(cfg.base, 'journal_dir', '')
        if not jdir or not os.path.isdir(jdir):
            print(f"[turtle] journal off/absent: {jdir}")
            return
        bj = BarJournal(jdir)
        suffix_map = {"d1": "d", "m5": "m5", "m1": "m1"}
        total = 0
        for inst in cfg.instruments:
            periods = {inst.period}
            if inst.intrabar_stop:
                periods.add(getattr(cfg.base, 'intrabar_period', 'm5'))
            for period in sorted(periods):
                sfx = suffix_map.get(period, period)
                n = bj.merge_to_csv(
                    inst.code, period,
                    os.path.join(csv_dir, f"{inst.code}_{sfx}.csv"))
                total += n
        print(f"[turtle] journal repair: +{total} bars -> {csv_dir}")
    except Exception as e:
        print(f"[turtle] journal repair skipped: {e}")


def _preflight(cfg, csv_dir: str):
    """数据门禁: hots键/K线健康/暖机深度。strict 模式 error 即退出,
    WatchDog 按 span 拉起重试 — 形成"数据未就绪不交易"的自愈闭环。"""
    mode = os.environ.get('WT_PREFLIGHT', 'warn').lower()
    if mode not in ('warn', 'strict'):
        return
    try:
        from helpers import DataChecker
        common = _common_dir()
        storage = os.path.abspath(os.path.join(csv_dir, '..'))
        issues = DataChecker.check_config(cfg, common, storage,
                                          live=True)
        n_err = 0
        for i in issues:
            if i.level == 'info':
                continue
            print(f"[turtle][{i.level.upper()}] {i.code}: {i.msg}")
            if i.level == 'error':
                n_err += 1
        if mode == 'strict' and n_err:
            raise SystemExit(
                f"[turtle] {n_err} data errors under WT_PREFLIGHT=strict, "
                f"exiting for watchdog retry")
    except SystemExit:
        raise
    except Exception as e:
        print(f"[turtle] preflight skipped: {e}")


def _common_dir() -> str:
    """basefiles 目录: 优先 WT_COMMON_DIR, 否则读 config.yaml
    (make_deploy 已重写为绝对路径)"""
    env = os.environ.get('WT_COMMON_DIR')
    if env:
        return env
    import yaml as _yaml
    with open(os.path.join(HERE, 'config.yaml'), encoding='utf-8') as _f:
        _cfg = _yaml.safe_load(_f)
    return os.path.dirname(_cfg.get('basefiles', {})
                           .get('contract', '../common/contracts.json'))


def _sync_contracts(common: str):
    """每日合约信息更新 (WT_CONTRACT_SYNC=0 可关, 默认开)。

    scripts/update_contracts.py 内部做当日幂等(mtime 判定),
    WatchDog 频繁重启不会重复请求 CTP; 凭据自动取同目录
    tdtraders.yaml (traders[0]); 失败仅告警, 旧文件兜底。"""
    script = os.path.join(HERE, 'scripts', 'update_contracts.py')
    cred = os.path.join(HERE, 'tdtraders.yaml')
    if not os.path.exists(script):
        print("[turtle] contract sync skipped: script missing")
        return
    cmd = [sys.executable, script, '--common', common]
    if os.path.exists(cred):
        cmd += ['--cred', cred]
    import subprocess
    try:
        r = subprocess.run(cmd, timeout=660)
        print(f"[turtle] contract sync rc={r.returncode}")
    except Exception as e:
        print(f"[turtle] contract sync failed (non-fatal): {e}")


def main():
    cfg_path = os.path.join(HERE, 'config', 'turtle_config.yaml')
    cfg = ConfigLoader.load(cfg_path)

    # === 启动顺序: 合约更新 → 回补 → 预检 → 引擎 ===
    if os.environ.get('WT_CONTRACT_SYNC', '1') != '0':
        _sync_contracts(_common_dir())
    csv_dir = _storage_csv_dir()
    if os.environ.get('WT_AUTO_REPAIR', '0') == '1':
        _auto_repair(cfg, csv_dir)
    _preflight(cfg, csv_dir)

    engine = WtEngine(EngineType.ET_CTA)

    # 注册 CSV 数据加载器 (绕过 libWtDataStorage.so DSB segfault bug)
    # config.yaml 中 storage path 是绝对路径, csv 子目录即数据源
    # stop_tdate=当日: his/rt 交易日分区律 — csv 暖机不含当日, 当日 bar 走 rt
    # (夜盘启动时自然日≠交易日属保守过滤, 当日日盘K由会话轮转后的 his 提供)
    if os.path.isdir(csv_dir):
        import datetime as _dt
        stop = int(_dt.datetime.now().strftime("%Y%m%d"))
        engine.set_extended_data_loader(CsvDataLoader(csv_dir,
                                                      stop_tdate=stop))
        print(f"[turtle] CSV data loader registered: {csv_dir} "
              f"(stop_tdate={stop})")
    else:
        print(f"[turtle] WARNING: CSV dir not found: {csv_dir}")

    # folder 参数在未传文件名单时被忽略(WtEngine.py:170-186),
    # basefiles/storage 路径由 config.yaml 决定 — make_deploy.py 已重写为绝对路径
    engine.init(HERE, os.path.join(HERE, 'config.yaml'))

    # 组合级共享: 风控账本 + webhook 推送 (多品种协同必需)
    from helpers.RiskManager import RiskManager
    risk_mgr = RiskManager(cfg.risk)
    notifier = None
    if getattr(cfg, 'notify', None) and cfg.notify.enabled:
        try:
            from helpers.notifier import WebhookNotifier
            notifier = WebhookNotifier(urls=cfg.notify.urls,
                                       wtype=cfg.notify.type,
                                       cooldown=cfg.notify.cooldown)
        except Exception as e:
            print(f"[turtle] notifier init failed: {e}")

    for inst in cfg.instruments:
        name = f"turtle_{inst.code.replace('.', '_')}"
        stra = StraTurtle(name, inst, cfg.base, risk_mgr=risk_mgr,
                          notify=notifier)
        engine.add_cta_strategy(stra)
        print(f"[turtle] add strategy {name}: {inst.period} "
              f"{inst.system} units={inst.units} dir={inst.direction}")

    engine.commitConfig()
    engine.run(bAsync=True)
    print("[turtle] engine running, press ctrl-c to exit")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    engine.release()


if __name__ == "__main__":
    main()
