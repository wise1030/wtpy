"""套利策略生产入口 (WatchDog 拉起, 引擎-only, 无监控进程)

与 runLive.py (开发/手工模式) 的区别:
- 无 WtMonSvr: 监控由独立进程 demos/runMonSvr.py 提供 (多组合共管)
- 无 input(): WatchDog 以 Popen 拉起, 无 tty, input 会立即 EOF 退出,
  改用 while-sleep 保活 (标准 WT 部署模式, 见 demos/cta_fut/run.py)
- 目录自包含: 策略代码/配置均在本组合目录内 (由 make_deploy.py 同步)
- 多实验: --experiment 逗号分隔多实例 (如 l_pp_2609,rb_hc_2601),
  每个实验一个 SpreadArbStrategy 实例, 复刻 runLive.py 的多实例构造

事件推送: config.yaml 的 notifier.url 与组合注册的 mqurl 完全一致
(默认 ipc:///tmp/wt_spread_arb.ipc), WatchDog 据此接收订单/成交/日志事件。
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
for _sub in ('strategies', 'helpers', 'factors'):
    _sub_path = os.path.join(HERE, _sub)
    if _sub_path not in sys.path:
        sys.path.insert(0, _sub_path)

from wtpy import WtEngine, EngineType

from SpreadArbStrategy import SpreadArbStrategy
from ArbIndexWriter import ArbIndexWriter
from preflight import run_or_exit as preflight_run_or_exit


def parse_args():
    parser = argparse.ArgumentParser(description='CTA Spread Arb Live (WatchDog)')
    parser.add_argument('--experiment', type=str, default='default',
                        help='实验名, 逗号分隔多实例 (如 l_pp_2609,rb_hc_2601)')
    parser.add_argument('--config-root', type=str, default=None,
                        help='config目录路径, 默认本目录下 config/')
    parser.add_argument('--skip-preflight', action='store_true',
                        help='跳过启动期 preflight 静态自检 (不推荐, 仅用于排障)')
    return parser.parse_args()


def main():
    args = parse_args()

    config_dir = os.path.join(HERE, 'config')
    config_root = os.path.abspath(args.config_root) if args.config_root else config_dir

    # 多实验多实例部署
    experiments = [e.strip() for e in args.experiment.split(',') if e.strip()]
    multi = len(experiments) > 1

    straInfos = []
    for exp in experiments:
        # 单实例保持历史命名 'spread_arb'; 多实例加实验名后缀保证唯一
        stra_name = 'spread_arb' if not multi else f'spread_arb_{exp}'

        # 启动期静态自检 (失败即 exit 2, 避免带病启动)
        if not args.skip_preflight:
            preflight_run_or_exit(
                HERE,
                use_legacy=False,
                experiment=exp,
                config_root=config_root,
            )

        stra = SpreadArbStrategy.from_experiment(
            name=stra_name,
            experiment=exp,
            config_root=config_root,
        )
        straInfos.append((stra_name, stra))
    if multi:
        print(f"[run] 多实例模式: {[n for n, _ in straInfos]}")

    engine = WtEngine(EngineType.ET_CTA)
    engine.init(HERE, "config.yaml")

    writer = ArbIndexWriter()
    engine.set_writer(writer)

    for stra_name, stra in straInfos:
        engine.add_cta_strategy(stra)
    # 多策略合并写 marker.json
    writer.write_arb_config_multi(
        [(name, stra.sub_strategies) for name, stra in straInfos])

    engine.commitConfig()
    engine.run(bAsync=True)
    print("[run] engine running, press ctrl-c to exit")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass

    # 优雅退出: 关闭文件句柄
    writer.close()
    engine.release()


if __name__ == "__main__":
    main()
