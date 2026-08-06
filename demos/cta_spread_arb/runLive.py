from wtpy import WtEngine, EngineType
from wtpy.monitor import WtMonSvr
import sys
import os
import argparse

# 统一设置项目根目录到sys.path，子模块不再需要自行sys.path.insert
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
for _sub in ('strategies', 'helpers', 'factors'):
    _sub_path = os.path.join(_PROJECT_ROOT, _sub)
    if _sub_path not in sys.path:
        sys.path.insert(0, _sub_path)

from SpreadArbStrategy import SpreadArbStrategy
from ArbIndexWriter import ArbIndexWriter
from preflight import run_or_exit as preflight_run_or_exit


def parse_args():
    parser = argparse.ArgumentParser(description='CTA Spread Arb Live Trading')
    parser.add_argument('--config', type=str, default=None, help='strategy_config路径')
    parser.add_argument('--factor-config', type=str, default=None, help='factor_config路径')
    # v9: --experiment 分层配置入口
    # A-4(2026-07): 支持逗号分隔多实验, 每个实验一个策略实例(多品种组合=
    # 多策略实例, 兑现 v10 架构声明; 策略名必须唯一)
    parser.add_argument('--experiment', type=str, default='default',
                        help='实验名, 逗号分隔多实例 (如 l_pp_2609,rb_hc_2601)')
    parser.add_argument('--config-root', type=str, default=None,
                        help='config目录路径')
    parser.add_argument('--port', type=int, default=8080, help='监控端口')
    parser.add_argument('--skip-preflight', action='store_true',
                        help='跳过启动期 preflight 静态自检 (不推荐, 仅用于排障)')
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    config_dir = os.path.join(os.path.dirname(__file__), 'config')
    use_legacy = args.config is not None

    if use_legacy:
        strategy_config_path = args.config or os.path.join(config_dir, 'strategy_config.yaml')
        factor_config_path = args.factor_config or os.path.join(config_dir, 'factor_config.yaml')

        # R2-B: 启动期静态自检 (失败即 exit 2, 避免带病启动)
        if not args.skip_preflight:
            preflight_run_or_exit(
                _PROJECT_ROOT,
                use_legacy=True,
                strategy_config_path=strategy_config_path,
                factor_config_path=factor_config_path,
            )

        straInfo = SpreadArbStrategy(
            name='spread_arb',
            strategy_config_path=strategy_config_path,
            factor_config_path=factor_config_path
        )
    else:
        config_root = os.path.abspath(args.config_root) if args.config_root else config_dir

        # A-4: 多实验多实例部署
        experiments = [e.strip() for e in args.experiment.split(',') if e.strip()]
        multi = len(experiments) > 1

        straInfos = []
        for exp in experiments:
            # 单实例保持历史命名 'spread_arb'; 多实例加实验名后缀保证唯一
            stra_name = 'spread_arb' if not multi else f'spread_arb_{exp}'

            # R2-B: 启动期静态自检 (失败即 exit 2, 避免带病启动)
            if not args.skip_preflight:
                preflight_run_or_exit(
                    _PROJECT_ROOT,
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
            print(f"[runLive] 多实例模式: {[n for n, _ in straInfos]}")

    engine = WtEngine(EngineType.ET_CTA)
    engine.init('../common/', "config.yaml")

    writer = ArbIndexWriter()
    engine.set_writer(writer)

    if use_legacy:
        engine.add_cta_strategy(straInfo)
        writer.write_arb_config("spread_arb", straInfo.sub_strategies)
    else:
        for stra_name, stra in straInfos:
            engine.add_cta_strategy(stra)
        # A-4: 多策略合并写 marker.json
        writer.write_arb_config_multi(
            [(name, stra.sub_strategies) for name, stra in straInfos])

    engine.commitConfig()
    engine.run(bAsync=True)

    svr = WtMonSvr()
    svr.run(port=args.port, bSync=False)

    try:
        input('press enter key to exit\n')
    except (KeyboardInterrupt, EOFError):
        pass

    # 优雅退出: 关闭文件句柄
    writer.close()
    engine.release()
