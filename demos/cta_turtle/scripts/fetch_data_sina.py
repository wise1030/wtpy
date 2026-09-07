#!/usr/bin/env python3
"""新浪主力连续日线拉取脚本 (akshare futures_main_sina)

拉取指定品种的主力连续日线, 经 BarJournal.merge_rows 幂等合并进
storage/csv (与运行时回补/TqSdk 适配器同源合并逻辑)。

适用场景: 新品种冷启动暖机数据、日常数据补齐 (TqSdk 之外的免费兜底源)。
注意口径: 新浪"主力连续"按持仓量/成交量切换, 与 WT HOT 规则不完全同源,
换月日可能有小幅跳空; 日线级趋势策略影响可控 (见 docs/DATA_OPS.md)。

品种代码映射: futures_main_sina 用 'RB0' 风格, 本脚本接受
'SHFE.rb.HOT' 风格配置代码, 映射关系内置 (按需扩充 SINA_SYMBOL)。

用法:
  python3 scripts/fetch_data_sina.py                     # 配置内全部品种
  python3 scripts/fetch_data_sina.py --codes SHFE.cu.HOT DCE.m.HOT
  python3 scripts/fetch_data_sina.py --start 20180101 --end 20260901
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# WT 配置代码 → 新浪主力连续代码 (futures_main_sina 的 symbol)
SINA_SYMBOL = {
    "SHFE.rb.HOT": "RB0", "SHFE.hc.HOT": "HC0",
    "SHFE.cu.HOT": "CU0", "SHFE.al.HOT": "AL0",
    "SHFE.zn.HOT": "ZN0", "SHFE.ni.HOT": "NI0",
    "SHFE.au.HOT": "AU0", "SHFE.ag.HOT": "AG0",
    "SHFE.ru.HOT": "RU0", "SHFE.bu.HOT": "BU0",
    "SHFE.sp.HOT": "SP0",
    "INE.sc.HOT": "SC0",
    "CZCE.TA.HOT": "TA0", "CZCE.MA.HOT": "MA0",
    "CZCE.CF.HOT": "CF0", "CZCE.SR.HOT": "SR0",
    "CZCE.OI.HOT": "OI0", "CZCE.RM.HOT": "RM0",
    "CZCE.FG.HOT": "FG0", "CZCE.AP.HOT": "AP0",
    "DCE.m.HOT": "M0", "DCE.p.HOT": "P0",
    "DCE.l.HOT": "L0", "DCE.pp.HOT": "PP0",
    "DCE.i.HOT": "I0", "DCE.y.HOT": "Y0",
    "DCE.c.HOT": "C0", "DCE.a.HOT": "A0",
    "DCE.v.HOT": "V0", "DCE.eg.HOT": "EG0",
    "CFFEX.IF.HOT": "IF0", "CFFEX.IC.HOT": "IC0",
    "CFFEX.IH.HOT": "IH0", "CFFEX.IM.HOT": "IM0",
}


def fetch_daily(symbol: str, start: str, end: str) -> list:
    """akshare futures_main_sina → merge_rows 行列表 (纯函数可单测)"""
    import akshare as ak
    df = ak.futures_main_sina(symbol=symbol, start_date=start,
                              end_date=end)
    rows = []
    for _, r in df.iterrows():
        d = int(str(r["日期"]).replace("-", "").replace("/", ""))
        rows.append({
            "date": d, "time": 0,
            "open": float(r["开盘价"]), "high": float(r["最高价"]),
            "low": float(r["最低价"]), "close": float(r["收盘价"]),
            "volume": float(r["成交量"]),
            "open_interest": float(r["持仓量"]),
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=str,
                    default=os.path.join(ROOT, "config",
                                         "turtle_config.yaml"))
    ap.add_argument("--storage", type=str,
                    default=os.path.abspath(os.path.join(ROOT, "..",
                                                         "storage")))
    ap.add_argument("--codes", nargs="*", default=None,
                    help="缺省=配置内全部品种")
    ap.add_argument("--start", type=str, default="19900101")
    ap.add_argument("--end", type=str,
                    default=time.strftime("%Y%m%d"))
    ap.add_argument("--retry", type=int, default=2,
                    help="单品种失败重试次数")
    args = ap.parse_args()

    if args.codes:
        codes = args.codes
    else:
        from helpers.ConfigLoader import ConfigLoader
        cfg = ConfigLoader.load(args.config)
        codes = [inst.code for inst in cfg.instruments]

    from helpers.BarJournal import BarJournal
    import tempfile
    # merge_rows 不读写 journal 目录, 给个临时目录满足构造即可
    bj = BarJournal(tempfile.mkdtemp(prefix="sina_fetch_"))
    csv_dir = os.path.join(args.storage, "csv")
    os.makedirs(csv_dir, exist_ok=True)

    n_fail = 0
    for code in codes:
        symbol = SINA_SYMBOL.get(code)
        if not symbol:
            print(f"[sina] {code}: 无新浪代码映射, 跳过 "
                  f"(在 SINA_SYMBOL 中补充)")
            n_fail += 1
            continue
        rows = None
        for attempt in range(1 + args.retry):
            try:
                rows = fetch_daily(symbol, args.start, args.end)
                break
            except Exception as e:
                print(f"[sina] {code}({symbol}) 拉取失败 "
                      f"[{attempt + 1}/{1 + args.retry}]: {e}")
                time.sleep(2)
        if rows is None:
            n_fail += 1
            continue
        csv_path = os.path.join(csv_dir, f"{code}_d.csv")
        n = bj.merge_rows(rows, "d1", csv_path)
        print(f"[sina] {code}({symbol}): 拉取 {len(rows)} 行, "
              f"合并 +{n} → {csv_path}")
        time.sleep(0.5)  # 防新浪限流

    print(f"[sina] done: {len(codes) - n_fail}/{len(codes)} 成功")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
