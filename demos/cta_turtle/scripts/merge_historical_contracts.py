#!/usr/bin/env python3
"""merge_historical_contracts — 历史合约回填 contracts.json (Phase 1)。

回测逐合约信号需要 WtBtCore 能解析每个历史主力合约 (extractStdCode /
commodity 查询, 缺条目的合约码会让 CtaMocker::stra_get_bars 段错误 —
2026-11-10 rb contract 模式首跑实证)。

数据源: roll_calendar.json (哪些合约当过主力) + storage/csv/contracts/
缓存 (精确挂牌/摘牌日期) + 同品种现有合约条目 (交易限制/保证金字段)。
已存在的键不动 (服务器 update_contracts.py 同语义: 保留手工/已有字段)。
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from core.hotsutil import split_code, wt_contract  # noqa: E402


def _template(d: dict, ex: str, prod: str) -> dict:
    """同品种任现存合约条目作字段模板; 无则默认。"""
    for _k, v in (d.get(ex) or {}).items():
        if isinstance(v, dict) and v.get("product") == prod:
            return v
    return {"maxlimitqty": 500, "maxmarketqty": 30, "minlimitqty": 1,
            "minmarketqty": 1, "longmarginratio": 0.12,
            "shortmarginratio": 0.12}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--common", default=os.path.abspath(
        os.path.join(ROOT, "..", "common")))
    ap.add_argument("--storage", default=os.path.abspath(
        os.path.join(ROOT, "..", "storage")))
    args = ap.parse_args()
    cal_path = os.path.join(args.common, "roll_calendar.json")
    contracts_path = os.path.join(args.common, "contracts.json")
    commodities_path = os.path.join(args.common, "commodities.json")
    cal = json.load(open(cal_path, encoding="utf-8"))["codes"]
    contracts = json.load(open(contracts_path, encoding="utf-8"))
    comms = json.load(open(commodities_path, encoding="utf-8"))
    cache_dir = os.path.join(args.storage, "csv", "contracts")
    added = 0
    for code, entry in cal.items():
        ex, prod = split_code(code)
        prod_name = ((comms.get(ex) or {}).get(prod) or {}).get(
            "name", prod)
        tpl = _template(contracts, ex, prod)
        ymms = {str(s[2]) for s in (entry or {}).get("segments") or []}
        bucket = contracts.setdefault(ex, {})
        for yymm in sorted(ymms):
            symbol = f"{prod}{yymm}"       # 键=品种+年月 (rb1905 口径)
            if symbol in bucket:
                continue
            cache = os.path.join(cache_dir, f"{wt_contract(ex, prod, yymm)}.json")
            dates = []
            try:
                rows = json.load(open(cache, encoding="utf-8"))
                dates = [r["date"] for r in rows if r.get("date")]
            except (OSError, ValueError):
                pass
            opendate = int(dates[0]) if dates else 0
            expiredate = int(dates[-1]) if dates else 0
            item = dict(tpl)
            item.update({"name": f"{prod_name}{yymm}", "code": symbol,
                         "exchg": ex, "product": prod,
                         "opendate": opendate, "expiredate": expiredate})
            bucket[symbol] = item
            added += 1
    tmp = contracts_path + ".tmp"
    json.dump(contracts, open(tmp, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    os.replace(tmp, contracts_path)
    total = sum(len(v) for v in contracts.values())
    print(f"added={added} total={total} -> {contracts_path}")


if __name__ == "__main__":
    main()
