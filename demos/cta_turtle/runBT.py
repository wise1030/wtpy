"""海龟策略单品种回测
用法: python runBT.py --code SHFE.rb.HOT --start 202001010900 --end 202510151500
"""
import argparse
import csv
import json
import math
import os

from wtpy import WtBtEngine, EngineType
from wtpy.apps import WtBtAnalyst

from StraTurtle import StraTurtle

HERE = os.path.dirname(os.path.abspath(__file__))


def _precheck(code: str, config_path: str = "./config/turtle_config.yaml"):
    """数据预检 (warn-only): 缺数据给出明确提示而非 C++ 段错误"""
    try:
        from core.ConfigLoader import ConfigLoader
        from core import DataChecker
        # 2026-10-20: 必须用实际 --config, 旧实现永远读默认配置,
        # 自定义配置/临时 walk-forward 配置的预检结果无意义。
        cfg = ConfigLoader.load(config_path, warn=False)
        inst = next((i for i in cfg.instruments if i.code == code), None)
        if inst is None:
            return
        common = os.path.abspath(os.path.join(HERE, "..", "common"))
        storage = os.path.abspath(os.path.join(HERE, "..", "storage"))
        ok, _, _n = DataChecker.check_hots(code, common)
        if not ok:
            print(f"[precheck][ERROR] {code}: hots.json 缺键 → "
                  f"python3 scripts/prepare_data.py --fix-hots")
        # h1 一期: 策略层 m5→h1 聚合, h1 无独立数据文件, 预检 m5
        eff_period = "m5" if inst.period == "h1" else inst.period
        issues = [i for i in
                  DataChecker.check_bars(code, eff_period, storage)
                  if i.level != "info"]
        for i in issues:
            print(f"[precheck] {i}")
    except Exception as e:
        print(f"[precheck] skipped: {e}")




def _fallback_summary(name: str, capital: float, folder: str = None,
                      rf: float = 0.02) -> bool:
    """WtBtAnalyst 失败时的最小 summary 兜底 (2026-10-20)。

    只依赖 mocker 落盘的 funds.csv (date,closeprofit,positionprofit,
    dynbalance,fee), 保证 run_matrix/walkforward/slippage 仍能收集结果。
    注意: win_rate 无法从 funds 还原, 写 0.0 并标记 fallback=True;
    Excel 报表仍以 WtBtAnalyst 为准。
    """
    folder = folder or os.path.join(HERE, "outputs_bt", name)
    path = os.path.join(folder, "funds.csv")
    if not os.path.exists(path) or capital <= 0:
        return False
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    rows.append((int(r["date"]), float(r["dynbalance"])))
                except (KeyError, TypeError, ValueError):
                    continue
    except OSError:
        return False
    if len(rows) < 2:
        return False
    pnl = [r[1] for r in rows]
    equity = [capital + x for x in pnl]
    total = pnl[-1] / capital
    days = len(rows)
    years = max(days / 240.0, 1e-9)
    annual = (1.0 + total) ** (1.0 / years) - 1.0 \
        if 1.0 + total > 0 else -1.0
    peak = equity[0]
    max_dd = 0.0
    for x in equity:
        peak = max(peak, x)
        if peak > 0:
            max_dd = max(max_dd, (peak - x) / peak)
    rets = [(equity[i] - equity[i - 1]) / equity[i - 1]
            for i in range(1, len(equity)) if equity[i - 1] > 0]
    mean = sum(rets) / len(rets) if rets else 0.0
    var = sum((x - mean) ** 2 for x in rets) / len(rets) if rets else 0.0
    std = math.sqrt(var)
    sharpe = (mean * 240 - rf) / (std * math.sqrt(240)) if std > 0 else 0.0
    down = [x for x in rets if x < 0]
    dstd = math.sqrt(sum(x * x for x in down) / len(down)) if down else 0.0
    sortino = (mean * 240 - rf) / (dstd * math.sqrt(240)) if dstd > 0 else 0.0
    summary = {
        "capital": capital, "days": days,
        "total_return": total * 100.0,
        "annual_return": annual * 100.0,
        "win_rate": 0.0,
        "max_falldown": max_dd * 100.0,
        "max_profratio": 0.0,
        "std": std * math.sqrt(240) * 100.0,
        "down_std": dstd * math.sqrt(240) * 100.0,
        "sharpe_ratio": sharpe, "sortino_ratio": sortino,
        "calmar_ratio": annual / max_dd if max_dd > 0 else 0.0,
        "name": name, "fallback": True,
    }
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "summary.json"), "w",
              encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=4)
    return True

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

    _precheck(args.code, args.config)

    # bt 日线 trigger 退化判定: m5 数据缺失时 tick 模拟链断裂
    # (simTicks 跳过日K), 通知策略改走 check_bar+收盘撮合口径。
    # 2026-10-20: 同时认 DSB/CSV; 旧实现只看 storage/csv 会误判。
    from core import DataChecker
    storage = os.path.abspath(os.path.join(HERE, "..", "storage"))
    bt_no_simtick = not DataChecker.has_bars(args.code, "m5", storage)

    # --capital 同时作为 RiskManager 权益基数 (2026-10-20 口径统一):
    # 旧实现报告分母=5M、风控基数=fallback_capital(15M), 手数被放大 3 倍。
    stra = StraTurtle.from_config(
        name=f"turtle_{args.code.replace('.', '_')}",
        code=args.code, config_path=args.config,
        bt_no_simtick=bt_no_simtick, capital=args.capital)
    print(f"[runBT] risk capital={args.capital:,.0f} "
          f"(= --capital; fallback_capital overridden)")
    if bt_no_simtick:
        print(f"[runBT] {args.code} 无 m5 数据 (CSV/DSB 均无), "
              f"trigger 退化为 check_bar触发+收盘撮合口径")

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
    try:
        analyst.run_new()
    except Exception as e:
        print(f"[runBT] WARN WtBtAnalyst failed: {e}")
        if _fallback_summary(stra.name(), args.capital):
            print("[runBT] fallback summary written from funds.csv "
                  "(win_rate=0, fallback=True)")
        else:
            print("[runBT] fallback summary skipped: funds.csv 不可用")
    finally:
        engine.release_backtest()


if __name__ == "__main__":
    main()
