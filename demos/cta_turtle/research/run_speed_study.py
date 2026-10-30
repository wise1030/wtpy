#!/usr/bin/env python3
"""多速度（多窗口）趋势信号研究 — 用本项目自身数据检验"速度分散化"

目的 (2026-09-16): Man AHL《The Need for Speed in Trend-Following
Strategies》主张"交易所有速度"; 另一篇 2025 arXiv 工作主张杠铃结构
(剔除冗余的中周期)。两者结论都依赖其样本。本脚本用**本项目 12 个国内
商品 + 日线** 直接测量:
  1) 各档速度的独立表现 (风险归一化的 Sharpe/回撤/换手);
  2) 速度间日收益相关系数矩阵 (分散化是否真的存在);
  3) 等权混合 / 杠铃混合 vs 单速度 (夏普、回撤);
  4) 速度档数 K 的饱和曲线 (边际收益递减点)。

口径:
- 每档速度 = 一个海龟引擎实例 (entry/exit 通道为该档窗口, ATR(20) 共用,
  2N 止损 + 棘轮 N + 4×0.5N 加仓), 与线上策略同一套纯逻辑代码;
- 仓位按"每单位 1N 风险 = 100,000"反算 (可分数手, 仅用于研究)；
  日 PnL = 决策前方向×层数×(估值价-prev_close)，估值价在出场 bar
  取实际出场决策价、其余取 close，避免计入止损后的无持仓时段；
- 用 close 模式评估 (日线边沿触发)；本工具是**风险归一化窗口研究**，
  不包含生产 net router/整数手/共享风控/成交窗口，不能用来证明
  S1+S2 ↔ speeds 或 trigger 生产行为等价。
- 默认所有窗口均为 **S1 语义**（盈利跳过/sim/fail-safe）；55/20 因此
  不是生产 S2。可用 --system S2 做显式对照，输出 meta 必须随附语义。
- 跳空止损按 open 成交（与当前 close 的 S4-3 诚实口径一致）。

用法: python3 run_speed_study.py [--csv-dir ../storage/csv] [--codes ...]
输出: 终端表格 + speed_study.json
"""
import argparse
import csv
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)   # 项目根 (本文件在 research/ 下)
sys.path.insert(0, ROOT)

from core.indicators import wilder_atr, donchian      # noqa: E402
from core.TurtleEngine import TurtleEngine            # noqa: E402
from research.run_portfolio_bt import _hash_file, _code_version  # noqa: E402

SPEEDS = [(10, 5), (20, 10), (40, 20), (55, 20), (80, 40),
          (120, 60), (250, 125)]
UNIT_VAR = 100_000.0
DAYS = 240


def load_d1(path: str):
    d, o, h, l, c = [], [], [], [], []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                d.append(int(str(r["date"]).replace("/", "")
                             .replace("-", "")[:8]))
                o.append(float(r["open"]))
                h.append(float(r["high"]))
                l.append(float(r["low"]))
                c.append(float(r["close"]))
            except (KeyError, ValueError, TypeError):
                continue
    return (np.array(d), np.array(o), np.array(h), np.array(l), np.array(c))


