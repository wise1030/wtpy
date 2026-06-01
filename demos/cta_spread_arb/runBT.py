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
    parser = argparse.ArgumentParser(description='CTA Spread Arb Backtest - rb2510 vs hc2510')
    parser.add_argument('--start', type=int, default=202502030900, help='回测起始 YYYYMMDDHHMM')
    parser.add_argument('--end', type=int, default=202512311500, help='回测结束 YYYYMMDDHHMM')
    parser.add_argument('--capital', type=float, default=500000, help='初始资金')
    parser.add_argument('--slippage', type=int, default=0, help='滑点')
    parser.add_argument('--config', type=str, default=None, help='strategy_config路径')
    parser.add_argument('--factor-config', type=str, default=None, help='factor_config路径')
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    engine = WtBtEngine(EngineType.ET_CTA)
    # 使用本项目本地的 common 和 storage
    engine.init('./common/', "configbt.yaml")
    engine.configBacktest(args.start, args.end)
    engine.configBTStorage(mode="csv", path="./storage/")
    engine.commitBTConfig()

    config_dir = os.path.join(os.path.dirname(__file__), 'config')
    stra_cfg = args.config or os.path.join(config_dir, 'strategy_config.yaml')
    fact_cfg = args.factor_config or os.path.join(config_dir, 'factor_config.yaml')

    straInfo = SpreadArbStrategy(
        name='spread_arb_rbhc',
        strategy_config_path=stra_cfg,
        factor_config_path=fact_cfg,
        is_backtest=True
    )
    engine.set_cta_strategy(straInfo, slippage=args.slippage)

    engine.run_backtest(bAsync=False)

    analyst = WtBtAnalyst()
    analyst.add_strategy("spread_arb_rbhc", folder="./outputs_bt/",
                         init_capital=args.capital, rf=0.02, annual_trading_days=240)
    analyst.run_new()

    engine.release_backtest()
