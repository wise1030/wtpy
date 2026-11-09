#!/usr/bin/env python3
"""build_contract_signals — 逐合约信号数据管道 (Phase 1, 2026-11-10)。

用户拍板换月口径: 不做价格复权; 移仓后信号直接在新合约自身真实序列上
重算。本管道为该口径提供数据与换月日历:

1) 按品种轮换月规则枚举候选合约 (core.hotsutil.yymm_range), akshare
   futures_zh_daily_sina 逐合约拉全历史日线 (含持仓量), 缓存
   storage/csv/contracts/<EXCHG>.<prod>.<yymm>.csv (幂等可续跑);
2) 每日主力判定 = 候选合约持仓量最大者 → run-length 日历
   common/roll_calendar.json (与 server hots.json 规则同源语义);
3) 主力合约写 WT 格式 storage/csv/<合约>_d.csv (runBT/WtBtEngine 直读);
4) 自校验: 主力合约收盘价 vs 现有 HOT 序列 (非换月日应逐位贴近,
   偏差率 >5% 的品种报告人工复核 — 防 CZCE 跨十年同码取错实例)。

用法:
  python3 scripts/build_contract_signals.py                # 全量 (续跑)
  python3 scripts/build_contract_signals.py --codes SHFE.rb.HOT
  python3 scripts/build_contract_signals.py --skip-fetch   # 只重算日历/校验
"""
import argparse
import datetime
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
_PKG = ROOT
sys.path.insert(0, _PKG)

from core.hotsutil import sina_symbol, split_code, wt_contract, \
    yymm_range  # noqa: E402

WINDOW_START = "2016-01-01"
WINDOW_END = "2027-12-31"
_FETCH_SLEEP = 0.35          # sina 频控
WT_CSV_HEADER = "date,open,high,low,close,volume,turnover,open_interest"


def _fetch_one(symbol: str, cache: str) -> list:
    """拉单合约日线 -> [{date,open,high,low,close,volume,hold,settle}]。

    幂等: 缓存命中直接读。无数据/网络失败 -> []。"""
    if os.path.exists(cache):
        try:
            with open(cache, encoding="utf-8") as f:
                return json.load(f)
        except ValueError:
            pass
    import akshare as ak
    rows = []
    try:
        df = ak.futures_zh_daily_sina(symbol=symbol)
        if df is None or df.empty:
            return []
        for _, r in df.iterrows():
            d = str(r.get("date", ""))[:10]
            if not (WINDOW_START <= d <= WINDOW_END):
                continue
            rows.append({
                "date": d.replace("-", ""),
                "open": float(r.get("open") or 0),
                "high": float(r.get("high") or 0),
                "low": float(r.get("low") or 0),
                "close": float(r.get("close") or 0),
                "volume": float(r.get("volume") or 0),
                "hold": float(r.get("hold") or 0),
                "settle": float(r.get("settle") or 0)})
    except Exception:
        return []
    try:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        tmp = cache + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows, f)
        os.replace(tmp, cache)
    except OSError:
        pass
    return rows


