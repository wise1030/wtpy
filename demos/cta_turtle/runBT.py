"""海龟策略单品种回测
用法: python runBT.py --code SHFE.rb.HOT --start 202001010900 --end 202510151500
"""
import argparse
import os

from wtpy import WtBtEngine, EngineType
from wtpy.apps import WtBtAnalyst

from StraTurtle import StraTurtle

HERE = os.path.dirname(os.path.abspath(__file__))


def _precheck(code: str):
    """数据预检 (warn-only): 缺数据给出明确提示而非 C++ 段错误"""
    try:
        from helpers.ConfigLoader import ConfigLoader
        from helpers import DataChecker
        cfg = ConfigLoader.load("./config/turtle_config.yaml")
        inst = next((i for i in cfg.instruments if i.code == code), None)
        if inst is None:
            return
        common = os.path.abspath(os.path.join(HERE, "..", "common"))
        storage = os.path.abspath(os.path.join(HERE, "..", "storage"))
        ok, _, _n = DataChecker.check_hots(code, common)
        if not ok:
            print(f"[precheck][ERROR] {code}: hots.json 缺键 → "
                  f"python3 scripts/prepare_data.py --fix-hots")
        issues = [i for i in
                  DataChecker.check_bars(code, inst.period, storage)
                  if i.level != "info"]
        for i in issues:
            print(f"[precheck] {i}")
    except Exception as e:
        print(f"[precheck] skipped: {e}")


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

    _precheck(args.code)

    # bt 日线 trigger 退化判定: m5 数据缺失时 tick 模拟链断裂
    # (simTicks 跳过日K), 通知策略改走 check_bar+收盘撮合口径
    m5_csv = os.path.join(HERE, "..", "storage", "csv",
                          f"{args.code}_m5.csv")
    bt_no_simtick = not os.path.exists(m5_csv)

    stra = StraTurtle.from_config(
        name=f"turtle_{args.code.replace('.', '_')}",
        code=args.code, config_path=args.config,
        bt_no_simtick=bt_no_simtick)
    if bt_no_simtick:
        print(f"[runBT] {args.code} 无 m5 数据, trigger 退化为 "
              f"check_bar触发+收盘撮合口径")

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
