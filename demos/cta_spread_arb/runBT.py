from wtpy import WtBtEngine, EngineType
from wtpy.apps import WtBtAnalyst

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


def parse_args():
    parser = argparse.ArgumentParser(description='CTA Spread Arb Backtest - rb/hc cross-commodity')
    # v9: --experiment 替代 --config + --factor-config
    parser.add_argument('--experiment', type=str, default='default',
                        help='实验名 (对应 config/experiments/<name>.yaml), v9 分层配置入口')
    parser.add_argument('--config', type=str, default=None,
                        help='[v8.1 legacy] strategy_config路径, 显式提供时走旧路径')
    parser.add_argument('--factor-config', type=str, default=None,
                        help='[v8.1 legacy] factor_config路径, 需与 --config 配合')
    parser.add_argument('--start', type=int, default=None,
                        help='回测起始 YYYYMMDDHHMM (留空则读 experiment yaml)')
    parser.add_argument('--end', type=int, default=None,
                        help='回测结束 YYYYMMDDHHMM (留空则读 experiment yaml)')
    parser.add_argument('--capital', type=float, default=500000, help='初始资金')
    parser.add_argument('--slippage', type=int, default=0, help='滑点')
    parser.add_argument('--config-root', type=str, default=None,
                        help='[v9] config目录路径；默认使用项目根 config/。grid回测用隔离配置目录')
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    # === 决定配置加载方式 ===
    config_dir = os.path.abspath(args.config_root) if args.config_root else os.path.join(_PROJECT_ROOT, 'config')
    use_legacy = args.config is not None  # 显式提供 --config → v8.1 老路径

    # === 回测时段 + capital + slippage: CLI > experiment yaml > 默认 ===
    capital_from_cli = '--capital' in sys.argv
    if use_legacy:
        start = args.start or 202502030900
        end = args.end or 202512311500
    else:
        if args.start is not None or args.end is not None:
            start = args.start or 202502030900
            end = args.end or 202512311500
        else:
            import yaml
            exp_path = os.path.join(config_dir, 'experiments', f'{args.experiment}.yaml')
            with open(exp_path, 'r', encoding='utf-8') as f:
                exp_cfg = yaml.safe_load(f) or {}
            bt_meta = exp_cfg.get('backtest', {})
            start = bt_meta.get('start', 202502030900)
            end = bt_meta.get('end', 202512311500)
            if args.slippage == 0 and 'slippage' in bt_meta:
                args.slippage = bt_meta['slippage']
            if not capital_from_cli and 'capital' in bt_meta:
                args.capital = bt_meta['capital']

    engine = WtBtEngine(EngineType.ET_CTA)
    engine.init('./common/', "configbt.yaml")
    engine.configBacktest(start, end)
    engine.configBTStorage(mode="csv", path="./storage/")
    engine.commitBTConfig()

    # === 创建策略实例 ===
    if use_legacy:
        stra_cfg = args.config or os.path.join(config_dir, 'strategy_config.yaml')
        fact_cfg = args.factor_config or os.path.join(config_dir, 'factor_config.yaml')
        straInfo = SpreadArbStrategy(
            name='spread_arb_rbhc',
            strategy_config_path=stra_cfg,
            factor_config_path=fact_cfg,
            is_backtest=True
        )
    else:
        straInfo = SpreadArbStrategy.from_experiment(
            name='spread_arb_rbhc',
            experiment=args.experiment,
            config_root=config_dir,
            is_backtest=True,
        )

    engine.set_cta_strategy(straInfo, slippage=args.slippage)
    engine.run_backtest(bAsync=False)

    analyst = WtBtAnalyst()
    analyst.add_strategy("spread_arb_rbhc", folder="./outputs_bt/",
                         init_capital=args.capital, rf=0.02, annual_trading_days=240)
    analyst.run_new()

    engine.release_backtest()
