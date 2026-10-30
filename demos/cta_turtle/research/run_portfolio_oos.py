#!/usr/bin/env python3
"""固定参数的组合样本外与成本/容量验证（不做网格寻优）。

复用 PortfolioSim，训练/验证窗分别运行 close/trigger 与多档滑点。
输出保证金峰值、超限天数、换月覆盖及统一输入 manifest。
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from core.ConfigLoader import ConfigLoader                 # noqa: E402
from research.run_portfolio_bt import (                    # noqa: E402
    PortfolioSim, load_contracts, load_fees, _metrics,
    _hash_file, _input_manifest)


def validate_windows(train_start, train_end, test_start, test_end):
    if not train_start < train_end <= test_start < test_end:
        raise ValueError(
            "需满足 train_start < train_end <= test_start < test_end")


def run_case(cfg, contracts, fees, args, mode, start, end, slippage):
    sim = PortfolioSim(cfg, contracts, fees, args.capital,
                       args.margin_usage, slippage,
                       entry_mode=mode, period=args.period,
                       codes=args.codes_list)
    sim.priority = args.priority
    res = sim.run(start, end)
    return sim, {
        "mode": mode, "start": start, "end": end,
        "slippage_ticks": slippage,
        "eval_start": res["eval_start"], "eval_end": res["eval_end"],
        "n_days": res["n_days"],
        "metrics": _metrics(res["daily"], args.capital),
        "trades": res["trades"], "rejected": res["rejected"],
        "fees": round(res["fees"], 2),
        "margin_peak": round(res["margin_peak"], 2),
        "margin_breach_days": res["margin_breach_days"],
        "roll_events": len(res["rolls"]),
        "roll_calendar_products": res["roll_calendar_products"],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(
        ROOT, "config", "turtle_config.yaml"))
    ap.add_argument("--train-start", type=int, required=True)
    ap.add_argument("--train-end", type=int, required=True)
    ap.add_argument("--test-start", type=int, required=True)
    ap.add_argument("--test-end", type=int, required=True)
    ap.add_argument("--capital", type=float, default=60_000_000)
    ap.add_argument("--margin-usage", type=float, default=0.60)
    ap.add_argument("--slippages", default="0,1,2")
    ap.add_argument("--modes", default="close,trigger")
    ap.add_argument("--period", choices=["d1", "h1"], default="d1")
    ap.add_argument("--codes", default=None)
    ap.add_argument("--priority", choices=["config", "strength"],
                    default="strength")
    ap.add_argument("--tag", default="fixed")
    ap.add_argument("--out-dir", default=os.path.join(ROOT,
                                                      "matrix_results"))
    args = ap.parse_args()
    validate_windows(args.train_start, args.train_end,
                     args.test_start, args.test_end)
    args.codes_list = [x.strip() for x in args.codes.split(",")] \
        if args.codes else None
    cfg = ConfigLoader.load(args.config, warn=False)
    common = os.path.abspath(os.path.join(ROOT, "..", "common"))
    contracts = load_contracts(common)
    fees = load_fees(common)
    modes = [x.strip() for x in args.modes.split(",") if x.strip()]
    if any(m not in ("close", "trigger") for m in modes):
        raise SystemExit("--modes 仅支持 close,trigger")
    slips = [int(x) for x in args.slippages.split(",") if x.strip()]
    if not slips or any(x < 0 for x in slips):
        raise SystemExit("--slippages 需为非负整数列表")

    cases = []
    manifest = None
    for mode in modes:
        for slippage in slips:
            for label, start, end in (
                    ("train", args.train_start, args.train_end),
                    ("test", args.test_start, args.test_end)):
                sim, row = run_case(cfg, contracts, fees, args, mode,
                                    start, end, slippage)
                row["window"] = label
                cases.append(row)
                if manifest is None:
                    manifest = _input_manifest(args.config, common, sim)
                m = row["metrics"]
                print(f"[oos] {label:5s} {mode:7s} slip={slippage} "
                      f"ret={m.get('total_return', 0):+.2%} "
                      f"dd={m.get('max_drawdown', 0):.2%} "
                      f"sharpe={m.get('sharpe_ratio', 0):.3f} "
                      f"margin_breach={row['margin_breach_days']}")
    out = {
        "spec": {"config": os.path.abspath(args.config),
                 "config_sha256": _hash_file(args.config),
                 "capital": args.capital,
                 "margin_usage": args.margin_usage,
                 "priority": args.priority, "period": args.period,
                 "codes": args.codes_list},
        "manifest": manifest,
        "note": "固定参数训练/验证与成本容量验证，非网格寻优；"
                "roll_calendar_products=0 时跨换月持仓未覆盖",
        "cases": cases,
    }
    os.makedirs(args.out_dir, exist_ok=True)
    path = os.path.join(args.out_dir, f"portfolio_oos_{args.tag}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[oos] {len(cases)} cases -> {path}")


if __name__ == "__main__":
    main()
