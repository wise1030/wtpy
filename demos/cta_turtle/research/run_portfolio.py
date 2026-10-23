#!/usr/bin/env python3
"""组合约束回放 (2026-10-20)

目的: run_matrix 是"12 个独立品种各自满额"的回测, 组合层的
`max_units_total` / `max_units_per_group` / 总风险预算从未参与决策,
所以组合曲线是无约束上界。本脚本用已生成的
`outputs_bt/<stra>/{trades,funds}.csv` 做一次**约束回放**:

  1. 从 trades.csv 重建每个品种的持仓 episode (flat→持仓→flat);
  2. 按日期回放, 新 episode 只有在"全组合单位数 < max_units_total
     且同组单位数 < max_units_per_group"时才放行;
  3. 未放行的 episode 不计 PnL (也不占单位), 得到约束后曲线;
  4. 同时给出无约束曲线与逐日超限统计。

口径与局限:
- 这是"基于已生成交易流"的近似, 不是重新跑信号: 被拒绝的 episode
  不会改变后续信号/加仓/止损路径; 真实共享账本回测仍需下一阶段实现。
- 每个品种仍按 `--capital` 独立计 PnL; 组合总资金 =
  n_instruments × capital。报告同时给"每品种归一化"收益 (= 总收益)。
- 同一天多品种同时入场按配置顺序先到先得; 这是确定性口径, 不是
  实盘的风控缩量顺序。

用法:
  python run_portfolio.py --config config/turtle_config.yaml \
      --bt-dir outputs_bt --capital 5000000
"""
import argparse
import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)   # 项目根 (本文件在 research/ 下)
sys.path.insert(0, ROOT)

from core.ConfigLoader import ConfigLoader          # noqa: E402
from core import portfolio as pf                    # noqa: E402


def stra_name(code: str) -> str:
    return f"turtle_{code.replace('.', '_')}"


def read_funds(path: str) -> dict:
    """funds.csv → {date: dynbalance}; 失败返回 {}"""
    out = {}
    if not os.path.exists(path):
        return out
    try:
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    out[int(r["date"])] = float(r["dynbalance"])
                except (KeyError, TypeError, ValueError):
                    continue
    except OSError:
        return {}
    return out


def read_trades(path: str):
    """trades.csv → [(date, signed_qty)]; signed_qty 按方向开平折算"""
    rows = []
    if not os.path.exists(path):
        return rows
    try:
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    qty = float(r["qty"])
                    sign = 1.0 if str(r["direct"]).upper().startswith("L") \
                        else -1.0
                    act = str(r["action"]).upper()
                    date = int(str(r["time"])[:8])
                except (KeyError, TypeError, ValueError):
                    continue
                if act.startswith("O"):
                    rows.append((date, sign * qty))
                elif act.startswith("C"):
                    rows.append((date, -sign * qty))
    except OSError:
        return rows
    rows.sort(key=lambda x: x[0])
    return rows


def build_episodes(code: str, group: str, trades, last_date: int) -> list:
    """净持仓 episode: flat→持仓→flat; 反手在当日结束旧 episode 并开新。"""
    out = []
    pos = 0.0
    start = None
    dirn = 0
    for date, dq in trades:
        new = pos + dq
        if pos == 0 and new != 0:
            start, dirn = date, (1 if new > 0 else -1)
        elif pos != 0 and new != 0 and (new > 0) != (pos > 0):
            out.append({"code": code, "group": group, "start": start,
                        "end": date, "dir": dirn})
            start, dirn = date, (1 if new > 0 else -1)
        elif pos != 0 and new == 0:
            out.append({"code": code, "group": group, "start": start,
                        "end": date, "dir": dirn})
            start, dirn = None, 0
        pos = new
    if pos != 0 and start is not None:
        out.append({"code": code, "group": group, "start": start,
                    "end": last_date, "dir": dirn})
    return [e for e in out if e["start"] is not None
            and e["end"] >= e["start"]]


def _pnl_between(pnl: dict, start: int, end: int) -> float:
    """episode PnL: 开仓日收盘不享有当日涨跌, 平仓日享有当日涨跌。"""
    return sum(v for d, v in pnl.items() if start < d <= end)


