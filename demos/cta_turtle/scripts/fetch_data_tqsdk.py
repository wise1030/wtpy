#!/usr/bin/env python3
"""TqSdk 数据获取适配器 (P2)

两条能力:
1. K线获取 → storage/csv: 走 wtpy DHFactory("tqsdk") 的 dmpBars 回调,
   内存行经 BarJournal.merge_rows 幂等合并进 csv (版式/键逻辑与运行时
   journal 回补完全同源)。分钟线带 time 列 (G2 口径已修)。
2. hots 增强 (--fix-hots): query_cont_quotes 拿当前主力映射, 给
   hots.json 中的空键写入真实主力码 (实盘路由用; 仅当前快照, 不构成
   历史换月表, 回测复权不适用)。

凭据: --user/--pass 或环境变量 TQ_USER/TQ_PASS (快期账户)。
依赖: pip install tqsdk (未安装时给出明确提示, 不影响其他模块)。

用法:
  python3 scripts/fetch_data_tqsdk.py --config config/turtle_config.yaml \
      --periods d m5 --start 20200101 --csv-dir ../storage/csv
  python3 scripts/fetch_data_tqsdk.py --fix-hots --common ../common
"""
import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "helpers"))


# ------------------------------------------------------------------
# 纯逻辑 (无 tqsdk/wtpy 依赖, 可单测)
# ------------------------------------------------------------------
def std_code_to_tq(std_code: str) -> str:
    """WT 标准代码 → TqSdk 代码 (仅期货; HOT→主连 KQ.m@)。

    与 wtpy.apps.datahelper.DHTqSdk.stdCodeToTQ 的期货分支同口径,
    本地复制以避免在纯逻辑环境引入 tqsdk 依赖。
    """
    items = std_code.split(".")
    exchg = items[0]
    if len(items) == 3 and exchg in ("SHFE", "CFFEX", "DCE", "CZCE",
                                     "INE", "GFEX"):
        if items[2] != "HOT":
            return f"{exchg}.{items[1]}{items[2]}"
        return f"KQ.m@{exchg}.{items[1]}"
    return std_code


def tq_to_wt_contract(tq_symbol: str) -> str:
    """TqSdk 合约码 → WT 三段式: SHFE.rb2601 → SHFE.rb.2601"""
    exchg, raw = tq_symbol.split(".", 1)
    i = 0
    while i < len(raw) and raw[i].isalpha():
        i += 1
    return f"{exchg}.{raw[:i]}.{raw[i:]}"


