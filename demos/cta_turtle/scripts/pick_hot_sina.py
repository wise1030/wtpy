#!/usr/bin/env python3
"""新浪实时行情持仓量主力判定 → hots.json 规则生成/合并

对每个品种: contracts.json 筛未过期合约 → akshare futures_zh_spot 批量查
新浪实时行情 → 按持仓量最大判定当前主力 → 生成 hots.json 规则条目。

口径 (与远端 rb 手工条目一致):
  {"date": 当日, "from": 旧主力, "to": 新主力, "oldclose": 0.0, "newclose": 0.0}
  oldclose/newclose 置 0 (不做价格复权 — 实盘坑①只要求键存在+规则非空,
  换月价差由策略 re-anchor 处理)。

用途: 多品种上线/换月运维辅助; 替代 TqSdk fetch_hots 的免费兜底。
用法:
  python3 scripts/pick_hot_sina.py --common ../common            # 只打印判定
  python3 scripts/pick_hot_sina.py --common ../common --write    # 合并写 hots.json
"""
import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 品种 WT 代码 → (交易所, 品种pid); market: CF=商品期货, FF=金融期货
PRODUCTS = {
    "SHFE.cu": ("SHFE", "cu"), "SHFE.al": ("SHFE", "al"),
    "SHFE.rb": ("SHFE", "rb"), "SHFE.hc": ("SHFE", "hc"),
    "INE.sc": ("INE", "sc"),
    "CZCE.TA": ("CZCE", "TA"), "CZCE.MA": ("CZCE", "MA"),
    "CZCE.CF": ("CZCE", "CF"),
    "DCE.l": ("DCE", "l"), "DCE.pp": ("DCE", "pp"),
    "DCE.m": ("DCE", "m"), "DCE.p": ("DCE", "p"),
    "CFFEX.IF": ("CFFEX", "IF"),
}
INDEX_PRODUCTS = {"IF", "IC", "IH", "IM", "T", "TF", "TS"}

# 新浪 symbol 中文品种名 → 英文代码 (futures_zh_spot 返回"铜2610"风格,
# 无英文代码列, 需映射解析; 未收录品种的查询结果直接忽略)
CN2EN = {
    "铜": "CU", "铝": "AL", "螺纹钢": "RB", "热轧卷板": "HC",
    "上海原油": "SC",
    "PTA": "TA", "甲醇": "MA", "棉花": "CF",
    "塑料": "L", "聚丙烯": "PP", "豆粕": "M", "棕榈油": "P",
    "沪深300指数期货": "IF",
}


def _parse_symbol(cn_symbol: str) -> str:
    """"铜2610"/"沪深300指数期货2609" → "CU2610"/"IF2609"; 未映射返回空

    只取**尾部**数字 (品种名本身可能含数字, 如"沪深300指数期货")。"""
    i = len(cn_symbol)
    while i > 0 and cn_symbol[i - 1].isdigit():
        i -= 1
    digits, name = cn_symbol[i:], cn_symbol[:i]
    en = CN2EN.get(name, "")
    return en + digits if en and digits else ""


def _to_sina_code(exchg: str, pid: str, code: str, expiredate: int) -> str:
    """WT 合约代码 → 新浪代码。

    CZCE 特例: CTP 返回 3 位月份码 (TA610), 新浪要 4 位 (TA2610,
    2026-09-03 实测 3 位码接口报错) — 年份用 expiredate 补全:
    CZCE 到期日为合约月前一月, 故合约月 < 到期月时年份 +1
    (如 CF701 到期 2026-11 → 合约月 2027-01)。"""
    c = code.upper()
    if exchg == "CZCE" and len(c) == len(pid) + 3:
        mon = int(c[len(pid) + 1:])
        exp_year, exp_mon = expiredate // 10000, expiredate // 100 % 100
        year = exp_year + (1 if mon < exp_mon else 0)
        return pid + f"{year % 100:02d}{mon:02d}"
    return c


def active_contracts(contracts: dict, exchg: str, pid: str,
                     today: int, keep: int = 5) -> list:
    """contracts.json → 该品种最近 keep 个未到期合约 (新浪格式)

    主力必是近月合约之一, 全量查询太慢且易限流, 只保留到期日最近的
    keep 个候选 (按合约代码排序≈按到期月排序)。"""
    out = []
    for code, c in contracts.get(exchg, {}).items():
        if str(c.get("product", "")).lower() != pid.lower():
            continue
        exp = int(c.get("expiredate", 0) or 0)
        if exp and exp < today:
            continue
        out.append(_to_sina_code(exchg, pid, code, exp))
    return sorted(out)[:keep]


def parse_hq_text(text: str, batch: list, hold_idx: int) -> dict:
    """解析 hq.sinajs.cn 返回文本 → {请求代码(原样): 持仓量} (纯函数可单测)。

    响应形如 var hq_str_nf_RB2701="螺纹钢2701,230000,..."; 字段按逗号分隔,
    CF(商品) hold 在第 13 列 / FF(股指) 在第 6 列 (2026-09-05 实测 akshare
    futures_zh_sina 列定义对照); 无效代码返回空 payload (len<2) 静默跳过。
    """
    want = {s.upper(): s for s in batch}
    holds = {}
    for item in text.split(";"):
        item = item.strip()
        if "=" not in item:
            continue
        name, payload = item.split("=", 1)
        code = name.rsplit("nf_", 1)[-1].strip().upper()
        if code not in want:
            continue
        fields = payload.strip().strip('"').split(",")
        if len(fields) > hold_idx and fields[hold_idx]:
            try:
                holds[want[code]] = float(fields[hold_idx])
            except ValueError:
                pass
    return holds


