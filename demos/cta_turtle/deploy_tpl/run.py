"""海龟策略生产入口 (WatchDog 拉起, 引擎-only, 无监控进程)

与 runLive.py (开发/手工模式) 的区别:
- 无 WtMonSvr: 监控由独立进程 runMonSvr.py 提供
- 无 input(): WatchDog 以 Popen 拉起, 无 tty, input 会立即 EOF 退出,
  改用 while-sleep 保活 (标准 WT 部署模式, 见 demos/cta_fut/run.py)
- 目录自包含: 策略代码/配置均在本组合目录内 (由 make_deploy.py 同步)

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


def main():
    cfg = ConfigLoader.load(os.path.join(HERE, 'config', 'turtle_config.yaml'))

    engine = WtEngine(EngineType.ET_CTA)

    # 注册 CSV 数据加载器 (绕过 libWtDataStorage.so DSB segfault bug)
    # config.yaml 中 storage path 是绝对路径, csv 子目录即数据源
    import yaml as _yaml
    with open(os.path.join(HERE, 'config.yaml'), encoding='utf-8') as _f:
        _cfg = _yaml.safe_load(_f)
    _storage_path = _cfg.get('data', {}).get('store', {}).get('path', '')
    csv_dir = os.path.join(_storage_path, 'csv')
    if os.path.isdir(csv_dir):
        engine.set_extended_data_loader(CsvDataLoader(csv_dir))
        print(f"[turtle] CSV data loader registered: {csv_dir}")
    else:
        print(f"[turtle] WARNING: CSV dir not found: {csv_dir}")

    # folder 参数在未传文件名单时被忽略(WtEngine.py:170-186),
    # basefiles/storage 路径由 config.yaml 决定 — make_deploy.py 已重写为绝对路径
    engine.init(HERE, os.path.join(HERE, 'config.yaml'))

    for inst in cfg.instruments:
        name = f"turtle_{inst.code.replace('.', '_')}"
        stra = StraTurtle(name, inst, cfg.base)
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
