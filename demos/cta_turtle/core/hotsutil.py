"""主力合约工具 (Phase 1, 2026-11-10) — 移仓新合约信号口径的基础设施。

用户拍板口径: **换月不做价格复权**; 移仓后指标/通道直接在**新合约自身
的真实价格序列**上计算 (其挂牌以来历史即窗口来源, 无任何调整), 持仓
层价移仓时按价差平移 (既有 _reanchor_on_roll 保留)。

本模块纯函数:
- hots.json 解析 (当前主力 / 全规则) — update_hot_daily/build_contract_
  signals/StraTurtle 三处单源;
- 统计换月检测: 从既有 HOT 拼接序列检测换月日 (跳空 k×ATR 或 OI 骤降),
  作为**低成本先行日历**, 后续由逐合约 OI 精化;
- 合约代码换算: WT 格式 (SHFE.rb.2701) ↔ Sina 格式 (RB2701 / CZCE TA701)。

口径注意: CZCE 代码 = 年尾数+月 (TA701=2027-01, 跨十年同码需按日期窗消歧);
SHFE/DCE/INE/CFFEX = 4 位年月。
"""
import json
import math
import os

# 各交易所主力合约轮换的候选交割月 (None=每月都是候选, 由 OI 判定)
_CYCLE_MONTHS = {
    ("SHFE", "rb"): (1, 5, 10),
    ("SHFE", "cu"): None,
    ("SHFE", "al"): None,
    ("INE", "sc"): None,
    ("CZCE", "TA"): (1, 5, 9),
    ("CZCE", "MA"): (1, 5, 9),
    ("CZCE", "CF"): (1, 5, 9),
    ("DCE", "pp"): (1, 5, 9),
    ("DCE", "l"): (1, 5, 9),
    ("DCE", "m"): (1, 5, 9),
    ("DCE", "p"): (1, 5, 9),
    ("CFFEX", "IF"): None,
}

_SINA_SUFFIX = {
    "SHFE": "", "DCE": "", "INE": "", "CFFEX": "",
    "CZCE": "XZCE", "CFFEX2": "",
}


