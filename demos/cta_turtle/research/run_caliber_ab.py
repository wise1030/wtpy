#!/usr/bin/env python3
"""S4 信号口径 A/B 汇总 (2026-10-24)

读取 run_portfolio_bt.py 各变体产出 (matrix_results/portfolio_bt*.json),
汇总为 matrix_results/caliber_ab.json 并打印对照表。变体跑法见
docs/OPTIMIZATION_PLAN_2026-10-22.md §S4; 本脚本只做聚合与留档,
不重复跑回测 (回测耗时, 由人工/CI 按需触发各变体)。

口径: 12 品种组合 / 60M / 1tick / 2019-01~2026-08 (1859 交易日) /
priority=strength / constrained。

变体清单:
- close 基线          : (无附加参数)
- S4-3 旧跳空口径     : --no-gap-stop
- S4-2 禁同 bar 反转  : --no-same-bar-reversal
- trigger 基线        : --entry-mode trigger
- S4-1 纯通道武装     : --entry-mode trigger --no-envelope
                        (实证: 与 trigger 基线逐位相同 — 包络在通道
                        窗口已含信号 bar 时为恒等操作)
- S4-1b 严格经典武装  : --entry-mode trigger --arm-exclude-signal
                        (突破前 N 日通道, 排除信号 bar; A/B 显著更优)
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 变体键 → (文件名 tag, 说明, 附加口径注记)
VARIANTS = {
    "close_baseline": ("", "close 基线 (S4 默认口径)", ""),
    "s4_3_no_gap_stop": ("calib_nogap", "S4-3 旧跳空口径",
                         "--no-gap-stop"),
    "s4_2_no_same_bar_reversal": ("calib_norev", "S4-2 禁同 bar 反转",
                                  "--no-same-bar-reversal"),
    "trigger_baseline": ("trigger", "trigger 基线", "--entry-mode trigger"),
    "s4_1_channel_only": ("trigger_calib_noenv", "S4-1 纯通道武装",
                          "--no-envelope (与基线逐位相同: 包络恒等)"),
    "s4_1b_arm_exclude_signal": ("trigger_calib_classic", "S4-1b 严格经典武装",
                                 "--arm-exclude-signal"),
}


def main():
    out = {"note": ("12 品种组合/60M/1tick/2019-01~2026-08/strength/"
                    "constrained; 变体跑法见 OPTIMIZATION_PLAN §S4"),
           "variants": {}}
    rows = []
    reference = None
    spec_keys = ("config_sha256", "capital", "start", "end",
                 "slippage_ticks", "margin_usage", "period", "codes",
                 "priority", "max_units_total", "max_units_per_group")
    for key, (tag, desc, note) in VARIANTS.items():
        name = f"portfolio_bt_{tag}.json" if tag else "portfolio_bt.json"
        path = os.path.join(ROOT, "matrix_results", name)
        if not os.path.exists(path):
            print(f"[caliber_ab] SKIP {key}: 缺 {name} (先跑对应变体)",
                  file=sys.stderr)
            continue
        d = json.load(open(path, encoding="utf-8"))
        current = {k: d.get(k) for k in spec_keys}
        if reference is None:
            reference = current
            reference_manifest = d.get("manifest")
        else:
            mismatch = [f"{k}: {reference[k]!r} != {current[k]!r}"
                        for k in spec_keys if reference[k] != current[k]]
            manifest = d.get("manifest")
            if reference_manifest is not None and manifest is not None \
                    and reference_manifest != manifest:
                mismatch.append("manifest: 输入数据/代码指纹不一致")
            if mismatch:
                raise SystemExit(
                    f"[caliber_ab] 拒绝异口径聚合 {key}: " +
                    "; ".join(mismatch))
        c = d["constrained"]
        m = c["metrics"]
        entry = {
            "desc": desc, "flags": note,
            "entry_mode": d.get("entry_mode"),
            "total_return": m.get("total_return"),
            "max_drawdown": m.get("max_drawdown"),
            "sharpe_ratio": m.get("sharpe_ratio"),
            "trades": c.get("trades"),
            "rejected": c.get("rejected"),
        }
        out["variants"][key] = entry
        rows.append((key, entry))
    dst = os.path.join(ROOT, "matrix_results", "caliber_ab.json")
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"{'变体':<28}{'总收益':>10}{'回撤':>8}{'Sharpe':>8}"
          f"{'笔数':>6}  说明")
    for key, e in rows:
        print(f"{key:<28}{e['total_return']:>10.2%}"
              f"{e['max_drawdown']:>8.2%}{e['sharpe_ratio']:>8.3f}"
              f"{e['trades']:>6}  {e['desc']}")
    print(f"[caliber_ab] -> {dst}")


if __name__ == "__main__":
    main()
