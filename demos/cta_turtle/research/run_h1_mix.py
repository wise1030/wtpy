#!/usr/bin/env python3
"""h1/d1 混合速度组合证据 (S3 二期, 2026-10-24)

背景 (OPTIMIZATION_PLAN §S3): h1 一期单品种实证"单看 h1 更弱, 价值在
混合"——速度研究 (10/5 与 250/125 相关仅 0.40) 表明不同速度的收益流
低相关。本脚本在**同一模拟方法论**下产出组合层证据:

1. d1 组合 (品种子集, 窗口截到 m5 数据末端, 全资金 C);
2. h1 组合 (同品种同窗口, 全资金 C, 窗口计数按 h1 bar = 更快速度档);
3. 日收益相关 (Pearson/Spearman, d1 vs h1 日收益序列);
4. 混合曲线 = d1 @ C/2 + h1 @ C/2 逐日权益相加 (各自独立共享账本,
   近似口径: 跨速度的总单位/相关性组上限不互相占用, 混合的真实
   上界——两账本独立都偏松, 结果解读时注意)。

口径注意:
- 本地仅 rb/IF 有 m5 数据 (其余品种 h1 自动跳过), 故默认篮子
  --codes rb,IF; 结论只代表这两品种, 不外推 12 品种。
- m5 数据末端 2023-04-03 → 评估窗口自动截断到交集, 与 d1 全窗口
  成绩不可直接比 (脚本同时输出截断窗口的 d1 成绩作对照)。
- 混合近似不含跨速度闸门互斥, 混合夏普应视为**上界参考**; 生产化
  前仍需 plan §S3 步骤 3 (影子仓) 与步骤 4 (rt m5 日历口径验证)。

用法:
  python research/run_h1_mix.py --codes rb,IF --capital 60000000 \
      --start 20190101 --end 20260831 --entry-mode close
"""
import argparse
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from core.ConfigLoader import ConfigLoader                 # noqa: E402
from research.run_portfolio_bt import (                     # noqa: E402
    PortfolioSim, load_contracts, load_fees, _hash_file, _metrics)


def _run(cfg, contracts, fees, capital, args, period):
    sim = PortfolioSim(cfg, contracts, fees, capital,
                       args.margin_usage, args.slippage,
                       entry_mode=args.entry_mode, period=period,
                       codes=args.codes_list, data_dir=args.data_dir)
    sim.priority = args.priority
    return sim.run(args.start, args.end)


def _daily_np(res: dict):
    dates = np.asarray([x["date"] for x in res["daily"]], dtype=np.int64)
    eq = np.asarray([x["equity"] for x in res["daily"]], dtype=float)
    return dates, eq


