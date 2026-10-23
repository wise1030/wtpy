#!/usr/bin/env python3
"""共享账本组合回测 (2026-10-20, v1)

与 run_matrix 的区别:
- run_matrix: 每个品种独立进程/独立 RiskManager, 组合层只做曲线平均;
  `max_units_total` / 组上限 / 总风险 / 保证金从未参与决策 → 无约束上界。
- 本脚本: 12 个品种共用**一个** RiskManager/账本, 逐日推进;
  新开仓/加仓前统一过单品种/累计/总敞口/保证金/单位上限闸门。
  因此组合曲线是"实盘约束下"的可复现回测。

口径 (v1.1, 明确边界):
- close 模式 (默认): 信号用 bar 收盘确认, 成交价=收盘价(+滑点);
  止损按 min(open, stop)/max(open, stop) 处理跳空。
- trigger 模式 (--entry-mode trigger, 2026-10-21): 日线评估只做
  武装 (含当根包络), 盘中用 m5 bar 走引擎 check_bar 触碰仿真
  (跳空按开盘价, 止损→出场→加仓→入场 顺序, 每 bar 至多一个决策);
  无 m5 数据的品种退化为 d1 bar 触碰 (与实盘 bt_no_simtick 降级
  同口径)。交易时段归属: 交易日 T = T-1 夜盘 (21:00+, 日历日 T-1)
  + T 日盘; 武装发生在 T 开盘前 (对齐实盘 session_end(T-1) 评估)。
  过滤器=武装时前置裁决 (disarm_entry), 风控=触发时复核 (与实盘
  trigger 管线一致); 名额竞争按 m5 时间先后 (实盘 first-come)。
- 每品种一个 TurtleEngine, system 仅 S1/S2; S1+S2/多速度/过滤器
  可按需扩展 (过滤器已接线, 但 v1 默认无过滤)。
- 组合总资金 = --capital (默认 1500 万), 不再按品种各给一份;
  `max_margin_usage` 用 --margin-usage 覆盖 (默认 0.60, 全局口径;
  生产 config 的 0.10 是"每策略"口径, 不能直接用于总账户)。
- 手续费读 common/fees.json (byvolume / 比例两种); 滑点按 pricetick。
- 数据读 storage/csv/<code>_d.csv; 不处理换月价差 (HOT 连续已含)。

用法:
  python run_portfolio_bt.py --config config/turtle_config.yaml \
      --capital 15000000 --start 20190101 --end 20260831
  # 同时输出无组合约束对照 (相同信号/相同单笔风险, 只放开组合上限)
"""
import argparse
import csv
import dataclasses
import hashlib
import json
import os
import sys
from itertools import groupby

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)   # 项目根 (本文件在 research/ 下)
sys.path.insert(0, ROOT)

from core.ConfigLoader import ConfigLoader                 # noqa: E402
from core.PositionSizer import PositionSizer                # noqa: E402
from core.RiskManager import RiskManager                    # noqa: E402
from core.TurtleEngine import TurtleEngine                  # noqa: E402
from core.aggregate import aggregate_h1                     # noqa: E402
from core.indicators import wilder_atr, donchian            # noqa: E402
from filters import build_chain, FilterContext                 # noqa: E402
from core import portfolio as pf                            # noqa: E402


# ----------------------------------------------------------------------
# 数据 / 合约 / 费率
# ----------------------------------------------------------------------
def _date_int(s) -> int:
    digits = "".join(ch for ch in str(s) if ch.isdigit())
    return int(digits[:8]) if len(digits) >= 8 else 0