def sim_speed(dates, opens, highs, lows, closes, atr, entry, exit_,
              fs=55, system="S1") -> dict:
    """单速度海龟引擎模拟; 返回日收益(风险归一化)/换手/交易数。

    system 必须显式理解：55/20 + S1 仍带盈利跳过/fail-safe，不是 S2。
    """
    n_bars = len(closes)
    pnl = np.zeros(n_bars)
    e_up, e_lo = donchian(highs, lows, entry)
    x_up, x_lo = donchian(highs, lows, exit_)
    f_up, f_lo = donchian(highs, lows, fs)
    e = TurtleEngine(system, stop_atr_mult=2.0, pyramid_layers=4,
                     pyramid_step_atr=0.5, stop_n_mode="ratchet",
                     entry_mode="close")
    trades = 0
    for i in range(1, n_bars):
        n = float(atr[i])
        if not np.isfinite(n) or n <= 0 or not np.isfinite(e_up[i]) \
                or not np.isfinite(x_up[i]) or not np.isfinite(f_up[i]):
            continue
        ch = {"entry_upper": float(e_up[i]), "entry_lower": float(e_lo[i]),
              "exit_upper": float(x_up[i]), "exit_lower": float(x_lo[i]),
              "fs_upper": float(f_up[i]), "fs_lower": float(f_lo[i]),
              "prev_entry_upper": float(e_up[i - 1]),
              "prev_entry_lower": float(e_lo[i - 1]),
              "prev_exit_upper": float(x_up[i - 1]),
              "prev_exit_lower": float(x_lo[i - 1]),
              "prev_fs_upper": float(f_up[i - 1]),
              "prev_fs_lower": float(f_lo[i - 1])}
        decs = e.on_bar(float(highs[i]), float(lows[i]), float(closes[i]),
                        float(closes[i - 1]), n, ch, open=float(opens[i]))
        dir_before = e.direction      # 本根收盘前的持仓方向
        # 决策前的手数 (含金字塔层): 当日 PnL 必须按实际持仓规模计算,
        # 旧实现固定 1 个单位, 4 层金字塔的收益被低估 4 倍 (2026-10-20)
        lots_before = sum(l for l, _ in e.layers) if dir_before != 0 else 0.0
        exit_price = None
        for dec in decs:
            k, px = dec["decision"], float(dec["price"])
            dn = e.direction or dec.get("direction", 0)
            lots = UNIT_VAR / (n * 1.0) if n > 0 else 0.0
            if k == "enter":
                e.confirm_enter(dn, lots, px, n)
                trades += 1
            elif k == "add":
                e.confirm_add(lots, px, n)
                trades += 1
            elif k == "exit":
                exit_price = px
                e.confirm_exit(px)
                trades += 1
        # 当日收益用**决策前**的方向与层数：新入场/加仓不享有本根
        # 此前涨跌；旧持仓在出场 bar 只计到实际出场决策价（止损可为
        # open/stop，通道出场通常为 close），其余 bar 才计到 close。
        # 旧口径一律 close-prev_close，会把止损后的无持仓时段也算入。
        if dir_before != 0 and lots_before > 0 and n > 0:
            mark = exit_price if exit_price is not None else closes[i]
            pnl[i] = dir_before * lots_before * (mark - closes[i - 1])
    from scipy.stats import skew
    return {"pnl": pnl, "trades": trades, "bars": n_bars,
            "years": n_bars / DAYS,
            "skew": float(skew(pnl[pnl != 0])) if (pnl != 0).sum() > 10
            else 0.0}


def metrics(pnl: np.ndarray, unit: float) -> dict:
    eq = np.cumsum(pnl)
    ret = pnl / unit
    sd = ret.std()
    sharpe = ret.mean() / sd * np.sqrt(DAYS) if sd > 0 else 0.0
    peak = np.maximum.accumulate(eq)
    dd = float(((peak - eq) / unit).max()) if len(eq) else 0.0
    from scipy.stats import skew
    return {"sharpe": round(float(sharpe), 3),
            "max_dd_R": round(dd, 1),
            "total_R": round(float(eq[-1] / unit), 1) if len(eq) else 0.0,
            "skew": round(float(skew(ret)), 2),
            "worst_day_R": round(float((pnl / unit).min()), 2)}


def blend_dated_series(per_instr: dict, dates_by_code: dict, sel: list):
    """按日期合并速度/品种 PnL；当日仍有数据的品种才参与等权。

    每个 code 先对所选速度求均值，再按交易日并集合成。历史起止之外
    不计权重（尤其禁止末日冻结），区间内缺该日 bar 记 0 PnL。
    """
    series = {}
    all_days = set()
    for code, by_speed in per_instr.items():
        dates = np.asarray(dates_by_code[code], dtype=np.int64)
        if len(dates) == 0:
            continue
        values = np.mean([by_speed[nm] for nm in sel], axis=0)
        idx = {int(d): i for i, d in enumerate(dates)}
        series[code] = (idx, values, int(dates[0]), int(dates[-1]))
        all_days.update(idx)
    out = []
    for day in sorted(all_days):
        active = []
        for idx, values, first, last in series.values():
            if first <= day <= last:
                active.append(values[idx[day]] if day in idx else 0.0)
        out.append(float(np.mean(active)) if active else 0.0)
    return np.asarray(out, dtype=float)