def load_hots(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def split_code(code: str):
    """SHFE.rb.HOT -> (SHFE, rb); 真实合约 SHFE.rb.2701 -> (SHFE, rb)"""
    parts = code.split(".")
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None, None


def parse_contracts(hots: dict, codes: list) -> dict:
    """hots.json + 代码列表 -> {code: 当前主力合约 WT 格式 或 None}。

    hots 规则 {"SHFE": {"rb": [{"date":..., "to": "rb2701"}, ...]}}:
    取最后一条规则的 "to"; 键缺失/规则为空返回 None。
    """
    out = {}
    for code in codes:
        ex, prod = split_code(code)
        if not ex:
            out[code] = None
            continue
        rules = (hots.get(ex) or {}).get(prod)
        if not isinstance(rules, list) or not rules:
            out[code] = None
            continue
        last = rules[-1]
        out[code] = str(last.get("to") or "").strip() or None
    return out


def wt_contract(exchg: str, product: str, yymm: str) -> str:
    """(SHFE, rb, 2701) -> SHFE.rb.2701 ; (CZCE, TA, 701) -> CZCE.TA.701"""
    return f"{exchg}.{product}.{yymm}"


def sina_symbol(exchg: str, product: str, yymm: str) -> str:
    """Sina 合约符号: SHFE/DCE/INE/CFFEX 4 位; CZCE 3 位 (年尾数+月)。"""
    if exchg == "CZCE":
        if len(yymm) == 4:
            return f"{product}{int(yymm[:2]) % 10}{yymm[2:4]}"
        return f"{product}{yymm}"
    return f"{product.upper()}{yymm}"


def yymm_range(exchg: str, product: str, start_year: int,
               end_year: int) -> list:
    """候选合约年月列表 (cycle 规则展开; None=每月)。"""
    months = _CYCLE_MONTHS.get((exchg, product))
    out = []
    for y in range(start_year, end_year + 1):
        if months is None:
            for m in range(1, 13):
                out.append(f"{y % 100:02d}{m:02d}" if exchg != "CZCE"
                           else f"{y % 10}{m:02d}")
        else:
            for m in months:
                out.append(f"{y % 100:02d}{m:02d}" if exchg != "CZCE"
                           else f"{y % 10}{m:02d}")
    return out


def find_common(group_dir: str) -> str:
    """common 目录自适应: 本地开发 = <group>/../common; 服务器部署 =
    <group>/../../common (deploy/ 层)。显式存在者优先。"""
    for cand in (os.path.join(group_dir, "..", "common"),
                 os.path.join(group_dir, "..", "..", "common")):
        if os.path.exists(os.path.join(cand, "hots.json")):
            return os.path.abspath(cand)
    return os.path.abspath(os.path.join(group_dir, "..", "common"))


def align_window(hot_dates, highs, lows, closes,
                 sig_dates, sig_h, sig_l, sig_c,
                 min_bars: int):
    """合约信号窗口对齐 (Phase 1 移仓新合约口径, 纯函数)。

    hot_dates: HOT 主K日期序列 (驱动流); highs/lows/closes: 同长度 HOT
    窗口。sig_*: 当前主力合约自身序列 (真实价格, 无复权)。

    规则: 逐日取合约真实 OHLC; 合约上市前的主K日期**头部截断** (尾部
    对齐 — 评估只依赖窗口尾部); 上市后偶发缺失日 (停牌) 沿用前收
    (h=l=c); 末根主K日必须有合约数据且截断后长度 >= min_bars, 否则
    返回 None (调用方跳过本次评估)。

    返回 (highs, lows, closes) list 三元组 (尾部对齐, 长度可能短于
    输入) 或 None。
    """
    sig = {}
    for d, h, l, c in zip(sig_dates, sig_h, sig_l, sig_c):
        try:
            sig[int(d)] = (float(h), float(l), float(c))
        except (TypeError, ValueError):
            continue
    out_h, out_l, out_c = [], [], []
    for _i, d in enumerate(hot_dates):
        d = int(d)
        row = sig.get(d)
        if row is not None:
            out_h.append(row[0])
            out_l.append(row[1])
            out_c.append(row[2])
        elif out_c:
            pc = out_c[-1]
            out_h.append(pc)
            out_l.append(pc)
            out_c.append(pc)
        # 上市前 (尚无合约数据): 头部截断, 跳过
    if len(out_c) < min_bars:
        return None
    return out_h, out_l, out_c


def detect_rolls(dates, opens, prev_closes, ois=None,
                 atr_window: int = 20,
                 gap_atr_mult: float = 3.0,
                 gap_pct_min: float = 0.015,
                 oi_ratio_max: float = 0.65) -> list:
    """统计换月检测: [{date, delta, gap_pct, reason}]。

    信号: 隔夜跳空 > gap_atr_mult×ATR(atr_window) 且 > gap_pct_min,
    或 (提供 ois 时) 持仓量日比 < oi_ratio_max (主力切换 OI 断层)。
    delta = open - prev_close (换月价差, 供参考/校验, 不做复权)。
    连续触发去重: 3 个 bar 窗口内只保留首个。
    """
    n = len(dates)
    out = []
    last_hit = -10
    # 收盘-收盘 TR 的滚动均值作 ATR 代理 (检测用, 无需 Wilder 精度)
    atr = None
    for i in range(1, n):
        pc = float(prev_closes[i])
        o = float(opens[i])
        if not (math.isfinite(pc) and math.isfinite(o)) or pc <= 0:
            continue
        gap = o - pc
        gap_pct = abs(gap) / pc
        if i >= atr_window:
            trs = [abs(float(prev_closes[j]) - float(prev_closes[j - 1]))
                   for j in range(i - atr_window + 1, i + 1)]
            trs = [t for t in trs if math.isfinite(t)]
            atr = sum(trs) / len(trs) if trs else None
        hit = False
        reason = ""
        if atr is None or atr <= 0:
            hit = gap_pct > max(gap_pct_min, 0.03)
            reason = "gap"
        elif abs(gap) > gap_atr_mult * atr and gap_pct > gap_pct_min:
            hit = True
            reason = "gap"
        if not hit and ois is not None and i >= 1:
            oi0 = float(ois[i])
            oi1 = float(ois[i - 1])
            if oi1 > 0 and oi0 / oi1 < oi_ratio_max:
                hit = True
                reason = "oi"
        if hit and i - last_hit > 3:
            out.append({"date": int(dates[i]), "delta": gap,
                        "gap_pct": round(gap_pct, 6), "reason": reason})
            last_hit = i
    return out