def rows_from_buffer(buffer, count: int, period: str) -> list:
    """dmpBars 回调的 WTSBarStruct 缓冲 → merge_rows 行字典列表

    缓冲口径 (DHTqSdk.dmpBars): date=yyyymmdd; 分钟线 time=
    (date-19900000)*10000+HHMM, 日线 time=0。输出口径: 日线 time=0,
    分钟线 time=HHMMSS (BarJournal 键口径)。
    """
    rows = []
    for i in range(count):
        b = buffer[i]
        d = int(b.date)
        if period == "day":
            t = 0
        else:
            hhmm = int(b.time) % 10000
            t = (hhmm // 100) * 10000 + (hhmm % 100) * 100
        rows.append({"date": d, "time": t,
                     "open": float(b.open), "high": float(b.high),
                     "low": float(b.low), "close": float(b.close),
                     "volume": float(b.vol),
                     "open_interest": float(b.hold)})
    return rows


def hots_entries_from_cont(cont_map: dict, tdate: int) -> dict:
    """{WT代码: TqSdk主力合约码} → hots.json 规则条目 {exchg: {product: [..]}}

    单条快照映射: {date, from:"", to, oldclose:0, newclose:0}
    (实盘路由用; closes 未知置 0, 不用于回测换月复权)。
    """
    out = {}
    for std_code, tq_underlying in cont_map.items():
        exchg, product = std_code.split(".")[:2]
        wt_contract = tq_to_wt_contract(tq_underlying)
        raw = wt_contract.split(".")[-1]
        out.setdefault(exchg, {})[product] = [{
            "date": int(tdate), "from": "", "to": raw,
            "oldclose": 0.0, "newclose": 0.0,
        }]
    return out


def merge_hots(hots_path: str, entries: dict,
               only_empty: bool = True) -> list:
    """把主力映射条目合并进 hots.json; 返回被修改的 exchg.product 列表

    only_empty=True (默认): 仅当该产品规则列表为空时写入 (不覆盖
    已有历史换月表); False 时强制替换为当前快照。
    """
    if os.path.exists(hots_path):
        with open(hots_path, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}
    changed = []
    for exchg, prods in entries.items():
        for product, rules in prods.items():
            cur = data.get(exchg, {}).get(product)
            if only_empty and cur:
                continue
            data.setdefault(exchg, {})[product] = rules
            changed.append(f"{exchg}.{product}")
    if changed:
        with open(hots_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1,
                      sort_keys=True)
    return changed


# ------------------------------------------------------------------
# 集成 (需 tqsdk + wtpy 运行时)
# ------------------------------------------------------------------
def _auth(user: str, password: str):
    try:
        from tqsdk import TqApi, TqAuth
    except ImportError:
        raise SystemExit("[fetch] 未安装 tqsdk: pip install tqsdk "
                         "(需在快期注册账户)") from None
    if not user or not password:
        raise SystemExit("[fetch] 缺凭据: --user/--pass 或环境变量 "
                         "TQ_USER/TQ_PASS")
    return TqApi(auth=TqAuth(user, password))


def fetch_bars(user: str, password: str, codes: list, periods: list,
               csv_dir: str, start: int = None, end: int = None):
    """dmpBars 回调 → BarJournal.merge_rows → storage/csv (幂等)"""
    from helpers.BarJournal import BarJournal
    from wtpy.WtCoreDefs import WTSBarStruct  # noqa: F401 (类型锚定)
    from wtpy.apps.datahelper.DHFactory import DHFactory

    helper = DHFactory.createHelper("tqsdk")
    helper.auth(username=user, password=password)

    j = BarJournal(os.path.join(csv_dir, ".journal_fetch"))
    os.makedirs(csv_dir, exist_ok=True)
    suffix = {"day": "d", "min5": "m5", "min1": "m1"}

    kw = {}
    if start:
        kw["start_date"] = datetime.datetime.strptime(str(start), "%Y%m%d")
    if end:
        kw["end_date"] = datetime.datetime.strptime(str(end), "%Y%m%d")

    for period in periods:
        sfx = suffix.get(period)
        if sfx is None:
            print(f"[fetch] 跳过未知周期: {period}")
            continue

        def cb(exchg, std_code, buffer, count, _period,
               _sfx=sfx, _period_name=period):
            rows = rows_from_buffer(buffer, count, _period_name)
            csv_path = os.path.join(csv_dir, f"{std_code}_{_sfx}.csv")
            n = j.merge_rows(rows, _period_name, csv_path)
            print(f"[fetch] {std_code} {_period_name}: "
                  f"{count} bars 获取, +{n} 行合并 → {csv_path}")

        helper.dmpBars(codes, cb, period=period, **kw)


def fetch_hots(user: str, password: str, codes: list, common_dir: str,
               only_empty: bool = True) -> list:
    """query_cont_quotes → hots.json 当前主力映射"""
    api = _auth(user, password)
    try:
        cont_map = {}
        for std_code in codes:
            tq = std_code_to_tq(std_code)
            if not tq.startswith("KQ.m@"):
                continue
            q = api.get_quote(tq)
            if q and q.underlying_symbol:
                cont_map[std_code] = q.underlying_symbol
                print(f"[hots] {std_code} → {q.underlying_symbol}")
    finally:
        api.close()
    if not cont_map:
        print("[hots] 未取到任何主力映射")
        return []
    tdate = int(datetime.datetime.now().strftime("%Y%m%d"))
    entries = hots_entries_from_cont(cont_map, tdate)
    hots_path = os.path.join(common_dir, "hots.json")
    changed = merge_hots(hots_path, entries, only_empty=only_empty)
    print(f"[hots] hots.json 更新: {changed or '无变化'}")
    return changed


def main():
    ap = argparse.ArgumentParser(description="TqSdk 数据获取适配器")
    ap.add_argument("--config", type=str,
                    default=os.path.join(ROOT, "config",
                                         "turtle_config.yaml"),
                    help="从配置取 instruments 代码列表")
    ap.add_argument("--codes", type=str, nargs="*", default=None,
                    help="显式代码列表 (覆盖 --config)")
    ap.add_argument("--periods", type=str, nargs="+",
                    default=["day"], choices=["day", "min5", "min1"])
    ap.add_argument("--start", type=int, default=None,
                    help="起始日期 yyyymmdd (默认 TqSdk 全量)")
    ap.add_argument("--end", type=int, default=None)
    ap.add_argument("--csv-dir", type=str,
                    default=os.path.join(ROOT, "..", "storage", "csv"))
    ap.add_argument("--fix-hots", action="store_true",
                    help="写 hots.json 当前主力映射 (仅空键)")
    ap.add_argument("--force-hots", action="store_true",
                    help="hots 强制覆盖 (慎用: 会丢弃历史换月表)")
    ap.add_argument("--common", type=str,
                    default=os.path.join(ROOT, "..", "common"))
    ap.add_argument("--user", type=str,
                    default=os.environ.get("TQ_USER", ""))
    ap.add_argument("--password", type=str,
                    default=os.environ.get("TQ_PASS", ""))
    args = ap.parse_args()

    codes = args.codes
    if not codes:
        from helpers.ConfigLoader import ConfigLoader
        cfg = ConfigLoader.load(args.config)
        codes = [i.code for i in cfg.instruments]
    print(f"[fetch] codes={codes} periods={args.periods}")

    if args.fix_hots or args.force_hots:
        fetch_hots(args.user, args.password, codes,
                   os.path.abspath(args.common),
                   only_empty=not args.force_hots)
    else:
        fetch_bars(args.user, args.password, codes, args.periods,
                   os.path.abspath(args.csv_dir),
                   start=args.start, end=args.end)


if __name__ == "__main__":
    main()
