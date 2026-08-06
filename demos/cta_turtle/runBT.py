"""海龟策略单品种回测
用法: python runBT.py --code SHFE.rb.HOT --start 202001010900 --end 202510151500
"""
import argparse

from wtpy import WtBtEngine, EngineType
from wtpy.apps import WtBtAnalyst

from StraTurtle import StraTurtle


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", type=str, required=True,
                    help="合约代码, e.g. SHFE.rb.HOT")
    ap.add_argument("--config", type=str, default="./config/turtle_config.yaml")
    ap.add_argument("--start", type=int, default=202001010900)
    ap.add_argument("--end", type=int, default=202510151500)
    ap.add_argument("--capital", type=float, default=5000000)
    ap.add_argument("--slippage", type=int, default=0)
    args = ap.parse_args()

    stra = StraTurtle.from_config(
        name=f"turtle_{args.code.replace('.', '_')}",
        code=args.code, config_path=args.config)

    engine = WtBtEngine(EngineType.ET_CTA)
    engine.init('../common/', "configbt.yaml")
    engine.configBacktest(args.start, args.end)
    engine.configBTStorage(mode="csv", path="../storage/")
    engine.commitBTConfig()
    engine.set_cta_strategy(stra, slippage=args.slippage)
    engine.run_backtest(bAsync=False)

    analyst = WtBtAnalyst()
    analyst.add_strategy(stra.name(), folder="./outputs_bt/",
                         init_capital=args.capital, rf=0.02,
                         annual_trading_days=240)
    analyst.run_new()
    engine.release_backtest()


if __name__ == "__main__":
    main()
