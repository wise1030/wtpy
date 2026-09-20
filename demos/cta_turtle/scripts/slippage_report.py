#!/usr/bin/env python3
"""执行质量报告: 真实成交滑点 + 分批价差 (2026-09-16, 阶段0)

数据源 (均为实盘产物, 只读):
  - Logs/Runner_<date>.log : 策略决策行 `enter/add ... @~X, N=..` (记账价=决策价)
  - generated/traders/<id>/trades.csv : 真实逐笔成交 (code,action,volume,price)

输出 (按品种):
  n_orders  决策笔数;  n_fills 成交笔数
  slip_ticks  |成交均价 - 决策价| 的中位数 (tick, 需 --pricetick 或 contracts.json)
  slip_N      |成交均价 - 决策价| / N 的中位数 (N 取自决策行的 N= 字段)
  batch_ticks 同一决策批次内 (相邻成交间隔<120s) 的 (最高价-最低价) 中位数
              —— 直接量化"1 手顺序追单"的成本 (MA 53 手曾跨 50 分钟)

用法: python3 scripts/slippage_report.py --deploy-dir . --date 2026-09-16 \
        [--pricetick SHFE.rb=1.0,...] [--json out.json]
"""
import argparse
import csv
import datetime
import json
import os
import re
import statistics as st

DEC_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\].*?(?P<name>turtle_\S+?)\s+"
    r"(?P<kind>enter|add layer\d+)\s+(?P<rest>.+?)@~(?P<px>[0-9.]+)"
    r"(?:.*?N=(?P<n>[0-9.]+))?", re.M)


def parse_decisions(log_path: str, date_tag: str) -> list:
    """策略决策行 → [{ts,name,code,px,n}] (记账价=决策价)"""
    out = []
    if not os.path.exists(log_path):
        return out
    with open(log_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if date_tag and date_tag not in line:
                continue
            m = DEC_RE.search(line)
            if not m:
                continue
            name = m.group("name")
            code = name[len("turtle_"):].replace("_HOT", "").replace("_", ".")
            try:
                ep = datetime.datetime.strptime(
                    m.group("ts").strip()[:19], "%Y.%m.%d %H:%M:%S").replace(
                    tzinfo=datetime.timezone(
                        datetime.timedelta(hours=8))).timestamp()
            except ValueError:
                continue
            out.append({"ts": m.group("ts"), "epoch": ep, "code": code,
                        "px": float(m.group("px")),
                        "n": float(m.group("n")) if m.group("n") else None})
    return out


def load_fills(path: str) -> dict:
    """trades.csv → {code: [(time_ms, volume, price), ...]}"""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as f:
        for row in csv.reader(f):
            if len(row) < 8 or not row[0].isdigit():
                continue
            code = row[3].strip()
            try:
                out.setdefault(code, []).append(
                    (int(row[2]), float(row[5]), float(row[6])))
            except ValueError:
                continue
    for v in out.values():
        v.sort()
    return out


def _pref(code: str) -> str:
    """'CZCE.CF' -> 'CZCE.CF.' (决策侧是 HOT 码, 成交侧是真实月份码)"""
    p = code.split(".")
    return "." .join(p[:2]) + "." if len(p) >= 2 else code


def match_slippage(decisions: list, fills: dict, window_s: int = 86400,
                   batch_gap_ms: int = 120_000) -> dict:
    """把决策行与成交匹配 (决策侧 HOT 码 ↔ 成交侧真实合约码, 按品种前缀) →
    每品种滑点/分批价差统计。跨日也成立 (决策在夜盘、成交在次日日盘)。"""
    by_pref = {}
    for code, rows in fills.items():
        by_pref.setdefault(_pref(code), []).extend(rows)
    for v in by_pref.values():
        v.sort()
    stat = {}
    for d in decisions:
        fl = by_pref.get(_pref(d["code"])) or []
        # 时间窗: 决策时刻 (CST→epoch) 之后 window_s 内的成交
        cand = [f for f in fl
                if 0 <= f[0] / 1000.0 - d["epoch"] <= window_s]
        if not cand:
            continue
        vols = [f[1] for f in cand]
        px = sum(f[1] * f[2] for f in cand) / max(sum(vols), 1e-9)
        s = stat.setdefault(d["code"], {"n_orders": 0, "n_fills": 0,
                                       "slip": [], "slipN": [],
                                       "batch": [], "dur": []})
        s["n_orders"] += 1
        s["n_fills"] += len(cand)
        s["slip"].append(abs(px - d["px"]))
        s["dur"].append((cand[-1][0] - cand[0][0]) / 60000.0)   # 分批时长(分)
        if d["n"]:
            s["slipN"].append(abs(px - d["px"]) / d["n"])
        # 批次内价差: 相邻成交间隔 < batch_gap 的簇
        cluster = [cand[0]]
        for a, b in zip(cand, cand[1:]):
            if b[0] - a[0] <= batch_gap_ms:
                cluster.append(b)
            else:
                if len(cluster) > 1:
                    s["batch"].append(max(x[2] for x in cluster)
                                      - min(x[2] for x in cluster))
                cluster = [b]
        if len(cluster) > 1:
            s["batch"].append(max(x[2] for x in cluster)
                              - min(x[2] for x in cluster))
    return stat


def report(stat: dict, pricetick: dict, json_out: str = ""):
    print("%-14s %6s %7s %11s %10s %12s %12s" % ("品种", "决策", "成交笔",
                                            "滑点(tick)*", "滑点(N)*",
                                            "批次价差(tick)", "分批时长(分)"))
    dump = {}
    for code in sorted(stat):
        s = stat[code]
        tk = pricetick.get(code) or pricetick.get(code.split(".")[1], 1.0)
        med = st.median(s["slip"]) if s["slip"] else 0.0
        medN = st.median(s["slipN"]) if s["slipN"] else 0.0
        bat = st.median(s["batch"]) if s["batch"] else 0.0
        dur = st.median(s["dur"]) if s["dur"] else 0.0
        print("%-14s %6d %7d %11.2f %10.4f %12.2f %12.1f" % (
            code, s["n_orders"], s["n_fills"], med / tk, medN, bat / tk, dur))
        dump[code] = {"n_orders": s["n_orders"], "n_fills": s["n_fills"],
                      "slip_ticks": round(med / tk, 2),
                      "slip_N": round(medN, 4),
                      "batch_ticks": round(bat / tk, 2),
                      "fill_minutes": round(dur, 1)}
    if json_out:
        with open(json_out, "w", encoding="utf-8") as f:
            json.dump(dump, f, ensure_ascii=False, indent=2)
        print(f"[slip] -> {json_out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deploy-dir", default=".")
    ap.add_argument("--date", required=True, help="2026-09-16")
    ap.add_argument("--trader", default="simnow")
    ap.add_argument("--pricetick", default="",
                    help="SHFE.rb=1,DCE.l=1 (缺省 1.0)")
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    D = os.path.abspath(args.deploy_dir)
    date_tag = args.date.replace("-", ".")
    dec = parse_decisions(os.path.join(D, "Logs", f"Runner_{args.date}.log"),
                          date_tag)
    fills = load_fills(os.path.join(D, "generated", "traders", args.trader,
                                    "trades.csv"))
    pt = {}
    for item in args.pricetick.split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            try:
                pt[k.strip()] = float(v)
            except ValueError:
                pass
    print(f"[slip] 决策 {len(dec)} 笔, 成交品种 {len(fills)} 个")
    report(match_slippage(dec, fills), pt, args.json)


if __name__ == "__main__":
    main()