def _align_returns(dates_a, eq_a, dates_b, eq_b):
    """两条日权益序列 → 交集日期上的日收益对 (ret_a, ret_b)。"""
    ia = {int(d): k for k, d in enumerate(dates_a)}
    ib = {int(d): k for k, d in enumerate(dates_b)}
    common = sorted(set(ia) & set(ib))
    ra, rb = [], []
    prev_a = prev_b = None
    for d in common:
        ka, kb = ia[d], ib[d]
        if prev_a is not None and prev_a > 0 and prev_b > 0:
            ra.append(eq_a[ka] / prev_a - 1.0)
            rb.append(eq_b[kb] / prev_b - 1.0)
        prev_a, prev_b = eq_a[ka], eq_b[kb]
    return np.asarray(ra), np.asarray(rb), common


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(
        ROOT, "config", "turtle_config.yaml"))
    ap.add_argument("--codes", default="rb,IF",
                    help="参与混合的品种子集 (需有 m5 数据), 逗号分隔")
    ap.add_argument("--capital", type=float, default=60_000_000)
    ap.add_argument("--start", type=int, default=20190101)
    ap.add_argument("--end", type=int, default=20260831)
    ap.add_argument("--slippage", type=int, default=1)
    ap.add_argument("--margin-usage", type=float, default=0.60)
    ap.add_argument("--priority", choices=["config", "strength"],
                    default="config")
    ap.add_argument("--entry-mode", choices=["close", "trigger"],
                    default="close")
    ap.add_argument("--mix-frac-h1", type=float, default=0.5,
                    help="h1 占混合资金比例 (默认 0.5)")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--out", default=os.path.join(
        ROOT, "matrix_results", "h1_mix.json"))
    args = ap.parse_args()
    args.codes_list = [c.strip() for c in args.codes.split(",")]

    cfg = ConfigLoader.load(args.config, warn=False)
    common = os.path.abspath(os.path.join(ROOT, "..", "common"))
    contracts = load_contracts(common)
    fees = load_fees(common)

    h1_frac = float(args.mix_frac_h1)
    d1_cap = args.capital * (1.0 - h1_frac)
    h1_cap = args.capital * h1_frac

    res_d1 = _run(cfg, contracts, fees, d1_cap if h1_frac else args.capital,
                  args, "d1")
    res_h1 = _run(cfg, contracts, fees, h1_cap, args, "h1")
    # 全资金 d1 对照 (截断窗口): 衡量"把一半资金挪去 h1"的机会成本
    res_d1_full = _run(cfg, contracts, fees, args.capital, args, "d1") \
        if h1_frac else res_d1

    dates_d, eq_d = _daily_np(res_d1)
    dates_h, eq_h = _daily_np(res_h1)
    ra, rb, common_days = _align_returns(dates_d, eq_d, dates_h, eq_h)
    pearson = float(np.corrcoef(ra, rb)[0, 1]) if len(ra) > 2 else None
    # Spearman: 排名相关 (对收益分布非线性稳健; scipy 缺失时只留 pearson)
    spearman = None
    if len(ra) > 2:
        try:
            from scipy.stats import spearmanr  # type: ignore
            spearman = float(spearmanr(ra, rb)[0])
        except Exception:
            spearman = None

    # 混合曲线: 半资金 d1 + 半资金 h1 逐日权益相加 (并集日期,
    # 缺失侧沿用该侧末日权益)
    all_days = sorted(set(int(d) for d in dates_d)
                      | set(int(d) for d in dates_h))
    id_map = {int(d): k for k, d in enumerate(dates_d)}
    ih_map = {int(d): k for k, d in enumerate(dates_h)}
    mix_daily = []
    last_d = last_h = None
    for d in all_days:
        if d in id_map:
            last_d = float(eq_d[id_map[d]])
        if d in ih_map:
            last_h = float(eq_h[ih_map[d]])
        if last_d is None or last_h is None:
            continue
        eq = last_d + last_h
        mix_daily.append({"date": d, "equity": eq, "pnl": eq - args.capital,
                          "margin": 0.0, "active_units": 0,
                          "positions": 0})

    out = {
        "config": os.path.abspath(args.config),
        "config_sha256": _hash_file(args.config),
        "codes": args.codes_list,
        "capital": args.capital,
        "mix_frac_h1": h1_frac,
        "entry_mode": args.entry_mode,
        "window": {"start": args.start, "end": args.end},
        "note": ("混合=两个独立共享账本近似 (跨速度单位上限不互斥,"
                 "夏普为上界参考); 窗口被 m5 数据末端截断到交集"),
        "d1_full_capital": {
            "eval": [res_d1_full["eval_start"], res_d1_full["eval_end"]],
            "metrics": _metrics(res_d1_full["daily"], args.capital),
            "trades": res_d1_full["trades"],
        },
        "d1_mix_share": {
            "capital": d1_cap,
            "eval": [res_d1["eval_start"], res_d1["eval_end"]],
            "metrics": _metrics(res_d1["daily"], d1_cap),
            "trades": res_d1["trades"],
        },
        "h1_mix_share": {
            "capital": h1_cap,
            "eval": [res_h1["eval_start"], res_h1["eval_end"]],
            "metrics": _metrics(res_h1["daily"], h1_cap),
            "trades": res_h1["trades"],
        },
        "mix": {
            "metrics": _metrics(mix_daily, args.capital),
            "n_days": len(mix_daily),
        },
        "daily_return_correlation": {
            "n_days": len(ra),
            "pearson": pearson,
            "spearman": spearman,
        },
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    c = out["daily_return_correlation"]
    print(f"[h1mix] d1 全资金 {out['d1_full_capital']['metrics']}")
    print(f"[h1mix] d1@{d1_cap:.0f} {out['d1_mix_share']['metrics']}")
    print(f"[h1mix] h1@{h1_cap:.0f} {out['h1_mix_share']['metrics']}")
    print(f"[h1mix] 混合   {out['mix']['metrics']}")
    print(f"[h1mix] 日收益相关 pearson={c['pearson']:.3f} "
          f"spearman={c['spearman']:.3f} (n={c['n_days']}) "
          f"-> {args.out}")
    # 判读提示 (plan §S3 步骤 2/3 门槛): 相关 <0.7 且混合夏普 >
    # 单速度夏普 → 值得影子仓; 否则 h1 速度在该篮子无分散化价值
    mix_shp = out["mix"]["metrics"].get("sharpe_ratio")
    d1_shp = out["d1_full_capital"]["metrics"].get("sharpe_ratio")
    h1_shp = out["h1_mix_share"]["metrics"].get("sharpe_ratio")
    if pearson is not None and mix_shp is not None:
        gate = pearson < 0.7 and mix_shp > max(d1_shp or 0, h1_shp or 0)
        print(f"[h1mix] 分散化门槛 (corr<0.7 且 mix_sharpe>单速度): "
              f"{'通过' if gate else '未通过'} -> "
              f"{'建议进入影子仓阶段' if gate else 'h1 暂不生产化'}")


if __name__ == "__main__":
    main()