def _load_hot_series(storage_csv: str):
    """既有 HOT csv -> {date: row} (列名大小写/日期格式兼容)。"""
    import csv
    out = {}
    try:
        with open(storage_csv, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                d = (r.get("date") or "").replace("/", "").replace("-", "")
                if len(d) == 8:
                    out[d] = r
    except OSError:
        pass
    return out


def build_product(code: str, storage_dir: str, cache_dir: str,
                  skip_fetch: bool) -> dict:
    ex, prod = split_code(code)
    yylist = yymm_range(ex, prod, 2016, 2027)
    series = {}                       # yymm -> {date: row}
    fetched = skipped = 0
    for yymm in yylist:
        wt = wt_contract(ex, prod, yymm)
        cache = os.path.join(cache_dir, f"{wt}.json")
        if skip_fetch and not os.path.exists(cache):
            skipped += 1
            continue
        rows = _fetch_one(sina_symbol(ex, prod, yymm), cache)
        if rows:
            series[yymm] = {r["date"]: r for r in rows}
            fetched += 1
        time.sleep(_FETCH_SLEEP)
    # 每日主力 = hold 最大
    all_dates = sorted({d for s in series.values() for d in s})
    cal = []                          # run-length [start,end,yymm]
    mismatch = total = 0
    hot = _load_hot_series(os.path.join(
        storage_dir, f"{code}_d.csv"))
    prev_d = None
    for d in all_dates:
        best = None
        best_hold = -1.0
        for yymm, s in series.items():
            row = s.get(d)
            if row and row["hold"] > best_hold:
                best_hold = row["hold"]
                best = yymm
        if best is None:
            continue
        if cal and cal[-1][2] == best and cal[-1][1] == prev_d:
            cal[-1][1] = d
        else:
            cal.append([d, d, best])
        prev_d = d
        # 校验: 与 HOT 收盘贴近
        h = hot.get(d)
        if h and best in series and d in series[best]:
            hc = float(h.get("close") or 0)
            cc = series[best][d]["close"]
            if hc > 0 and abs(cc - hc) / hc > 0.005:
                mismatch += 1
            total += 1
    # 写 WT csv (主力合约)
    written = 0
    csv_dir = storage_dir
    for yymm, s in series.items():
        used = any(seg[2] == yymm for seg in cal)
        if not used:
            continue
        wt = wt_contract(ex, prod, yymm)
        path = os.path.join(csv_dir, f"{wt}_d.csv")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(WT_CSV_HEADER + "\n")
            for d in sorted(s):
                r = s[d]
                ds = f"{d[:4]}/{d[4:6]}/{d[6:8]}"
                f.write("%s,%g,%g,%g,%g,%g,%g,%g\n" % (
                    ds, r["open"], r["high"], r["low"], r["close"],
                    r["volume"], r["close"] * r["volume"], r["hold"]))
        written += 1
    return {"code": code, "contracts_fetched": fetched,
            "contracts_skipped_missing": skipped, "segments": len(cal),
            "cal": cal, "dominant_csvs": written,
            "validate": {"checked": total, "mismatch": mismatch,
                         "mismatch_rate":
                             round(mismatch / total, 4) if total else None},
            "window": [cal[0][0], cal[-1][1]] if cal else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default="",
                    help="逗号分隔, 缺省=生产配置 12 品种")
    ap.add_argument("--storage", default=os.path.abspath(
        os.path.join(_PKG, "..", "storage")))
    ap.add_argument("--common", default=os.path.abspath(
        os.path.join(_PKG, "..", "common")))
    ap.add_argument("--skip-fetch", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        from core.ConfigLoader import ConfigLoader
        cfg = ConfigLoader.load(os.path.join(
            _PKG, "config", "turtle_config.yaml"), warn=False)
        codes = [i.code for i in cfg.instruments]
    cache_dir = os.path.join(args.storage, "csv", "contracts")
    report = {}
    cal_out = {}
    for code in codes:
        t0 = time.time()
        info = build_product(code, os.path.join(args.storage, "csv"),
                             cache_dir, args.skip_fetch)
        cal_out[code] = {"segments": info.pop("cal")}
        report[code] = info
        print("[build] %s: contracts=%d segments=%d mismatch=%s (%.0fs)"
              % (code, info["contracts_fetched"], info["segments"],
                 info["validate"], time.time() - t0), flush=True)
    cal_path = os.path.join(args.common, "roll_calendar.json")
    tmp = cal_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"_note": "Phase1 逐合约信号日历: [start_date,end_date,"
                            " dominant_yymm]; 无复权, 移仓日切信号序列",
                   "generated": datetime.datetime.now().strftime("%F %T"),
                   "codes": cal_out}, f, ensure_ascii=False)
    os.replace(tmp, cal_path)
    rep_path = args.out or os.path.join(args.storage, "csv",
                                        "contracts", "_build_report.json")
    os.makedirs(os.path.dirname(rep_path), exist_ok=True)
    with open(rep_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    bad = {k: v["validate"] for k, v in report.items()
           if v["validate"]["mismatch_rate"] and
           v["validate"]["mismatch_rate"] > 0.05}
    print("[build] calendar ->", cal_path)
    print("[build] report ->", rep_path)
    if bad:
        print("[build] WARN mismatch>5% (人工复核):", bad)
    print("[build] done.")


if __name__ == "__main__":
    main()
