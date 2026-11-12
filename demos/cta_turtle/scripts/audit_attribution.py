"""成交归因链生产观测窗实证 (D5, 2026-11-12)。

归因链 (localid/outbox/trades.csv, 10-29~30 落地) 的生产实证工具:
从策略日志 + 交易通道 trades.csv 汇总归因证据, 回答三个问题:
1. 证据分布 —— 成交回写走哪条证据链 (orders=localid 归因 /
   trades=journal 游标 / truth / timeout)?
2. 失败模式 —— UNKNOWN 封锁 / exit_unconfirmed / fill_timeout /
   ghost_fill / stale_pending 各发生了多少次 (观测窗内应为零或
   有完整处置链)?
3. 交叉验证 —— trades.csv 本策略品种的 localid 归因率与日志
   回写是否互证?

用法 (服务器):
  cd deploy/cta_turtle && python3 scripts/audit_attribution.py \
      --days 7 --trades generated/traders/simnow/trades.csv
本地回测目录同样可跑 (--trades outputs_bt/... 可省略)。
"""
import argparse
import collections
import csv
import glob
import json
import os
import re
import sys

RE_REANCHOR = re.compile(
    r"fill re-anchor .*?\((\w+)\).*?(?:layer|lots)", re.I)
# 宽松口径: 证据标注在括号里, 行首带 fill re-anchor
RE_EVIDENCE = re.compile(r"fill re-anchor[^\n]*\((orders|trades|truth"
                         r"|truth_zero|timeout|fees)\)", re.I)
RE_DRIFT = re.compile(r"avgpx drift re-anchor: shift ([+-][\d.]+)")
ALERT_PATTERNS = {
    "exit_unconfirmed": re.compile(r"exit_unconfirmed|exit unconfirmed"),
    "fill_timeout": re.compile(r"fill_timeout|unconfirmed \d+s"),
    "ghost_fill": re.compile(r"ghost_fill|ghost trim"),
    "stale_pending": re.compile(r"drop stale pending"),
    "unknown_block": re.compile(r"UNKNOWN.*(block|封锁)|block.*UNKNOWN"),
    "truth_stale": re.compile(r"truth_stale"),
    "signal_window": re.compile(r"signal window insufficient"),
}

# 本策略 12 品种 (排除共享账户 futu_mm 的 ao/ec)
PRODUCTS = ("rb", "cu", "al", "sc", "TA", "MA", "pp", "l", "m",
            "p", "CF", "IF")


def parse_strategy_log(path: str) -> dict:
    """单策略日志 → 归因实证记录 (纯解析, 可测)。"""
    rec = {"evidence": collections.Counter(), "drift_shifts": [],
           "alerts": collections.Counter(), "lines": 0}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                rec["lines"] += 1
                m = RE_EVIDENCE.search(line)
                if m:
                    rec["evidence"][m.group(1).lower()] += 1
                m = RE_DRIFT.search(line)
                if m:
                    rec["drift_shifts"].append(float(m.group(1)))
                for key, pat in ALERT_PATTERNS.items():
                    if pat.search(line):
                        rec["alerts"][key] += 1
    except OSError:
        pass
    return rec


def audit_trades_csv(path: str) -> dict:
    """trades.csv → 本策略品种成交统计 + localid 归因率。

    localid!=0 = 订单带本策略意图归因 (归因链); =0 多为账户内其他
    来源 (futu_mm 或旧单)。共享账户下按品种白名单过滤。"""
    out = {"rows": 0, "ours_rows": 0, "ours_volume": 0.0,
           "attributed_rows": 0, "attributed_volume": 0.0,
           "close_volume": 0.0, "open_volume": 0.0}
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            for r in csv.DictReader(f):
                out["rows"] += 1
                code = (r.get("code") or "")
                parts = code.split(".")
                if len(parts) < 2:
                    continue
                prod = parts[1]
                if prod not in PRODUCTS:
                    # CZCE 原生 2 段码 CZCE.TA701: 品种=字母前缀
                    m = re.match(r"([A-Za-z]+)", prod)
                    prod = m.group(1) if m else prod
                if prod not in PRODUCTS:
                    continue
                try:
                    vol = float(r.get("volume") or 0)
                except ValueError:
                    continue
                out["ours_rows"] += 1
                out["ours_volume"] += vol
                if (r.get("localid") or "0").strip() not in ("", "0"):
                    out["attributed_rows"] += 1
                    out["attributed_volume"] += vol
                act = (r.get("action") or "").upper()
                if act in ("CL", "CNL"):
                    out["close_volume"] += vol
                else:
                    out["open_volume"] += vol
    except OSError:
        out["error"] = f"cannot read {path}"
    out["attribute_rate"] = (round(out["attributed_volume"]
                                   / out["ours_volume"], 4)
                             if out["ours_volume"] else 0.0)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", default="Logs/Strategy",
                    help="策略日志目录")
    ap.add_argument("--days", type=int, default=7,
                    help="扫描最近 N 天的 *_YYYY-MM-DD.log")
    ap.add_argument("--trades", default="",
                    help="trades.csv 路径 (交易通道逐笔成交)")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.logs, "*.log")))
    if args.days > 0:
        files = files[-12 * args.days:]   # 至多 12 策略 x days
    per_strategy = {}
    total_evidence = collections.Counter()
    total_alerts = collections.Counter()
    all_shifts = []
    for path in files:
        name = os.path.basename(path).replace(".log", "")
        rec = parse_strategy_log(path)
        if not rec["lines"]:
            continue
        per_strategy[name] = rec
        total_evidence.update(rec["evidence"])
        total_alerts.update(rec["alerts"])
        all_shifts.extend(rec["drift_shifts"])

    report = {
        "window_files": len(files),
        "strategies": len(per_strategy),
        "evidence_distribution": dict(total_evidence),
        "alert_counts": dict(total_alerts),
        "drift_reanchors": len(all_shifts),
        "drift_shift_abs_max": (max((abs(x) for x in all_shifts),
                                    default=0.0)),
        "per_strategy": {
            name: {"evidence": dict(r["evidence"]),
                   "alerts": dict(r["alerts"]),
                   "drift_count": len(r["drift_shifts"])}
            for name, r in sorted(per_strategy.items())},
    }
    if args.trades:
        report["trades_csv"] = audit_trades_csv(args.trades)

    print(json.dumps({k: v for k, v in report.items()
                      if k != "per_strategy"}, ensure_ascii=False,
                     indent=1))
    busy = {n: r for n, r in report["per_strategy"].items()
            if r["evidence"] or r["alerts"]}
    if busy:
        print("\n-- 有归因/告警活动的策略 --")
        for n, r in sorted(busy.items()):
            print(f"  {n}: evidence={r['evidence']} "
                  f"alerts={r['alerts']} drift={r['drift_count']}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        print(f"\n-> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
