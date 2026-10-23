#!/usr/bin/env python3
"""过滤器 A/B 实证 (S2, 2026-10-22): 逐一启用过滤器跑共享账本组合回测

证据驱动启用流程的第 1 步 (OPTIMIZATION_PLAN §S2):
baseline (无过滤器) vs 每个过滤器单独启用, 同窗口同资金同约束,
比较约束后组合指标。只启用 OOS 改善且成本可覆盖的——本脚本产出 IS
全窗口证据, OOS 稳健性用 research/run_walkforward.py 另行验证。

口径注意:
- LiquidityFloor 是**绝对成交量阈值**, 12 品种成交量横截面不可比
  (rb 日成交 ~百万手 vs IF ~万手) —— 本 A/B 用 5000 手的低地板,
  结论只说明"极低流动性剔除"的价值, 不代表相对流动性过滤;
- ADX/VolBand 是全相对指标, 横截面可比;
- 过滤器作用于入场裁决 (武装时/决策时), 出场不受影响。

用法:
  python3 research/run_filter_ab.py --config config/turtle_config.yaml
产出: matrix_results/filter_ab.json + 终端对照表
"""
import argparse
import copy
import json
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from core.ConfigLoader import ConfigLoader                      # noqa: E402
from research.run_portfolio_bt import (PortfolioSim, _metrics,  # noqa: E402
                                       load_contracts, load_fees)

VARIANTS = {
    "baseline": None,
    "adx_14_25": {"type": "AdxTrend",
                  "params": {"period": 14, "threshold": 25.0}},
    "volband_250_10_90": {"type": "VolatilityBand",
                          "params": {"window": 250, "min_pct": 0.10,
                                     "max_pct": 0.90}},
    # 绝对地板, 仅"剔除极低流动性"口径 (见模块 docstring 口径注意)
    "liqfloor_5_5000": {"type": "LiquidityFloor",
                        "params": {"k": 5, "min_volume": 5000.0}},
}


def build_variant_config(config_path: str, variant: dict,
                         out_dir: str) -> str:
    """把过滤器注入每个 instruments 项, 写临时 yaml, 返回路径"""
    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw = copy.deepcopy(raw)
    for inst in raw.get("instruments", []):
        inst["filters"] = [dict(variant)]
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "_ab_tmp.yaml")
    with open(out, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, allow_unicode=True)
    return out


def run_variant(cfg_path: str, contracts, fees, args) -> dict:
    cfg = ConfigLoader.load(cfg_path, warn=False)
    sim = PortfolioSim(cfg, contracts, fees, args.capital,
                       args.margin_usage, args.slippage,
                       entry_mode=args.entry_mode)
    sim.priority = args.priority
    res = sim.run(args.start, args.end)
    return {"metrics": _metrics(res["daily"], args.capital),
            "trades": res["trades"], "rejected": res["rejected"],
            "fees": round(res["fees"], 2),
            "eval_start": res["eval_start"], "eval_end": res["eval_end"],
            "n_days": res["n_days"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(
        ROOT, "config", "turtle_config.yaml"))
    ap.add_argument("--capital", type=float, default=60_000_000)
    ap.add_argument("--start", type=int, default=20190101)
    ap.add_argument("--end", type=int, default=20260831)
    ap.add_argument("--slippage", type=int, default=1)
    ap.add_argument("--margin-usage", type=float, default=0.60)
    ap.add_argument("--entry-mode", choices=["close", "trigger"],
                    default="close")
    ap.add_argument("--priority", choices=["config", "strength"],
                    default="strength")
    ap.add_argument("--out-dir", default=os.path.join(ROOT,
                                                      "matrix_results"))
    args = ap.parse_args()

    common = os.path.abspath(os.path.join(ROOT, "..", "common"))
    contracts = load_contracts(common)
    fees = load_fees(common)
    tmp_dir = os.path.join(args.out_dir, "_ab_tmp")

    results = {}
    for name, variant in VARIANTS.items():
        path = args.config if variant is None else \
            build_variant_config(args.config, variant, tmp_dir)
        print(f"[ab] {name} ...", flush=True)
        results[name] = run_variant(path, contracts, fees, args)
        results[name]["variant"] = variant
        m = results[name]["metrics"]
        print(f"[ab] {name}: total={m.get('total_return', 0):+.2%} "
              f"dd={m.get('max_drawdown', 0):.2%} "
              f"sharpe={m.get('sharpe_ratio', 0):.3f} "
              f"trades={results[name]['trades']} "
              f"rejected={results[name]['rejected']}")

    out_path = os.path.join(args.out_dir, "filter_ab.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"spec": {"capital": args.capital, "start": args.start,
                            "end": args.end, "slippage": args.slippage,
                            "margin_usage": args.margin_usage,
                            "entry_mode": args.entry_mode,
                            "priority": args.priority},
                   "results": results}, f, ensure_ascii=False, indent=2)
    print(f"[ab] -> {out_path}")
    print("\n=== 过滤器 A/B 对照 (约束后口径) ===")
    base_m = results["baseline"]["metrics"]
    print(f"{'variant':18s} {'total':>9s} {'dd':>8s} {'sharpe':>7s} "
          f"{'Δsharpe':>8s} {'trades':>7s} {'rejected':>9s}")
    for name, r in results.items():
        m = r["metrics"]
        d = m.get("sharpe_ratio", 0) - base_m.get("sharpe_ratio", 0)
        print(f"{name:18s} {m.get('total_return', 0):+9.2%} "
              f"{m.get('max_drawdown', 0):8.2%} "
              f"{m.get('sharpe_ratio', 0):7.3f} {d:+8.3f} "
              f"{r['trades']:7d} {r['rejected']:9d}")


if __name__ == "__main__":
    main()