def _fetch_sina_text(batch: list) -> str:
    """请求新浪实时行情原始文本 (stdlib, 3 次重试)"""
    import random
    import time
    import urllib.request
    sub = ",".join("nf_" + s for s in batch)
    url = f"https://hq.sinajs.cn/rn={random.getrandbits(31):x}&list={sub}"
    headers = {"Referer": "https://vip.stock.finance.sina.com.cn/",
               "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.read().decode("gbk", errors="ignore")
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2)


def query_holds(symbols: list, market: str) -> dict:
    """新浪批量查持仓: {合约代码: hold}

    stdlib 直连 hq.sinajs.cn (2026-09-05 重构: 去 akshare 重依赖,
    生产服务器 venv 无 akshare; akshare futures_zh_spot 底层即此接口)。
    """
    import time
    hold_idx = 6 if market == "FF" else 13
    holds = {}
    for i in range(0, len(symbols), 20):          # 20 个/批防限流
        batch = symbols[i:i + 20]
        text = _fetch_sina_text(batch)
        holds.update(parse_hq_text(text, batch, hold_idx))
        time.sleep(0.3)
    return holds


def pick_hots(candidates: dict) -> dict:
    """{品种key: [合约代码...]} → {品种key: (主力合约, 持仓量)}"""
    # 按 market 分组批量查询
    all_syms = {s for codes in candidates.values() for s in codes}
    idx_syms = [s for s in all_syms if _pid_of(s) in INDEX_PRODUCTS]
    cty_syms = [s for s in all_syms if _pid_of(s) not in INDEX_PRODUCTS]
    holds = {}
    if cty_syms:
        holds.update(query_holds(cty_syms, "CF"))
    if idx_syms:
        holds.update(query_holds(idx_syms, "FF"))
    out = {}
    for key, codes in candidates.items():
        best, best_hold = None, -1.0
        for c in codes:
            h = holds.get(c, 0.0) or 0.0
            if h > best_hold:
                best, best_hold = c, h
        out[key] = (best, best_hold)
    return out


def _pid_of(symbol: str) -> str:
    """新浪合约代码 → 品种字母部分"""
    return "".join(ch for ch in symbol if ch.isalpha())


def _to_wt_code(exchg: str, sina_code: str) -> str:
    """新浪合约代码 → WT 原生格式 (与 contracts.json 键一致)。

    CZCE: 4 位 → 3 位 (TA2701→TA701, 去年份十位); CFFEX 大写保持;
    其余交易所小写 (cu2610/l2701/sc2610)。hots.json 的 to 必须与
    contracts.json 代码完全一致, 否则 C++ 映射失败。"""
    if exchg == "CZCE":
        pid = _pid_of(sina_code)
        digits = sina_code[len(pid):]
        if len(digits) == 4:
            return pid + digits[1:]
        return sina_code
    if exchg == "CFFEX":
        return sina_code
    return sina_code.lower()


def merge_hots(hots: dict, picked: dict, today: int) -> int:
    """判定结果合并进 hots.json 结构; 返回变更品种数。

    规则: 与现有最后一条 to 不同 → 追加新条目; 相同 → 不动。
    """
    changed = 0
    for key, (hot, _hold) in picked.items():
        if not hot:
            continue
        exchg, pid = key.split(".", 1)
        hot = _to_wt_code(exchg, hot)
        lst = hots.setdefault(exchg, {}).setdefault(pid, [])
        cur = lst[-1]["to"] if lst else ""
        if cur.lower() == hot.lower():
            continue
        lst.append({"date": today, "from": cur, "to": hot,
                    "oldclose": 0.0, "newclose": 0.0})
        changed += 1
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--common", type=str,
                    default=os.path.abspath(os.path.join(ROOT, "..",
                                                         "common")))
    ap.add_argument("--write", action="store_true",
                    help="合并写入 hots.json (默认只打印)")
    ap.add_argument("--today", type=int, default=0,
                    help="判定日 YYYYMMDD, 缺省=今天")
    args = ap.parse_args()
    today = args.today or int(datetime.datetime.now().strftime("%Y%m%d"))

    with open(os.path.join(args.common, "contracts.json"),
              encoding="utf-8") as f:
        contracts = json.load(f)

    candidates = {}
    for key, (exchg, pid) in PRODUCTS.items():
        codes = active_contracts(contracts, exchg, pid, today)
        if codes:
            candidates[key] = codes
        else:
            print(f"[pick] {key}: 无未过期合约, 跳过")

    print(f"[pick] 查询 {sum(len(v) for v in candidates.values())} "
          f"个候选合约的实时持仓 ...")
    picked = pick_hots(candidates)
    for key in PRODUCTS:
        if key not in picked:
            continue
        hot, hold = picked[key]
        print(f"[pick] {key}: 主力={hot} 持仓={hold:.0f}")

    if not args.write:
        print("[pick] dry-run (加 --write 合并进 hots.json)")
        return 0

    hots_path = os.path.join(args.common, "hots.json")
    with open(hots_path, encoding="utf-8") as f:
        hots = json.load(f)
    changed = merge_hots(hots, picked, today)
    with open(hots_path, "w", encoding="utf-8") as f:
        json.dump(hots, f, ensure_ascii=False, indent=1)
    print(f"[pick] hots.json 更新 {changed} 个品种 -> {hots_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
