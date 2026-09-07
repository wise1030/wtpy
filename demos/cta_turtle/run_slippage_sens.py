"""滑点敏感性矩阵 (P0-3 一致性工程)

对同一品种在不同 slippage(tick 数)下重复回测, 归档各档产出并汇总
年化/夏普/回撤/胜率 → outputs_bt/sens/<code>_slip<k>/ + sens_report.csv

用法:
  python run_slippage_sens.py --code SHFE.rb.HOT \
      --start 202001010900 --end 202510151500 --slippages 0,1,2,3
"""
import argparse
import csv
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SENS_DIR = os.path.join(HERE, "outputs_bt", "sens")

METRICS = ("total_return", "annual_return", "max_falldown",
           "sharpe_ratio", "win_rate")


def run_one(code: str, slip: int, args) -> str:
    """跑单档滑点回测并把产出归档到 sens 目录; 返回归档目录"""
    stra_name = f"turtle_{code.replace('.', '_')}"
    cmd = [sys.executable, os.path.join(HERE, "runBT.py"),
           "--code", code, "--config", args.config,
           "--start", str(args.start), "--end", str(args.end),
           "--capital", str(args.capital),
           "--slippage", str(slip)]
    r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True)
    src = os.path.join(HERE, "outputs_bt", stra_name)
    if not os.path.isdir(src):
        raise RuntimeError(
            f"{code} slip={slip}: 无产出 ({r.stdout[-500:]}\n{r.stderr[-500:]})")
    dst = os.path.join(SENS_DIR, f"{code.replace('.', '_')}_slip{slip}")
    if os.path.exists(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst,
                    ignore=shutil.ignore_patterns("*.json.bak"))
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", type=str, required=True)
    ap.add_argument("--config", type=str,
                    default=os.path.join(HERE, "config", "turtle_config.yaml"))
    ap.add_argument("--start", type=int, default=202001010900)
    ap.add_argument("--end", type=int, default=202510151500)
    ap.add_argument("--capital", type=float, default=5000000)
    ap.add_argument("--slippages", type=str, default="0,1,2,3",
                    help="逗号分隔的滑点 tick 数序列")
    args = ap.parse_args()
    slips = [int(s) for s in args.slippages.split(",") if s != ""]
    if not slips:
        raise SystemExit("--slippages 为空")

    rows = []
    for slip in slips:
        print(f"[sens] {args.code} slippage={slip}")
        try:
            folder = run_one(args.code, slip, args)
        except RuntimeError as e:
            print(f"[sens] FAILED: {e}")
            continue
        with open(os.path.join(folder, "summary.json"),
                  encoding='utf-8') as f:
            s = json.load(f)
        row = {"code": args.code, "slippage_ticks": slip}
        row.update({k: s.get(k) for k in METRICS})
        rows.append(row)

    if not rows:
        raise SystemExit("全部档位失败, 无报告")
    out_csv = os.path.join(SENS_DIR, "sens_report.csv")
    os.makedirs(SENS_DIR, exist_ok=True)
    with open(out_csv, "w", newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"[sens] report -> {out_csv}")
    for r in rows:
        print(r)


if __name__ == "__main__":
    main()