def load_bars(path: str) -> dict:
    """CSV → {dates, opens, highs, lows, closes, volumes, ois, index}"""
    dates, opens, highs, lows, closes, vols, ois = [], [], [], [], [], [], []
    try:
        with open(path, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                d = _date_int(r.get("date"))
                if not d:
                    continue
                try:
                    o = float(r["open"])
                    h = float(r["high"])
                    lo = float(r["low"])
                    c = float(r["close"])
                    v = float(r.get("volume") or 0.0)
                    oi = float(r.get("open_interest") or 0.0)
                except (KeyError, TypeError, ValueError):
                    continue
                dates.append(d)
                opens.append(o)
                highs.append(h)
                lows.append(lo)
                closes.append(c)
                vols.append(v)
                ois.append(oi)
    except OSError:
        return {}
    if not dates:
        return {}
    return {"dates": np.asarray(dates, dtype=np.int64),
            "opens": np.asarray(opens, dtype=float),
            "highs": np.asarray(highs, dtype=float),
            "lows": np.asarray(lows, dtype=float),
            "closes": np.asarray(closes, dtype=float),
            "volumes": np.asarray(vols, dtype=float),
            "ois": np.asarray(ois, dtype=float),
            "index": {int(d): i for i, d in enumerate(dates)}}


def load_m5(path: str) -> dict:
    """m5 CSV → {"rows": {date: [(time, o, h, l, c), ... 按 time 升序]},
    "dates": 排重升序日期数组}。缺文件/空文件返回 {}。"""
    rows = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                d = _date_int(r.get("date"))
                if not d:
                    continue
                t = "".join(ch for ch in str(r.get("time") or "")
                            if ch.isdigit())
                t = int(t[:6]) if len(t) >= 4 else 0
                try:
                    o = float(r["open"])
                    h = float(r["high"])
                    lo = float(r["low"])
                    c = float(r["close"])
                except (KeyError, TypeError, ValueError):
                    continue
                rows.setdefault(d, []).append((t, o, h, lo, c))
    except OSError:
        return {}
    for d in rows:
        rows[d].sort(key=lambda x: x[0])
    return {"rows": rows, "dates": sorted(rows)}


def load_m5_full(path: str) -> dict:
    """m5 CSV → {(date,time): (o,h,l,c,vol,oi)} 含成交量/持仓量。

    load_m5 只取 OHLC (trigger 触碰仿真够用); h1 聚合的过滤器上下文
    (LiquidityFloor 等) 需要 vol/oi, 故单列本函数 (S3)。
    """
    rows = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                d = _date_int(r.get("date"))
                if not d:
                    continue
                t = "".join(ch for ch in str(r.get("time") or "")
                            if ch.isdigit())
                t = int(t[:6]) if len(t) >= 4 else 0
                try:
                    rows[(d, t)] = (float(r["open"]), float(r["high"]),
                                    float(r["low"]), float(r["close"]),
                                    float(r.get("volume") or 0.0),
                                    float(r.get("open_interest") or 0.0))
                except (KeyError, TypeError, ValueError):
                    continue
    except OSError:
        return {}
    return rows


def build_h1_bars(m5_rows: dict, trading_days: list) -> dict:
    """m5 rows → h1 bars dict, 结构与 load_bars 对齐 + tdate/day_idx。

    - dates = h1 键 (tdate*10000 + 钟点 label), index 按此建;
    - tdate[i] = 桶归属交易日 (aggregate_h1 夜盘日历映射);
    - day_idx = {tdate: [i, ...]} 供主循环按日取 bar (免逐日全扫);
    - 尾部桶不排除: 历史 csv 的最后一个桶是文件末端的真实部分小时,
      研究口径全量使用 (实盘由 session_end flush 闭合, 语义同 h1 一期)。
    """
    rows = sorted((d, t) + v for (d, t), v in m5_rows.items())
    agg = aggregate_h1(rows, trading_days)
    keys = [int(k) for k in agg["keys"]]
    day_idx = {}
    for i, td in enumerate(agg["dates"]):
        day_idx.setdefault(int(td), []).append(i)
    return {"dates": np.asarray(keys, dtype=np.int64),
            "opens": np.asarray(agg["opens"], dtype=float),
            "highs": np.asarray(agg["highs"], dtype=float),
            "lows": np.asarray(agg["lows"], dtype=float),
            "closes": np.asarray(agg["closes"], dtype=float),
            "volumes": np.asarray(agg["volumes"], dtype=float),
            "ois": np.asarray(agg["ois"], dtype=float),
            "tdate": np.asarray(agg["dates"], dtype=np.int64),
            "index": {int(k): i for i, k in enumerate(keys)},
            "day_idx": day_idx}


def load_contracts(common_dir: str) -> dict:
    try:
        with open(os.path.join(common_dir, "commodities.json"),
                  encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    for ex, prods in (raw or {}).items():
        if not isinstance(prods, dict):
            continue
        for prod, info in prods.items():
            if isinstance(info, dict):
                out[(ex, prod)] = info
    return out


def load_fees(common_dir: str) -> dict:
    try:
        with open(os.path.join(common_dir, "fees.json"),
                  encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def contract_info(code: str, contracts: dict):
    parts = code.split(".")
    if len(parts) < 2:
        return None
    return contracts.get((parts[0], parts[1]))


def fee_cfg(code: str, fees: dict):
    parts = code.split(".")
    if len(parts) < 2:
        return {}
    return fees.get(f"{parts[0]}.{parts[1]}", {}) or {}


def fee_cost(fee: dict, side: str, price: float, lots: float,
             volscale: float, today: bool = False) -> float:
    """side = open|close; 返回手续费 (元)。byvolume=元/手, 否则按成交额比例。

    today=True 表示平掉的是**当仓** (开仓日==平仓日), 适用 closetoday 费率
    (DCE/CZCE 平今与隔夜平仓费率不同, 如 DCE.m open/close 1.5 元、
    closetoday 0 元; P2-2)。fee 无 closetoday 键时回退 close。
    close 模式日线 v1 下同日往返不可能 (入场在 exit 阶段之后确认),
    该参数主要为 trigger/m5 口径预留。
    """
    if not fee or lots <= 0:
        return 0.0
    if side == "close" and today and "closetoday" in fee:
        key = "closetoday"
    else:
        key = side if side in fee else ("close" if "close" in fee
                                        else "open")
    rate = float(fee.get(key, 0.0) or 0.0)
    if not rate:
        return 0.0
    if fee.get("byvolume"):
        return abs(lots) * rate
    return abs(price * volscale * lots) * rate


# ----------------------------------------------------------------------
# 指标 / 通道
# ----------------------------------------------------------------------
def build_channels(bars: dict, base, system: str) -> tuple:
    """返回 (atr, chan dict数组); 通道不含当前 bar, 与 TurtleEngine 一致。"""
    h, l, c = bars["highs"], bars["lows"], bars["closes"]
    atr = wilder_atr(h, l, c, base.atr_period)
    if system == "S2":
        en, ex, fs = base.s2_entry, base.s2_exit, base.s2_entry
    else:
        en, ex, fs = base.s1_entry, base.s1_exit, base.s2_entry
    eu, el = donchian(h, l, en)
    xu, xl = donchian(h, l, ex)
    fu, fl = donchian(h, l, fs)
    return atr, {
        "entry_upper": eu, "entry_lower": el,
        "exit_upper": xu, "exit_lower": xl,
        "fs_upper": fu, "fs_lower": fl,
    }


def channel_at(chan: dict, i: int) -> dict:
    return {
        "entry_upper": float(chan["entry_upper"][i]),
        "entry_lower": float(chan["entry_lower"][i]),
        "exit_upper": float(chan["exit_upper"][i]),
        "exit_lower": float(chan["exit_lower"][i]),
        "fs_upper": float(chan["fs_upper"][i]),
        "fs_lower": float(chan["fs_lower"][i]),
        "prev_entry_upper": float(chan["entry_upper"][i - 1]),
        "prev_entry_lower": float(chan["entry_lower"][i - 1]),
        "prev_exit_upper": float(chan["exit_upper"][i - 1]),
        "prev_exit_lower": float(chan["exit_lower"][i - 1]),
        "prev_fs_upper": float(chan["fs_upper"][i - 1]),
        "prev_fs_lower": float(chan["fs_lower"][i - 1]),
    }


def need_bars(base) -> int:
    return max(base.s2_entry, base.s1_entry, base.s2_exit,
               base.atr_period, base.s2_entry) + 2


# ----------------------------------------------------------------------
# 组合模拟
# ----------------------------------------------------------------------
class PortfolioSim:
    def __init__(self, cfg, contracts, fees, capital: float,
                 margin_usage: float, slippage: int,
                 max_units: int = None, max_group: int = None,
                 unconstrained: bool = False,
                 entry_mode: str = "close", data_dir: str = None,
                 period: str = "d1", codes: list = None):
        # period (S3, 2026-10-24): d1=日级推进 (默认) | h1=小时级推进
        #   (m5→h1 聚合, 窗口计数按 h1 bar 解释 = 更快的速度档, 与
        #   速度研究口径一致; 保证金/记录仍按交易日日界标记)。
        #   本地仅 rb/i/IF 有 m5 数据, 其余品种 skip+WARN。
        # codes: 品种子集过滤 (短名或全码), None=全部 — 用于同篮子
        #   d1/h1 对比 (h1_mix 相关/混合分析)。
        if period not in ("d1", "h1"):
            raise ValueError(f"period 非法 '{period}', 仅支持 d1|h1")
        if codes:
            want = set(codes)
            kept = [inst for inst in cfg.instruments
                    if inst.code in want
                    or inst.code.split(".")[1] in want]
            missing = want - {i.code.split(".")[1] for i in kept} \
                - {i.code for i in kept}
            if missing:
                raise SystemExit(f"[pbt] --codes 未匹配: {sorted(missing)}")
            cfg = dataclasses.replace(cfg, instruments=kept)
        risk_cfg = dataclasses.replace(
            cfg.risk, fallback_capital=float(capital),
            max_margin_usage=float(margin_usage),
            account_gate_mode="off")
        if unconstrained:
            risk_cfg = dataclasses.replace(
                risk_cfg, max_units_total=0, max_units_per_group=0,
                max_total_risk_frac=1.0, max_risk_per_market=1.0,
                max_margin_usage=1.0)
        elif max_units is not None:
            risk_cfg = dataclasses.replace(risk_cfg,
                                           max_units_total=max_units)
        elif max_group is not None:
            risk_cfg = dataclasses.replace(risk_cfg,
                                           max_units_per_group=max_group)
        if max_units is not None and not unconstrained:
            risk_cfg = dataclasses.replace(risk_cfg,
                                           max_units_total=max_units)
        if max_group is not None and not unconstrained:
            risk_cfg = dataclasses.replace(risk_cfg,
                                           max_units_per_group=max_group)

        self.cfg = cfg
        self.base = cfg.base
        self.entry_mode = entry_mode
        self.period = period
        if entry_mode not in ("close", "trigger"):
            raise ValueError(f"entry_mode 非法 '{entry_mode}'")
        self.capital = float(capital)
        self.margin_rate = float(cfg.risk.margin_rate)
        self.slippage = int(slippage)
        self.risk = RiskManager(risk_cfg)
        self.sizer = PositionSizer(self.base.unit_var)
        self.contracts = contracts
        self.fees = fees
        self.unconstrained = unconstrained
        self.priority = "config"

        self.engines = {}
        self.data = {}
        self.chans = {}
        self.marks = {}
        self.priceticks = {}
        self.volscales = {}
        self.groups = {}
        self.units = {}
        self.chains = {}
        self.open_dates = {}    # code -> 首层开仓交易日 (平今费率判定, P2-2)
        self.m5 = {}            # code -> load_m5 结果 (trigger d1 模式)
        self._armed_upto = {}   # code -> h1 trigger 已武装到的 bar 下标 (S3)
        self.realized = 0.0
        self.fee_paid = 0.0
        self.trades = 0
        self.rejected = 0
        self.margin_peak = 0.0
        self.margin_breach_days = 0

        for inst in cfg.instruments:
            code = inst.code
            csv_dir = data_dir or os.path.join(ROOT, "..", "storage", "csv")
            path = os.path.join(csv_dir, f"{code}_d.csv")
            bars = load_bars(path)
            info = contract_info(code, contracts)
            if not bars or not info:
                print(f"[pbt] WARN {code}: 缺数据/合约信息, 跳过")
                continue
            if inst.system not in ("S1", "S2"):
                raise SystemExit(
                    f"[pbt] {code}: v1 仅支持 S1/S2, 当前 {inst.system}")
            vs = float(info.get("volscale") or 0)
            pt = float(info.get("pricetick") or 0)
            if vs <= 0 or pt <= 0:
                print(f"[pbt] WARN {code}: volscale/pricetick 非法, 跳过")
                continue
            if period == "h1":
                # S3: m5 → h1 聚合; 无 m5 数据的品种跳过 (本地仅
                # rb/i/IF 有 m5)。交易日历取该品种 d1 csv 的日期序列
                # (夜盘归属映射的权威日历)。
                m5_rows = load_m5_full(os.path.join(
                    csv_dir, f"{code}_m5.csv"))
                if not m5_rows:
                    print(f"[pbt] WARN {code}: h1 模式需要 m5 数据, 跳过")
                    continue
                bars = build_h1_bars(
                    m5_rows, sorted(int(x) for x in bars["dates"]))
            self.data[code] = bars
            self.volscales[code] = vs
            self.priceticks[code] = pt
            self.groups[code] = inst.group or ""
            self.units[code] = float(inst.units)
            self.marks[code] = None
            self.engines[code] = TurtleEngine(
                system=inst.system,
                stop_atr_mult=self.base.stop_atr_mult,
                pyramid_layers=self.base.pyramid_layers,
                pyramid_step_atr=self.base.pyramid_step_atr,
                enable_s1_filter=self.base.enable_s1_filter,
                entry_mode=entry_mode,
                stop_n_mode=self.base.stop_n_mode,
                trigger_envelope=self.base.trigger_envelope,
                allow_same_bar_reversal=self.base.allow_same_bar_reversal,
                gap_stop=self.base.gap_stop,
                arm_exclude_signal=self.base.arm_exclude_signal)
            atr, chan = build_channels(bars, self.base, inst.system)
            self.chans[code] = {"atr": atr, "chan": chan}
            self.chains[code] = build_chain(inst)
            if entry_mode == "trigger" and period == "d1":
                m5 = load_m5(os.path.join(csv_dir, f"{code}_m5.csv"))
                if m5:
                    self.m5[code] = m5
                else:
                    print(f"[pbt] {code}: 无 m5 数据, trigger 退化为 "
                          f"d1 bar 触碰口径 (同实盘 bt_no_simtick)")
        if not self.data:
            raise SystemExit("[pbt] 无可用品种数据")

    # -- 估值 ---------------------------------------------------------
    def _unrealized(self) -> float:
        total = 0.0
        for code, e in self.engines.items():
            if e.direction == 0 or not e.layers:
                continue
            mark = self.marks.get(code)
            if mark is None:
                continue
            vs = self.volscales[code]
            total += sum((mark - px) * e.direction * lots * vs
                         for lots, px in e.layers)
        return total

    def _gross_margin(self) -> float:
        total = 0.0
        for code, e in self.engines.items():
            if e.direction == 0 or not e.layers:
                continue
            mark = self.marks.get(code) or e.layers[-1][1]
            lots = sum(l for l, _ in e.layers)
            total += lots * mark * self.volscales[code] * self.margin_rate
        return total

    def equity(self) -> float:
        return self.capital + self.realized + self._unrealized() - self.fee_paid

    def _fill(self, code: str, side: str, price: float,
              direction: int) -> float:
        """side=open/close, direction=+1/-1; 返回含滑点的成交价。

        滑点永远朝不利方向: 开多/平空加价, 开空/平多加价。
        """
        if self.slippage <= 0:
            return price
        pt = self.priceticks[code]
        sign = direction if side == "open" else -direction
        return price + sign * self.slippage * pt

    def _strength(self, code: str, i: int, n: float, dec: dict) -> float:
        """入场强度: 收盘超过突破位的距离/N (用于名额竞争排序)。"""
        d = int(dec.get("direction") or 0)
        ch = self.chans[code]["chan"]
        if d > 0:
            lv = (ch["fs_upper"][i] if dec.get("failsafe")
                  else ch["entry_upper"][i])
            return max(0.0, (float(dec["price"]) - float(lv)) / n)
        lv = (ch["fs_lower"][i] if dec.get("failsafe")
              else ch["entry_lower"][i])
        return max(0.0, (float(lv) - float(dec["price"])) / n)

    def _cap_margin_mark(self, final: int, price: float, code: str,
                         equity: float) -> int:
        """按**当前 mark** 的全局保证金再收紧一次。

        RiskManager 的 margin 账本按建仓均价记, 趋势中浮盈会把当前
        保证金放大 (本回测 60M/0.6 预算实测峰值 52M); 这里用当日
        mark 的已占保证金 + 本笔预估, 把全局占用压在预算内。
        """
        if final <= 0:
            return 0
        budget = equity * float(self.risk.cfg.max_margin_usage)
        used = self._gross_margin()
        per_lot = price * self.volscales[code] * self.margin_rate
        if per_lot <= 0:
            return final
        afford = int(max(0.0, budget - used) // per_lot)
        return min(final, max(0, afford))

    def _sync_book(self, code: str):
        e = self.engines[code]
        if e.direction == 0 or not e.layers:
            self.risk.on_closed(code)
            return
        lots = sum(l for l, _ in e.layers)
        avg = sum(l * p for l, p in e.layers) / lots
        self.risk.on_rebuilt(
            code, e.direction, lots, avg, e._n or 1.0,
            self.volscales[code], self.base.stop_atr_mult,
            group=self.groups[code],
            risk=e.booked_risk(self.volscales[code],
                               self.base.stop_atr_mult,
                               stop=e.stop_anchor()),
            sid="", units=self.units[code])

    # -- 决策 ---------------------------------------------------------
    def _filter_ok(self, code: str, i: int, direction: int,
                   price: float, n: float) -> bool:
        chain = self.chains.get(code) or []
        if not chain:
            return True
        bars = self.data[code]
        ctx = FilterContext(
            code=code, sig_dir=direction, price=price, n=n,
            n_series=self.chans[code]["atr"][:i + 1],
            highs=bars["highs"][:i + 1], lows=bars["lows"][:i + 1],
            closes=bars["closes"][:i + 1],
            volumes=bars["volumes"][:i + 1],
            open_interests=bars["ois"][:i + 1])
        for flt in chain:
            try:
                ok, why = flt.allow_entry(ctx)
            except Exception:
                ok = getattr(flt, "on_error", "allow") != "deny"
            if not ok:
                return False
        return True

    def _entry(self, code: str, i: int, dec: dict, n: float):
        e = self.engines[code]
        d = int(dec["direction"])
        close = float(self.data[code]["closes"][i])
        fill = self._fill(code, "open", close, d)
        if not self._filter_ok(code, i, d, fill, n):
            return
        final = self._size_cap(code, fill, n)
        if final <= 0:
            self.rejected += 1
            return
        self.fee_paid += fee_cost(
            fee_cfg(code, self.fees), "open", fill, final,
            self.volscales[code])
        e.confirm_enter(d, final, fill, n)
        self.open_dates[code] = int(self.data[code]["dates"][i])
        self.trades += 1
        self._sync_book(code)

    def _add(self, code: str, i: int, dec: dict, n: float):
        e = self.engines[code]
        close = float(self.data[code]["closes"][i])
        fill = self._fill(code, "open", close, e.direction)
        final = self._size_cap(code, fill, n)
        if final <= 0:
            self.rejected += 1
            return
        self.fee_paid += fee_cost(
            fee_cfg(code, self.fees), "open", fill, final,
            self.volscales[code])
        e.confirm_add(final, fill, n)
        self.trades += 1
        self._sync_book(code)

    def _exit(self, code: str, i: int, dec: dict, tdate: int = None):
        e = self.engines[code]
        if e.direction == 0 or not e.layers:
            return
        bars = self.data[code]
        raw = float(dec["price"])
        if dec.get("reason") == "stop" and self.entry_mode == "close" \
                and self.base.gap_stop:
            # 跳空: 多头 open < stop → 按 open; 空头反之
            # (trigger 模式 check_bar 已内嵌跳空口径, 不再二次调整;
            # S4-3: 引擎层已按 open 成交时此处 min/max 幂等; --no-gap-stop
            # 时两层同时关闭, 回退旧"按止损价"口径供 A/B)
            if e.direction > 0:
                raw = min(raw, float(bars["opens"][i]))
            else:
                raw = max(raw, float(bars["opens"][i]))
        fill = self._fill(code, "close", raw, e.direction)
        lots = sum(l for l, _ in e.layers)
        avg = sum(l * p for l, p in e.layers) / lots
        vs = self.volscales[code]
        gross = (fill - avg) * e.direction * lots * vs
        self.realized += gross
        # 平今判定: trigger 模式按**交易日** (夜盘日历日 T-1 属交易日 T),
        # 故由调用方显式传入 tdate; close 模式 tdate=None → 用 d1 bar 日期
        today = self.open_dates.get(code) == int(
            tdate if tdate is not None else bars["dates"][i])
        self.fee_paid += fee_cost(
            fee_cfg(code, self.fees), "close", fill, lots, vs, today=today)
        e.confirm_exit(fill)
        self.open_dates.pop(code, None)
        self.risk.on_closed(code)
        self.trades += 1

    def _size_cap(self, code: str, fill: float, n: float) -> int:
        """尺寸→风控闸门链 (close/trigger 共用): 返回 final lots, <=0=否决"""
        lots = self.sizer.layer_lots(n, self.volscales[code], self.units[code])
        if lots <= 0:
            return 0
        final, _reasons = self.risk.cap_entry(
            code, lots=lots, n=n, volscale=self.volscales[code],
            price=fill, stop_mult=self.base.stop_atr_mult,
            equity=self.equity(), group=self.groups[code], share=1.0,
            sid="")
        if final > 0:
            final = self._cap_margin_mark(final, fill, code, self.equity())
        return final

    # -- trigger 模式: 日线武装 + m5 触碰触发 (2026-10-21) ------------
    def _arm_day(self, code: str, i: int):
        """交易日 T(=d1 index i) 开盘前武装, 对齐实盘 session_end(T-1) 评估:
        T-1 的 d1 OHLC + 含当根包络 (chan[i] 不含 bar i = 含 T-1) + atr[i-1]。
        过滤器武装时前置裁决, 被否决方向当日解除武装 (与实盘 trigger 管线一致)。"""
        e = self.engines[code]
        bars = self.data[code]
        if i < need_bars(self.base) or i < 2:
            return
        chan = channel_at(self.chans[code]["chan"], i)
        if any(not np.isfinite(v) for v in chan.values()):
            return
        n = float(self.chans[code]["atr"][i - 1])
        if not np.isfinite(n) or n <= 0:
            return
        decs = e.on_bar(float(bars["highs"][i - 1]),
                        float(bars["lows"][i - 1]),
                        float(bars["closes"][i - 1]),
                        float(bars["closes"][i - 2]), n, chan)
        for dec in decs:
            if dec.get("decision") != "arm":
                continue
            plan = dec.get("plan") or {}
            for d, keys in ((1, ("long", "fs_long")),
                            (-1, ("short", "fs_short"))):
                lv = [plan[k] for k in keys if plan.get(k) is not None]
                if not lv:
                    continue
                px = min(lv) if d > 0 else max(lv)
                # 数据窗口到 T-1 ([:i]), 与武装评估同口径
                if not self._filter_ok(code, i - 1, d, px, n):
                    e.disarm_entry(d)

    def _day_m5_bars(self, code: str, d: int, prev_d: int) -> list:
        """交易日 T 的 m5 序列: T-1 夜盘 (日历日 T-1, time>=200000) + T 凌晨
        续盘与日盘 (日历日 T, time<200000), 按 (date, time) 升序。
        无 m5 数据返回空 → 调用方退化 d1 bar 触碰 (实盘 bt_no_simtick 同口径)。"""
        m5 = self.m5.get(code)
        if not m5 or not prev_d:
            return []
        rows = m5["rows"]
        out = [(prev_d, t, o, h, l, c)
               for (t, o, h, l, c) in rows.get(prev_d, []) if t >= 200000]
        out += [(d, t, o, h, l, c)
                for (t, o, h, l, c) in rows.get(d, []) if t < 200000]
        out.sort(key=lambda x: (x[0], x[1]))
        return out

    def _dispatch_trigger(self, code: str, i: int, d: int, dec: dict):
        """盘中触发决策**立即**确认/否决 —— 与实盘"决策与确认在同一
        on_tick 完成"的不变量一致, pending 不跨 bar。"""
        e = self.engines[code]
        kind = dec.get("decision")
        if kind == "exit":
            self._exit(code, i, dec, tdate=d)
        elif kind == "enter":
            self._trigger_open(code, d, dec, "enter")
        elif kind == "add":
            self._trigger_open(code, d, dec, "add")
        # "skip" = S1 过滤器内部已处理 (sim 单 + fail-safe 武装), 无动作
        if e._pending is not None:      # 兜底: 决策必须被消费
            e.clear_pending()

    def _trigger_open(self, code: str, d: int, dec: dict, kind: str):
        """入场/加仓触发: 风控触发时复核 (与实盘一致), 否决则 reject_pending
        (当日该方向不再重复触发)。"""
        e = self.engines[code]
        n = float(dec.get("n") or e._n or 0.0)
        fill = self._fill(code, "open", float(dec["price"]),
                          int(dec.get("direction") or e.direction))
        final = self._size_cap(code, fill, n)
        if final <= 0:
            self.rejected += 1
            e.reject_pending()
            return
        self.fee_paid += fee_cost(fee_cfg(code, self.fees), "open", fill,
                                  final, self.volscales[code])
        if kind == "enter":
            e.confirm_enter(int(dec["direction"]), final, fill, n)
            self.open_dates[code] = d
        else:
            e.confirm_add(final, fill, n)
        self.trades += 1
        self._sync_book(code)

    # -- 主循环 -------------------------------------------------------
    def _run_close_date(self, d: int):
        """close 模式单日: 两段式收集全部决策 → exit→entry(可排序)→add。
        旧实现按配置顺序逐个处理, 名额满时后面的强信号永远进不来,
        选择顺序会主导组合收益 (2026-10-20)。"""
        need = need_bars(self.base)
        pending = []
        for inst in self.cfg.instruments:
            code = inst.code
            if code not in self.engines:
                continue
            bars = self.data[code]
            i = bars["index"].get(d)
            if i is None or i < need:
                continue
            n = float(self.chans[code]["atr"][i])
            if not np.isfinite(n) or n <= 0:
                continue
            chan = channel_at(self.chans[code]["chan"], i)
            if any(not np.isfinite(v) for v in chan.values()):
                continue
            decs = self.engines[code].on_bar(
                float(bars["highs"][i]), float(bars["lows"][i]),
                float(bars["closes"][i]), float(bars["closes"][i - 1]),
                n, chan)
            pending.append((code, i, n, decs))
        # 1) 出场先释放单位
        for code, i, _n, decs in pending:
            for dec in decs:
                if dec.get("decision") == "exit":
                    self._exit(code, i, dec)
        # 2) 入场: config=配置顺序; strength=突破距离/N 降序
        entries = []
        for code, i, n, decs in pending:
            for dec in decs:
                if dec.get("decision") == "enter":
                    entries.append((self._strength(code, i, n, dec),
                                    code, i, n, dec))
        if self.priority == "strength":
            entries.sort(key=lambda x: (-x[0], x[1]))
        for _s, code, i, n, dec in entries:
            self._entry(code, i, dec, n)
        # 3) 加仓 (不改变单位数, 最后处理)
        for code, i, n, decs in pending:
            for dec in decs:
                if dec.get("decision") == "add":
                    self._add(code, i, dec, n)

    def _run_trigger_date(self, d: int):
        """trigger 模式单日: 先全品种武装 (session_end(T-1) 口径), 再按
        m5 时间顺序跨品种触发 —— 名额竞争=实盘 first-come 语义。
        无 m5 品种退化为 d1 bar 触碰, 事件排在日盘开始 (09:00 槽, 配置序)。"""
        need = need_bars(self.base)
        streams = []    # (cal_date, time, cfg_order, code, (o,h,l,c))
        for order, inst in enumerate(self.cfg.instruments):
            code = inst.code
            if code not in self.engines:
                continue
            bars = self.data[code]
            i = bars["index"].get(d)
            if i is None:
                continue
            self._arm_day(code, i)
            prev_d = int(bars["dates"][i - 1]) if i >= 1 else 0
            m5bars = self._day_m5_bars(code, d, prev_d)
            if m5bars:
                for (md, t, o, h, l, c) in m5bars:
                    streams.append((md, t, order, code, (o, h, l, c)))
            elif i >= need:
                streams.append((d, 90000, order, code, (
                    float(bars["opens"][i]), float(bars["highs"][i]),
                    float(bars["lows"][i]), float(bars["closes"][i]))))
        streams.sort(key=lambda x: (x[0], x[1], x[2]))
        for _md, _t, _order, code, bar in streams:
            decs = self.engines[code].check_bar(*bar)
            i = self.data[code]["index"].get(d, 0)
            for dec in decs:
                self._dispatch_trigger(code, i, d, dec)

    # -- h1 模式: 小时级推进 + 日界标记 (S3, 2026-10-24) --------------
    def _arm_h1(self, code: str, j: int):
        """为 h1 bar j 武装: 用 bar j-1 的 OHLC + chan[j] (不含 bar j)
        + atr[j-1], 与 d1 trigger 的 session_end(T-1) 口径同构。
        过滤器武装时前置裁决 (disarm_entry), 与实盘 trigger 管线一致。"""
        e = self.engines[code]
        bars = self.data[code]
        need = need_bars(self.base)
        if j < need or j < 2:
            return
        chan = channel_at(self.chans[code]["chan"], j)
        if any(not np.isfinite(v) for v in chan.values()):
            return
        n = float(self.chans[code]["atr"][j - 1])
        if not np.isfinite(n) or n <= 0:
            return
        decs = e.on_bar(float(bars["highs"][j - 1]),
                        float(bars["lows"][j - 1]),
                        float(bars["closes"][j - 1]),
                        float(bars["closes"][j - 2]), n, chan,
                        open=float(bars["opens"][j - 1]))
        for dec in decs:
            if dec.get("decision") != "arm":
                continue
            plan = dec.get("plan") or {}
            for d, keys in ((1, ("long", "fs_long")),
                            (-1, ("short", "fs_short"))):
                lv = [plan[k] for k in keys if plan.get(k) is not None]
                if not lv:
                    continue
                px = min(lv) if d > 0 else max(lv)
                if not self._filter_ok(code, j - 1, d, px, n):
                    e.disarm_entry(d)

    def _h1_slots(self, d: int) -> list:
        """当日全部品种的 h1 bar 槽位, 按 (label, 配置序) 升序。"""
        slots = []
        for order, inst in enumerate(self.cfg.instruments):
            code = inst.code
            if code not in self.engines:
                continue
            bars = self.data[code]
            for i in bars["day_idx"].get(d, ()):
                slots.append((int(bars["dates"][i]) % 10000, order, code, i))
        slots.sort()
        return slots

    def _run_h1_day_close(self, d: int, slots: list):
        """h1 close 模式单日: 按桶分组 (同桶跨品种), 组内 exit→entry→add。

        与 d1 close 的两段式同构, 只是决策粒度从"日"细化到"小时桶";
        mark 随每根 bar 收盘更新 (决策价=桶收盘, 与 d1 的"mark=日收盘"
        同口径无前视)。"""
        need = need_bars(self.base)
        for _label, grp in groupby(slots, key=lambda x: x[0]):
            pending = []
            for _l, _o, code, i in grp:
                bars = self.data[code]
                if i < need:
                    continue
                n = float(self.chans[code]["atr"][i])
                if not np.isfinite(n) or n <= 0:
                    continue
                chan = channel_at(self.chans[code]["chan"], i)
                if any(not np.isfinite(v) for v in chan.values()):
                    continue
                self.marks[code] = float(bars["closes"][i])
                decs = self.engines[code].on_bar(
                    float(bars["highs"][i]), float(bars["lows"][i]),
                    float(bars["closes"][i]), float(bars["closes"][i - 1]),
                    n, chan, open=float(bars["opens"][i]))
                pending.append((code, i, n, decs))
            for code, i, _n, decs in pending:
                for dec in decs:
                    if dec.get("decision") == "exit":
                        self._exit(code, i, dec, tdate=d)
            entries = []
            for code, i, n, decs in pending:
                for dec in decs:
                    if dec.get("decision") == "enter":
                        entries.append((self._strength(code, i, n, dec),
                                        code, i, n, dec))
            if self.priority == "strength":
                entries.sort(key=lambda x: (-x[0], x[1]))
            for _s, code, i, n, dec in entries:
                self._entry(code, i, dec, n)
            for code, i, n, decs in pending:
                for dec in decs:
                    if dec.get("decision") == "add":
                        self._add(code, i, dec, n)

    def _run_h1_day_trigger(self, d: int, slots: list):
        """h1 trigger 模式单日: 每根 bar 前先补武装 (catch-up 跨日连续),
        再 check_bar 触碰仿真; 决策与确认同 bar 完成 (pending 不跨 bar)。"""
        need = need_bars(self.base)
        for _l, _o, code, i in slots:
            e = self.engines[code]
            upto = self._armed_upto.get(code, need)
            for k in range(max(upto, need), i):
                self._arm_h1(code, k)
            self._armed_upto[code] = max(upto, i)
            bars = self.data[code]
            self.marks[code] = float(bars["closes"][i])
            decs = e.check_bar(float(bars["opens"][i]),
                               float(bars["highs"][i]),
                               float(bars["lows"][i]),
                               float(bars["closes"][i]))
            for dec in decs:
                self._dispatch_trigger(code, i, d, dec)

    def _run_h1(self, start: int, end: int) -> dict:
        all_days = sorted({int(t) for b in self.data.values()
                           for t in np.unique(b["tdate"])})
        eval_days = [d for d in all_days if start <= d <= end]
        daily = []
        for d in eval_days:
            slots = self._h1_slots(d)
            if self.entry_mode == "trigger":
                self._run_h1_day_trigger(d, slots)
            else:
                self._run_h1_day_close(d, slots)
            self._record_day(d, daily)
        return {
            "daily": daily,
            "eval_start": daily[0]["date"] if daily else None,
            "eval_end": daily[-1]["date"] if daily else None,
            "n_days": len(daily),
            "realized": self.realized,
            "fees": self.fee_paid,
            "trades": self.trades,
            "rejected": self.rejected,
            "margin_peak": self.margin_peak,
            "margin_breach_days": self.margin_breach_days,
            "capital": self.capital,
            "unconstrained": self.unconstrained,
            "entry_mode": self.entry_mode,
        }

    def _record_day(self, d: int, daily: list):
        eq = self.equity()
        margin = self._gross_margin()
        self.margin_peak = max(self.margin_peak, margin)
        if eq > 0 and margin > eq * float(
                self.risk.cfg.max_margin_usage):
            self.margin_breach_days += 1
        daily.append({
            "date": d,
            "equity": eq,
            "pnl": eq - self.capital,
            "margin": margin,
            "active_units": self.risk.open_units,
            "positions": sum(1 for e in self.engines.values()
                             if e.direction != 0),
        })

    def run(self, start: int, end: int) -> dict:
        if self.period == "h1":
            return self._run_h1(start, end)
        all_dates = sorted({int(d) for b in self.data.values()
                            for d in b["dates"]})
        eval_dates = [d for d in all_dates if start <= d <= end]
        daily = []
        for d in eval_dates:
            # 先按当日收盘更新 mark (无当日 bar 的品种保留上一 mark)
            for code, bars in self.data.items():
                i = bars["index"].get(d)
                if i is not None:
                    self.marks[code] = float(bars["closes"][i])
            if self.entry_mode == "trigger":
                self._run_trigger_date(d)
            else:
                self._run_close_date(d)
            self._record_day(d, daily)
        return {
            "daily": daily,
            # 实际评估区间 (P2-5): 请求 start/end 可能被数据覆盖截断,
            # 调用方应与请求区间比对并告警, 不能静默当全窗口成绩
            "eval_start": daily[0]["date"] if daily else None,
            "eval_end": daily[-1]["date"] if daily else None,
            "n_days": len(daily),
            "realized": self.realized,
            "fees": self.fee_paid,
            "trades": self.trades,
            "rejected": self.rejected,
            "margin_peak": self.margin_peak,
            "margin_breach_days": self.margin_breach_days,
            "capital": self.capital,
            "unconstrained": self.unconstrained,
            "entry_mode": self.entry_mode,
        }


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------
def _metrics(daily: list, capital: float) -> dict:
    if not daily:
        return {}
    dates = [x["date"] for x in daily]
    pnl = np.asarray([x["pnl"] for x in daily], dtype=float)
    return pf.portfolio_metrics(dates, pnl, capital, rf=0.0)


def _hash_file(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(
        ROOT, "config", "turtle_config.yaml"))
    ap.add_argument("--capital", type=float, default=60_000_000,
                    help="组合总资金 (不是每品种一份); 默认 6000 万 = "
                         "12×500 万, 与旧矩阵口径对齐; 实盘共享账户按实际权益传")
    ap.add_argument("--start", type=int, default=20190101)
    ap.add_argument("--end", type=int, default=20260831)
    ap.add_argument("--slippage", type=int, default=1,
                    help="单边滑点 tick 数")
    ap.add_argument("--margin-usage", type=float, default=0.60,
                    help="全局保证金占用上限 (生产 config 的 0.10 是每策略)")
    ap.add_argument("--priority", choices=["config", "strength"],
                    default="config",
                    help="名额满时的入场优先级 (config=配置顺序/strength=突破强度;"
                         "仅 close 模式生效, trigger 按 m5 时间 first-come)")
    ap.add_argument("--entry-mode", choices=["close", "trigger"],
                    default="close",
                    help="trigger=日线武装+m5 触碰仿真 (2026-10-21); "
                         "无 m5 品种自动退化 d1 触碰")
    ap.add_argument("--period", choices=["d1", "h1"], default="d1",
                    help="h1=m5→h1 聚合小时级推进 (S3, 2026-10-24); "
                         "窗口计数按 h1 bar 解释=更快速度档; 仅本地有 "
                         "m5 数据的品种参与")
    ap.add_argument("--codes", default=None,
                    help="品种子集 (逗号分隔, 短名或全码), 用于同篮子 "
                         "d1/h1 对比; 默认全部")
    ap.add_argument("--no-envelope", action="store_true",
                    help="S4-1 A/B: trigger 武装位用纯通道")
    ap.add_argument("--arm-exclude-signal", action="store_true",
                    help="S4-1b A/B: trigger 武装位用突破前 N 日通道 "
                         "(严格经典, 排除信号 bar; A/B 显著更优, 待拍板)")
    ap.add_argument("--no-same-bar-reversal", action="store_true",
                    help="S4-2 A/B: close 模式禁止同 bar 出场后再入场")
    ap.add_argument("--no-gap-stop", action="store_true",
                    help="S4-3 A/B: 回退旧'止损按止损价成交'口径")
    ap.add_argument("--tag", default=None,
                    help="产出文件名附加后缀 (A/B 变体区分, 不覆盖基准)")
    ap.add_argument("--max-units", type=int, default=None)
    ap.add_argument("--max-group", type=int, default=None)
    ap.add_argument("--out-dir", default=os.path.join(ROOT,
                                                        "matrix_results"))
    args = ap.parse_args()

    cfg = ConfigLoader.load(args.config, warn=False)
    # S4 A/B: CLI 覆盖配置口径 (变体跑, 不改配置文件)
    if args.no_envelope or args.no_same_bar_reversal or args.no_gap_stop \
            or args.arm_exclude_signal:
        cfg = dataclasses.replace(
            cfg, base=dataclasses.replace(
                cfg.base,
                trigger_envelope=not args.no_envelope,
                allow_same_bar_reversal=not args.no_same_bar_reversal,
                gap_stop=not args.no_gap_stop,
                arm_exclude_signal=args.arm_exclude_signal))
    codes = [c.strip() for c in args.codes.split(",")] \
        if args.codes else None
    common = os.path.abspath(os.path.join(ROOT, "..", "common"))
    contracts = load_contracts(common)
    fees = load_fees(common)

    base_sim = PortfolioSim(cfg, contracts, fees, args.capital,
                            args.margin_usage, args.slippage,
                            max_units=args.max_units,
                            max_group=args.max_group,
                            entry_mode=args.entry_mode,
                            period=args.period, codes=codes)
    base_sim.priority = args.priority
    base = base_sim.run(args.start, args.end)
    unc_sim = PortfolioSim(cfg, contracts, fees, args.capital,
                           args.margin_usage, args.slippage,
                           unconstrained=True,
                           entry_mode=args.entry_mode,
                           period=args.period, codes=codes)
    unc_sim.priority = args.priority
    unc = unc_sim.run(args.start, args.end)

    os.makedirs(args.out_dir, exist_ok=True)
    # 非默认模式的产出加后缀, 避免覆盖 close 模式基准工件
    parts = []
    if args.period != "d1":
        parts.append(args.period)
    if args.entry_mode != "close":
        parts.append(args.entry_mode)
    if args.tag:
        parts.append(args.tag)
    suffix = "_" + "_".join(parts) if parts else ""
    csv_path = os.path.join(args.out_dir, f"portfolio_bt{suffix}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "equity", "pnl", "margin",
                    "active_units", "positions"])
        for row in base["daily"]:
            w.writerow([row["date"], round(row["equity"], 2),
                        round(row["pnl"], 2), round(row["margin"], 2),
                        row["active_units"], row["positions"]])
    summary = {
        "config": os.path.abspath(args.config),
        "config_sha256": _hash_file(args.config),
        "capital": args.capital,
        "start": args.start, "end": args.end,
        "eval_start": base["eval_start"], "eval_end": base["eval_end"],
        "n_days": base["n_days"],
        "slippage_ticks": args.slippage,
        "margin_usage": args.margin_usage,
        "entry_mode": args.entry_mode,
        "period": args.period,
        "codes": codes,
        "trigger_envelope": cfg.base.trigger_envelope,
        "allow_same_bar_reversal": cfg.base.allow_same_bar_reversal,
        "gap_stop": cfg.base.gap_stop,
        "arm_exclude_signal": cfg.base.arm_exclude_signal,
        "priority": args.priority,
        "max_units_total": (cfg.risk.max_units_total
                            if args.max_units is None else args.max_units),
        "max_units_per_group": (cfg.risk.max_units_per_group
                                if args.max_group is None
                                else args.max_group),
        "constrained": {
            "metrics": _metrics(base["daily"], args.capital),
            "trades": base["trades"], "rejected": base["rejected"],
            "fees": round(base["fees"], 2),
            "realized": round(base["realized"], 2),
            "margin_peak": round(base["margin_peak"], 2),
            "margin_breach_days": base["margin_breach_days"],
            "final_equity": round(base["daily"][-1]["equity"], 2)
            if base["daily"] else None,
        },
        "unconstrained": {
            "metrics": _metrics(unc["daily"], args.capital),
            "trades": unc["trades"], "rejected": unc["rejected"],
            "fees": round(unc["fees"], 2),
            "realized": round(unc["realized"], 2),
            "margin_peak": round(unc["margin_peak"], 2),
            "margin_breach_days": unc["margin_breach_days"],
            "final_equity": round(unc["daily"][-1]["equity"], 2)
            if unc["daily"] else None,
        },
    }
    json_path = os.path.join(args.out_dir, f"portfolio_bt{suffix}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    # P2-5: 实际评估区间与截断告警 (请求区间超出数据覆盖时静默截断过)
    print(f"[pbt] 实际评估区间: {base['eval_start']}~{base['eval_end']} "
          f"({base['n_days']} 交易日; 请求 {args.start}~{args.end})")
    if base["eval_start"] and \
            (base["eval_start"] > args.start or base["eval_end"] < args.end):
        print(f"[pbt] WARN: 请求区间被数据覆盖截断 "
              f"(实际 {base['eval_start']}~{base['eval_end']}), "
              f"成绩不是全窗口口径")
    if not base["daily"]:
        raise SystemExit("[pbt] 评估区间为空: 检查 --start/--end 与数据覆盖")
    print(f"[pbt] constrained  {summary['constrained']['metrics']}")
    print(f"[pbt] unconstrained {summary['unconstrained']['metrics']}")
    print(f"[pbt] constrained trades={base['trades']} "
          f"rejected={base['rejected']} margin_peak={base['margin_peak']:,.0f} "
          f"margin_breach_days={base['margin_breach_days']}")
    if base["margin_breach_days"]:
        print(f"[pbt] WARN: 保证金超限 {base['margin_breach_days']} 天 "
              f"(峰值 {base['margin_peak']:,.0f}); 当前 --capital 不足以"
              f"支撑该组合, 请降低 units/增加资金")
    print(f"[pbt] -> {csv_path} / {json_path}")


if __name__ == "__main__":
    main()