def mean_corr(series: dict) -> tuple:
    """多品种日收益 → 速度两两相关 (Fisher-z 平均)"""
    keys = list(series)
    acc = {}
    for a in range(len(keys)):
        for b in range(a + 1, len(keys)):
            zs = []
            for code, m in series[keys[a]].items():
                x, y = m, series[keys[b]].get(code)
                if y is None:
                    continue
                r = np.corrcoef(x, y)[0, 1]
                if np.isfinite(r) and abs(r) < 0.999999:
                    zs.append(np.arctanh(r))
            if zs:
                acc[(keys[a], keys[b])] = float(np.tanh(np.mean(zs)))
    return keys, acc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv-dir", default=os.path.abspath(
        os.path.join(ROOT, "..", "storage", "csv")))
    ap.add_argument("--config", default=os.path.join(
        ROOT, "config", "turtle_config.yaml"))
    ap.add_argument("--out", default=os.path.join(ROOT,
                                                  "speed_study.json"))
    ap.add_argument("--system", choices=["S1", "S2"], default="S1",
                    help="全部速度档的引擎语义；默认 S1。55/20 只有 "
                         "--system S2 才是生产 S2 语义")
    args = ap.parse_args()

    from core.ConfigLoader import ConfigLoader
    cfg = ConfigLoader.load(args.config)
    codes = [i.code for i in cfg.instruments]
    names = ["%d/%d" % s for s in SPEEDS]
    per_speed_pnl = {nm: {} for nm in names}
    per_instr = {}
    per_instr_dates = {}
    data_hashes = {}
    turnover = {nm: [] for nm in names}
    trades_all = {nm: {} for nm in names}   # nm -> code -> 笔数 (免二次重跑)
    for code in codes:
        path = os.path.join(args.csv_dir, f"{code}_d.csv")
        if not os.path.exists(path):
            print(f"[speed] 缺数据: {path}")
            continue
        dates, o, h, l, c = load_d1(path)
        if len(c) < 300:
            continue
        data_hashes[code] = _hash_file(path)
        atr = wilder_atr(h, l, c, 20)
        per_instr_dates[code] = dates
        per_instr[code] = {}
        for spec, nm in zip(SPEEDS, names):
            r = sim_speed(dates, o, h, l, c, atr, spec[0], spec[1],
                          system=args.system)
            per_speed_pnl[nm][code] = r["pnl"]
            per_instr[code][nm] = r["pnl"]
            turnover[nm].append(r["trades"] / max(r["years"], 1e-9))
            trades_all[nm][code] = r["trades"]
            print(f"[speed] {code:16s} {nm:>8s} "
                  f"{metrics(r['pnl'], UNIT_VAR)} "
                  f"trades={r['trades']} ({r['trades']/max(r['years'],1e-9):.1f}/年)")
    keys, corr = mean_corr(per_speed_pnl)
    # 组合层: 品种等权 + 速度等权/杠铃
    def blend_series(sel):
        return blend_dated_series(per_instr, per_instr_dates, sel)

    blends = {
        "single_20/10": ["20/10"],
        "single_55/20": ["55/20"],
        "single_120/60": ["120/60"],
        "single_250/125": ["250/125"],
        "all_7_equal": names,
        "barbell_10+55+250": ["10/5", "55/20", "250/125"],
        "barbell_20+250": ["20/10", "250/125"],
        "fast_10+20+40": ["10/5", "20/10", "40/20"],
        "slow_55+120+250": ["55/20", "120/60", "250/125"],
    }
    blend_m = {}
    print("\n=== 速度对相关性 (12 品种 Fisher-z 平均) ===")
    print("      " + "".join(f"{k:>9s}" for k in keys))
    for a in keys:
        row = []
        for b in keys:
            if a == b:
                row.append("    1.00")
            else:
                v = corr.get((a, b), corr.get((b, a)))
                row.append(f"{v:9.2f}")
        print(f"{a:>6s}" + "".join(row))
    print("\n=== 组合层 (品种等权) ===")
    for label, sel in blends.items():
        m = metrics(blend_series(sel), UNIT_VAR)
        for nm in sel:
            m.setdefault("turnover", []).append(np.mean(turnover[nm]))
        m["turnover_per_year"] = round(float(np.mean([np.mean(turnover[nm])
                                                      for nm in sel])), 1)
        m.pop("turnover", None)
        blend_m[label] = m
        print(f"  {label:22s} {m}")

    print("\n=== 每档速度: 组合层风险归一化指标 + 成本盈亏平衡 ===")
    print("%-9s %8s %8s %8s %8s %9s %10s" % ("速度", "Sharpe", "最大回撤R",
                                             "偏度", "最差单日R", "换手/年",
                                             "每边盈亏平衡N"))
    for nm in names:
        sel = [nm]
        m = metrics(blend_series(sel), UNIT_VAR)
        tr = float(np.mean(turnover[nm]))
        # 每笔平均盈亏 (R) = 组合总R / 组合总笔数
        # (2026-10-20 修复: 首遍缓存笔数复用; 旧实现含死代码且全量重跑
        #  一遍模拟, 结果与首遍逐位一致但耗时翻倍)
        n_trd = sum(trades_all[nm].values())
        tot_r = sum(float(per_speed_pnl[nm][c].sum()) / UNIT_VAR
                    for c in per_instr if c in per_speed_pnl[nm])
        per_trade = tot_r / n_trd if n_trd else 0.0
        print("%-9s %8.3f %8.1f %8.2f %8.2f %9.1f %10.4f" % (
            nm, m["sharpe"], m["max_dd_R"], m["skew"], m["worst_day_R"],
            tr, per_trade / 2.0))


    # 饱和曲线: 单速度→逐步加入
    print("\n=== 速度档数 K 的饱和曲线 (单速度→全部, 按顺序加入) ===")
    sat = {}
    for k in range(1, len(names) + 1):
        sel = names[:k]
        m = metrics(blend_series(sel), UNIT_VAR)
        sat[k] = {"set": sel, **m}
        print(f"  K={k}: {sel} {m}")
    import time as _time
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({
            "meta": {
                "generated": _time.strftime("%Y-%m-%d"),
                "manifest": {
                    "code_version": _code_version(),
                    "config_sha256": _hash_file(args.config),
                    "hots_sha256": _hash_file(os.path.join(
                        ROOT, "..", "common", "hots.json")),
                    "data": data_hashes,
                },
                "entry_system": args.system,
                "entry_mode": "close",
                "stop_n_mode": "ratchet",
                "gap_stop": True,
                "atr_period": 20,
                "stop_atr_mult": 2.0,
                "pyramid_layers": 4,
                "pyramid_step_atr": 0.5,
                "speed_windows": SPEEDS,
                "cost_model": "未计手续费/滑点; 风险归一化研究, 非生产净额执行",
                "caliber": "日PnL=决策前方向×层数×(估值价-prev_close); "
                           "出场bar估值价=实际出场决策价(open/stop/close), "
                           "其余=close; 风险归一化 unit=100000/N; "
                           "close 跳空止损按 open (S4-3)",
                "scope_note": "所有速度使用同一 entry_system; 55/20 在 "
                              "system=S1 时不是生产 S2。blend 仅为日PnL平均,"
                              "不含 net router/整数手/共享风控/成交窗口",
                "stale_note": "2026-09-16 版 speed_study.json 为固定 1 单位"
                              "口径, total_R/夏普被低估约金字塔倍数, 勿比",
            },
            "speeds": names, "corr": {f"{a}|{b}": v
                                      for (a, b), v in corr.items()},
            "blends": blend_m, "saturation": sat,
            "turnover": {nm: round(float(np.mean(v)), 1)
                         for nm, v in turnover.items()}},
            f, ensure_ascii=False, indent=2)
    print(f"\n[speed] 结果 → {args.out}")


if __name__ == "__main__":
    main()
