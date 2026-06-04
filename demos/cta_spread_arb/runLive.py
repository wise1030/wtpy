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
    parser.add_argument('--experiment', type=str, default='default',
                        help='实验名 (对应 config/experiments/<name>.yaml)')
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

        # R2-B: 启动期静态自检 (失败即 exit 2, 避免带病启动)
        if not args.skip_preflight:
            preflight_run_or_exit(
                _PROJECT_ROOT,
                use_legacy=False,
                experiment=args.experiment,
                config_root=config_root,
            )

        straInfo = SpreadArbStrategy.from_experiment(
            name='spread_arb',
            experiment=args.experiment,
            config_root=config_root,
        )

    engine = WtEngine(EngineType.ET_CTA)
    engine.init('../common/', "config.yaml")

    writer = ArbIndexWriter()
    engine.set_writer(writer)

    engine.add_cta_strategy(straInfo)

    # 写入套利配置到输出目录
    writer.write_arb_config("spread_arb", straInfo.sub_strategies)

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
