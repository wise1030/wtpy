"""海龟策略实盘运行器 (多品种多实例)

每品种一个 StraTurtle 实例 (CtaMocker 单主K限制), WtEngine.add_cta_strategy
天然支持多策略。GUI: WtMonSvr console + notifier 推送, 图表数据由策略内
register_index/set_index_value/add_chart_mark 喂给 generated/ 供监控读取。

用法: python runLive.py [--config ./config/turtle_config.yaml] [--port 8080]
"""
import argparse
import os

from wtpy import WtEngine, EngineType
from wtpy.monitor import WtMonSvr

from StraTurtle import StraTurtle
from helpers.ConfigLoader import ConfigLoader

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=str,
                    default=os.path.join(HERE, "config", "turtle_config.yaml"))
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    cfg = ConfigLoader.load(args.config)

    engine = WtEngine(EngineType.ET_CTA)
    engine.init('../common/', "config.yaml")

    for inst in cfg.instruments:
        name = f"turtle_{inst.code.replace('.', '_')}"
        stra = StraTurtle(name, inst, cfg.base)
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
