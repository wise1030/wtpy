#!/usr/bin/env python3
"""seed_live_contracts — 实盘 his/day 逐合约历史回填 (Phase 1, 2026-11-10)。

contract 信号源要求当前主力合约在 his/day 有 **挂牌以来** 的完整日线
(WtDataReader 按 his/day/<EXCHG>/<product><yymm>.dsb 读取), 而数据
底盘只从部署日起写合约 bar —— 本脚本按 common/roll_calendar.json 的
**末段 (当前主力)** 把已抓取的合约 csv 转成实盘 DSB 文件名口径:

    storage/csv/SHFE.rb.2701_d.csv -> storage/his/day/SHFE/rb2701.dsb

(实盘 reader 文件名 = <product><yymm>.dsb, 无交易所前缀, 见
 WtDataWriter::dump_day_data 与生产 his/day/SHFE/rb2701.dsb 实证。)

换月后由运维重跑本脚本 (或配 timer); 幂等全量重写当前段。
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from core.hotsutil import split_code  # noqa: E402


def current_segment_ymms(common_dir: str) -> dict:
    """roll_calendar.json -> {code: yymm} (每品种末段 = 当前主力)。"""
    path = os.path.join(common_dir, "roll_calendar.json")
    with open(path, encoding="utf-8") as f:
        raw = json.load(f) or {}
    out = {}
    for code, entry in (raw.get("codes") or {}).items():
        segs = (entry or {}).get("segments") or []
        if segs:
            out[code] = str(segs[-1][2])
    return out


def seed_one(csv_path: str, storage_dir: str, code: str,
             yymm: str) -> str:
    from convert_csv_to_dsb import load_csv_to_bars
    from wtpy.wrapper.WtDtHelper import WtDataHelper
    ex, prod = split_code(code)
    csv_file = os.path.join(csv_path, f"{ex}.{prod}.{yymm}_d.csv")
    if not os.path.exists(csv_file):
        return f"MISS {csv_file}"
    (result), count = load_csv_to_bars(csv_file, stop_tdate=0,
                                       is_min=False)
    if result is None or count == 0:
        return f"EMPTY {csv_file}"
    out_dir = os.path.join(storage_dir, "his", "day", ex)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{prod}{yymm}.dsb")
    ptr, _buf = result
    helper = WtDataHelper()
    ok = helper.store_bars(out_path, ptr, count, "d")
    return f"{'OK  ' if ok else 'FAIL'} {out_path} ({count} bars)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--storage", default=os.path.abspath(
        os.path.join(ROOT, "..", "storage")))
    ap.add_argument("--common", default=os.path.abspath(
        os.path.join(ROOT, "..", "common")))
    ap.add_argument("--codes", default="")
    args = ap.parse_args()
    targets = current_segment_ymms(args.common)
    if args.codes:
        keep = {c.strip() for c in args.codes.split(",") if c.strip()}
        targets = {k: v for k, v in targets.items() if k in keep}
    csv_dir = os.path.join(args.storage, "csv")
    for code, yymm in sorted(targets.items()):
        print(seed_one(csv_dir, args.storage, code, yymm))
    print("done.", len(targets), "contracts")


if __name__ == "__main__":
    main()
