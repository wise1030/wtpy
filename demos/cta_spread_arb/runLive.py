from wtpy import WtEngine, EngineType
from wtpy.monitor import WtMonSvr
import yaml
import sys
import os
import argparse

# 统一设置项目根目录到sys.path，子模块不再需要自行sys.path.insert
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
for _sub in ('strategies', 'helpers', 'factors', 'reporter'):
    _sub_path = os.path.join(_PROJECT_ROOT, _sub)
    if _sub_path not in sys.path:
        sys.path.insert(0, _sub_path)

from SpreadArbStrategy import SpreadArbStrategy
from ArbIndexWriter import ArbIndexWriter
from ArbDataReporter import ArbDataReporter


def load_sub_names(strategy_config_path: str) -> list:
    """从strategy_config.yaml动态读取策略名称"""
    with open(strategy_config_path, 'r') as f:
        cfg = yaml.full_load(f)
    return [s['name'] for s in cfg.get('strategies', [])]


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
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    config_dir = os.path.join(os.path.dirname(__file__), 'config')
    use_legacy = args.config is not None

    if use_legacy:
        strategy_config_path = args.config or os.path.join(config_dir, 'strategy_config.yaml')
        factor_config_path = args.factor_config or os.path.join(config_dir, 'factor_config.yaml')
        sub_names = load_sub_names(strategy_config_path)

        straInfo = SpreadArbStrategy(
            name='spread_arb',
            strategy_config_path=strategy_config_path,
            factor_config_path=factor_config_path
        )
    else:
        config_root = os.path.abspath(args.config_root) if args.config_root else config_dir
        straInfo = SpreadArbStrategy.from_experiment(
            name='spread_arb',
            experiment=args.experiment,
            config_root=config_root,
        )
        sub_names = [straInfo.core.name]

    engine = WtEngine(EngineType.ET_CTA)
    engine.init('../common/', "config.yaml")

    writer = ArbIndexWriter()
    engine.set_writer(writer)

    reporter = ArbDataReporter("spread_arb", sub_names)
    engine.set_data_reporter(reporter)

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