def replay(episodes: dict, pnl: dict, dates: list,
           max_units: int = 0, max_group: int = 0):
    """按日期回放组合约束。

    Returns:
        (daily_pnl: list[float], equity: list[float],
         allowed: list[dict], violations: dict)
    """
    starts = {}
    for _code, eps in episodes.items():
        for ep in eps:
            starts.setdefault(ep["start"], []).append(ep)
    for d in starts:
        starts[d].sort(key=lambda e: e["code"])   # 确定性先到先得

    active = {}                 # code -> episode
    allowed = []
    daily = []
    over_units = 0
    over_group = 0
    max_active = 0
    max_group_seen = 0
    for d in dates:
        # 1) 先计当日 PnL (含当日结束的 episode)
        day_pnl = 0.0
        for code, ep in list(active.items()):
            if ep["start"] < d <= ep["end"]:
                day_pnl += _pnl_between(pnl.get(code, {}), d - 1, d)
        daily.append(day_pnl)
        # 2) 释放当日结束的 episode (end == d)
        for code in list(active):
            if active[code]["end"] <= d:
                del active[code]
        # 3) 当日新开 episode: 检查全组合/组上限
        for ep in starts.get(d, []):
            code = ep["code"]
            # 同 code 反手: 旧 episode 已在上面释放
            if max_units > 0 and len(active) >= max_units:
                ep["allowed"] = False
                over_units += 1
                continue
            g = ep["group"] or ""
            if max_group > 0 and g and \
                    sum(1 for e in active.values() if e["group"] == g) >= \
                    max_group:
                ep["allowed"] = False
                over_group += 1
                continue
            ep["allowed"] = True
            allowed.append(ep)
            active[code] = ep
        max_active = max(max_active, len(active))
        if max_group > 0:
            for g in {e["group"] for e in active.values() if e["group"]}:
                max_group_seen = max(
                    max_group_seen,
                    sum(1 for e in active.values() if e["group"] == g))

    equity = []
    cum = 0.0
    for v in daily:
        cum += v
        equity.append(cum)
    return daily, equity, allowed, {
        "max_active": max_active,
        "max_group_seen": max_group_seen,
        "rejected_by_total_units": over_units,
        "rejected_by_group_units": over_group,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=str,
                    default=os.path.join(ROOT, "config",
                                         "turtle_config.yaml"))
    ap.add_argument("--bt-dir", type=str,
                    default=os.path.join(ROOT, "outputs_bt"))
    ap.add_argument("--out-dir", type=str,
                    default=os.path.join(ROOT, "matrix_results"))
    ap.add_argument("--capital", type=float, default=5000000,
                    help="每品种初始资金 (组合总资金 = n×capital)")
    ap.add_argument("--max-units", type=int, default=None,
                    help="覆盖 risk.max_units_total")
    ap.add_argument("--max-group", type=int, default=None,
                    help="覆盖 risk.max_units_per_group")
    args = ap.parse_args()

    cfg = ConfigLoader.load(args.config)
    codes = [i.code for i in cfg.instruments]
    groups = {i.code: (i.group or "") for i in cfg.instruments}
    max_units = (cfg.risk.max_units_total if args.max_units is None
                 else args.max_units)
    max_group = (cfg.risk.max_units_per_group if args.max_group is None
                 else args.max_group)

    episodes, pnl, dates = {}, {}, set()
    for code in codes:
        d = os.path.join(args.bt_dir, stra_name(code))
        f = read_funds(os.path.join(d, "funds.csv"))
        t = read_trades(os.path.join(d, "trades.csv"))
        if not f or not t:
            print(f"[portfolio] WARN {code}: 缺 funds/trades, 跳过")
            continue
        # daily pnl = dynbalance 差分
        ds = sorted(f)
        pnl[code] = {ds[i]: f[ds[i]] - f[ds[i - 1]]
                     for i in range(1, len(ds))}
        dates.update(ds)
        episodes[code] = build_episodes(code, groups.get(code, ""), t,
                                        ds[-1])
    if not episodes:
        raise SystemExit("无可用回测产出")

    dates = sorted(dates)
    daily, equity, allowed, stats = replay(
        episodes, pnl, dates, max_units, max_group)

    total_capital = float(args.capital) * len(episodes)
    metrics = pf.portfolio_metrics(dates, _as_array(equity), total_capital,
                                   rf=0.0)
    # 无约束: 所有 episode 都放行, 与约束曲线使用同一 episode 归因
    # (注意: 不是 funds 总和 — episode 不含开仓日手续费/空仓日费用)
    unconstrained = sum(
        _pnl_between(pnl[c], e["start"], e["end"])
        for c in episodes for e in episodes[c])
    constrained = equity[-1] if equity else 0.0

    os.makedirs(args.out_dir, exist_ok=True)
    csv_path = os.path.join(args.out_dir, "portfolio_overlay.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "pnl", "equity"])
        for i, d in enumerate(dates):
            w.writerow([d, round(daily[i], 2), round(equity[i], 2)])
    summary = {
        "capital_per_instrument": args.capital,
        "total_capital": total_capital,
        "instruments": len(episodes),
        "max_units_total": max_units,
        "max_units_per_group": max_group,
        "episodes": sum(len(v) for v in episodes.values()),
        "allowed_episodes": len(allowed),
        "constrained_pnl": round(constrained, 2),
        "unconstrained_pnl": round(unconstrained, 2),
        "constrained_return": round(constrained / total_capital, 6)
        if total_capital > 0 else None,
        "unconstrained_return": round(unconstrained / total_capital, 6)
        if total_capital > 0 else None,
        "metrics": metrics,
        "stats": stats,
        "attribution": "episode (entry-day fees excluded; not funds total)",
        "selection_bias": (
            "事后按已生成交易流回放: 被拒 episode 的盈亏不再出现, "
            "结果对样本内赢家/输家选择敏感; 仅用于量化约束暴露与"
            "单位上限影响, 不能作为策略提升证据。"),
    }
    json_path = os.path.join(args.out_dir, "portfolio_overlay.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"[portfolio] constrained={constrained:,.0f} "
          f"unconstrained={unconstrained:,.0f} "
          f"total_capital={total_capital:,.0f}")
    print("[portfolio] NOTE: 约束回放是事后筛选交易流 (非重跑信号), "
          "被拒 episode 的盈亏不再出现; 结果对样本内赢家/输家敏感, "
          "只用于量化约束暴露, 不能当作策略提升。")
    print(f"[portfolio] stats={stats}")
    print(f"[portfolio] metrics={metrics}")
    print(f"[portfolio] -> {csv_path} / {json_path}")


def _as_array(x):
    import numpy as np
    return np.asarray(x, dtype=float)


if __name__ == "__main__":
    main()
