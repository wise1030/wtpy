"""海龟策略全品种矩阵回测

WtBtEngine 单策略上下文约束(WtBtEngine.__context__ 为标量) →
每品种独立回测, 两种模式:
  serial:    进程内串行 (release_backtest 复用单例, 快)
  subprocess: 每品种独立子进程调 runBT.py (隔离, 稳)
汇总 outputs_bt/<stra>/summary.json → matrix_results/matrix_summary.csv

用法: python run_matrix.py --mode subprocess --start 202001010900 --end 202510151500
"""
import argparse
import csv
import json
import os
import subprocess
import sys

from helpers.ConfigLoader import ConfigLoader

HERE = os.path.dirname(os.path.abspath(__file__))


def stra_name(code: str) -> str:
    return f"turtle_{code.replace('.', '_')}"


def parse_weights(s: str) -> dict:
    """'rb:0.6,i:0.4' → {'SHFE.rb.HOT':0.6,...} 按代码后缀匹配"""
    out = {}
    for part in s.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        k, v = part.split(":", 1)
        try:
            out[k.strip()] = float(v)
        except ValueError:
            pass
    return out


def synthesize_portfolio(codes, args, out_dir) -> dict:
    """组合层权益合成 + 贡献分解 (P1-4); 失败返回 {} 不阻断"""
    from helpers import portfolio as pf
    curves = {}
    for c in codes:
        path = os.path.join(HERE, "outputs_bt", stra_name(c), "funds.csv")
        curve = pf.read_funds_curve(path)
        if curve:
            curves[c] = curve
        else:
            print(f"[matrix] WARN {c}: funds.csv 缺失/为空, 跳过合成")
    if not curves:
        return {}
    wmap = parse_weights(args.weights)
    dates, port_pnl, aligned = pf.merge_curves(curves, wmap or None)
    metrics = pf.portfolio_metrics(dates, port_pnl, args.capital,
                                   rf=args.rf)
    contrib = pf.contribution(aligned, port_pnl)

    os.makedirs(out_dir, exist_ok=True)
    eq_path = os.path.join(out_dir, "portfolio_equity.csv")
    with open(eq_path, "w", newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(["date", "equity", "pnl"] +
                   [f"pnl_{c}" for c in aligned])
        for i, d in enumerate(dates):
            w.writerow([d, round(args.capital + float(port_pnl[i]), 2),
                        round(float(port_pnl[i]), 2)] +
                       [round(float(a[i]), 2) for a in aligned.values()])
    summary = {"capital": args.capital,
               "weights": {c: round(_w_of(c, curves, wmap), 4)
                           for c in aligned},
               "metrics": metrics,
               "contribution": contrib}
    with open(os.path.join(out_dir, "portfolio_summary.json"), "w",
              encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"[matrix] portfolio equity -> {eq_path}")
    print(f"[matrix] portfolio metrics: {metrics}")
    print(f"[matrix] contribution: {contrib}")
    return summary


def _w_of(code, curves, wmap):
    tot = sum(wmap.get(c, 0.0) for c in curves)
    if wmap and tot > 0 and code in wmap:
        return wmap[code] / tot
    return 1.0 / max(1, len(curves))


def collect(row_code: str) -> dict:
    folder = os.path.join(HERE, "outputs_bt", stra_name(row_code))
    path = os.path.join(folder, "summary.json")
    if not os.path.exists(path):
        return {"code": row_code, "error": "no summary.json"}
    with open(path, encoding='utf-8') as f:
        s = json.load(f)
    return {
        "code": row_code,
        "total_return": s.get("total_return"),
        "annual_return": s.get("annual_return"),
        "max_falldown": s.get("max_falldown"),
        "sharpe_ratio": s.get("sharpe_ratio"),
        "win_rate": s.get("win_rate"),
    }


def run_serial(codes, args):
    from wtpy import WtBtEngine, EngineType
    from wtpy.apps import WtBtAnalyst
    from StraTurtle import StraTurtle

    for code in codes:
        print(f"[matrix] serial backtest {code}")
        m5_csv = os.path.join(HERE, "..", "storage", "csv",
                              f"{code}_m5.csv")
        stra = StraTurtle.from_config(
            name=stra_name(code), code=code, config_path=args.config,
            bt_no_simtick=not os.path.exists(m5_csv))
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


def run_subprocess(codes, args):
    for code in codes:
        print(f"[matrix] subprocess backtest {code}")
        cmd = [sys.executable, os.path.join(HERE, "runBT.py"),
               "--code", code, "--config", args.config,
               "--start", str(args.start), "--end", str(args.end),
               "--capital", str(args.capital),
               "--slippage", str(args.slippage)]
        r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[matrix] {code} FAILED:\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["serial", "subprocess"],
                    default="subprocess")
    ap.add_argument("--config", type=str,
                    default=os.path.join(HERE, "config", "turtle_config.yaml"))
    ap.add_argument("--start", type=int, default=202001010900)
    ap.add_argument("--end", type=int, default=202510151500)
    ap.add_argument("--capital", type=float, default=5000000)
    ap.add_argument("--slippage", type=int, default=0)
    ap.add_argument("--weights", type=str, default="",
                    help="组合权重 'code后缀:权重' 如 'rb:0.5,i:0.3'; "
                         "缺省等权")
    ap.add_argument("--rf", type=float, default=0.02,
                    help="组合指标无风险利率 (年化)")
    args = ap.parse_args()

    cfg = ConfigLoader.load(args.config)
    codes = [inst.code for inst in cfg.instruments]

    if args.mode == "serial":
        run_serial(codes, args)
    else:
        run_subprocess(codes, args)

    rows = [collect(c) for c in codes]
    out_dir = os.path.join(HERE, "matrix_results")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "matrix_summary.csv")
    keys = sorted({k for r in rows for k in r.keys()})
    with open(out_path, "w", newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    print(f"[matrix] summary -> {out_path}")
    for r in rows:
        print(r)

    # 组合层权益合成与贡献分解 (P1-4): 单品种曲线等权/自定义权合并
    try:
        synthesize_portfolio(codes, args, out_dir)
    except Exception as e:
        print(f"[matrix] WARN portfolio synthesis failed: {e}")


if __name__ == "__main__":
    main()
