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
- 仓位按"每单位 1N 风险 = 100,000"反算 (可分数手, 仅用于研究),
  故日收益 = 方向 × Δclose / N × 100,000 → 无需合约乘数, 各速度可比;
- 用 close 模式评估 (日线边沿触发), 与线上 trigger 模式的**信号集合**一致,
  差别只在成交时点, 对速度结构研究无害。

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
sys.path.insert(0, HERE)

from helpers.indicators import wilder_atr, donchian      # noqa: E402
from helpers.TurtleEngine import TurtleEngine            # noqa: E402

SPEEDS = [(10, 5), (20, 10), (40, 20), (55, 20), (80, 40),
          (120, 60), (250, 125)]
UNIT_VAR = 100_000.0
DAYS = 240


def load_d1(path: str):
    d, h, l, c = [], [], [], []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                d.append(int(str(r["date"]).replace("/", "")
                             .replace("-", "")[:8]))
                h.append(float(r["high"]))
                l.append(float(r["low"]))
                c.append(float(r["close"]))
            except (KeyError, ValueError, TypeError):
                continue
    return (np.array(d), np.array(h), np.array(l), np.array(c))


def sim_speed(dates, highs, lows, closes, atr, entry, exit_,
              fs=55) -> dict:
    """单速度海龟引擎模拟; 返回日收益(风险归一化)/换手/交易数"""
    n_bars = len(closes)
    pnl = np.zeros(n_bars)
    e_up, e_lo = donchian(highs, lows, entry)
    x_up, x_lo = donchian(highs, lows, exit_)
    f_up, f_lo = donchian(highs, lows, fs)
    e = TurtleEngine("S1", stop_atr_mult=2.0, pyramid_layers=4,
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
              "prev_fs_upper": float(f_up[i - 1]),
              "prev_fs_lower": float(f_lo[i - 1])}
        decs = e.on_bar(float(highs[i]), float(lows[i]), float(closes[i]),
                        float(closes[i - 1]), n, ch)
        dir_before = e.direction      # 本根收盘前的持仓方向
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
                e.confirm_exit(px)
                trades += 1
        # 当日收益用**决策前**的方向 (本根收盘才建/平的仓不享有本根涨跌,
        # 否则会有前视偏差 — 初版即因此把快速度夏普抬到 6+)
        if dir_before != 0 and n > 0:
            pnl[i] = dir_before * (closes[i] - closes[i - 1]) / n * UNIT_VAR
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
        os.path.join(HERE, "..", "storage", "csv")))
    ap.add_argument("--config", default=os.path.join(
        HERE, "config", "turtle_config.yaml"))
    ap.add_argument("--out", default=os.path.join(HERE,
                                                  "speed_study.json"))
    args = ap.parse_args()

    from helpers.ConfigLoader import ConfigLoader
    cfg = ConfigLoader.load(args.config)
    codes = [i.code for i in cfg.instruments]
    names = ["%d/%d" % s for s in SPEEDS]
    per_speed_pnl = {nm: {} for nm in names}
    per_instr = {}
    turnover = {nm: [] for nm in names}
    for code in codes:
        path = os.path.join(args.csv_dir, f"{code}_d.csv")
        if not os.path.exists(path):
            print(f"[speed] 缺数据: {path}")
            continue
        dates, h, l, c = load_d1(path)
        if len(c) < 300:
            continue
        atr = wilder_atr(h, l, c, 20)
        per_instr[code] = {}
        for spec, nm in zip(SPEEDS, names):
            r = sim_speed(dates, h, l, c, atr, spec[0], spec[1])
            per_speed_pnl[nm][code] = r["pnl"]
            per_instr[code][nm] = r["pnl"]
            turnover[nm].append(r["trades"] / max(r["years"], 1e-9))
            print(f"[speed] {code:16s} {nm:>8s} "
                  f"{metrics(r['pnl'], UNIT_VAR)} "
                  f"trades={r['trades']} ({r['trades']/max(r['years'],1e-9):.1f}/年)")
    keys, corr = mean_corr(per_speed_pnl)
    # 组合层: 品种等权 + 速度等权/杠铃
    def blend_series(sel):
        out = []
        for code in per_instr:
            L = min(len(per_instr[code][nm]) for nm in sel)
            out.append(np.mean([per_instr[code][nm][-L:] for nm in sel],
                               axis=0))
        L = min(len(x) for x in out)
        return np.sum([x[-L:] for x in out], axis=0) / len(out)

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
        n_trd = 0
        for code in per_instr:
            r = sim_speed(*load_d1(os.path.join(args.csv_dir,
                                                f"{code}_d.csv")),
                          None, 0, 0) if False else None
        # 直接用已存 trades: 重算一次每速度总笔数与总R
        n_trd = 0
        tot_r = 0.0
        for code in per_instr:
            path = os.path.join(args.csv_dir, f"{code}_d.csv")
            d2, h2, l2, c2 = load_d1(path)
            atr2 = wilder_atr(h2, l2, c2, 20)
            spec = SPEEDS[names.index(nm)]
            rr = sim_speed(d2, h2, l2, c2, atr2, spec[0], spec[1])
            n_trd += rr["trades"]
            tot_r += rr["pnl"].sum() / UNIT_VAR
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
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"speeds": names, "corr": {f"{a}|{b}": v
                                             for (a, b), v in corr.items()},
                   "blends": blend_m, "saturation": sat,
                   "turnover": {nm: round(float(np.mean(v)), 1)
                                for nm, v in turnover.items()}},
                  f, ensure_ascii=False, indent=2)
    print(f"\n[speed] 结果 → {args.out}")


if __name__ == "__main__":
    main()
