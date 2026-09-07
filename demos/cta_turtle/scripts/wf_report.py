#!/usr/bin/env python3
"""Walk-forward 结果分析面板 (P2)

读取 wf_results/wf_summary.csv (各窗最优组合 IS/OOS 对照) +
wf_results/wf_grid.csv (全网格 IS 成绩), 生成单文件 HTML 报告:
- 汇总卡片: 窗数 / OOS 年化均值 / OOS 回撤最差 / OOS 胜率 / IS→OOS 衰减比
- 参数稳定性: 各窗最优参数频次 (参数漂移越离散, 策略越脆弱)
- 参数热力图: 全网格 IS score 按前两个参数维度聚合均值, 色阶着色
- 窗明细表: 逐窗 is_* vs oos_* 对照

纯 stdlib, 无第三方依赖; 纯函数可单测。

用法: python3 scripts/wf_report.py [--dir ./wf_results] [--out report.html]
"""
import argparse
import csv
import html
import json
import os
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


# ------------------------------------------------------------------
# 纯函数 (可单测)
# ------------------------------------------------------------------
def load_rows(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def summary_stats(rows: list) -> dict:
    """汇总统计: OOS 均值/最差回撤/胜率 + IS→OOS 衰减比 (中位口径)"""
    oos_ann = [x for x in (_f(r.get("oos_annual_return")) for r in rows)
               if x is not None]
    oos_dd = [x for x in (_f(r.get("oos_max_falldown")) for r in rows)
              if x is not None]
    oos_win = [x for x in (_f(r.get("oos_win_rate")) for r in rows)
               if x is not None]
    degs = []
    for r in rows:
        is_a, oos_a = _f(r.get("is_annual_return")), \
            _f(r.get("oos_annual_return"))
        if is_a and is_a > 0 and oos_a is not None:
            degs.append(oos_a / is_a)
    degs.sort()
    med_deg = degs[len(degs) // 2] if degs else None
    return {
        "windows": len(rows),
        "oos_ann_mean": sum(oos_ann) / len(oos_ann) if oos_ann else None,
        "oos_dd_worst": max(oos_dd) if oos_dd else None,
        "oos_win_mean": sum(oos_win) / len(oos_win) if oos_win else None,
        "deg_median": med_deg,
    }


def param_frequency(rows: list) -> list:
    """各窗最优参数频次: [(params_json, count)], 按频次降序"""
    cnt = Counter(r.get("params", "{}") for r in rows)
    return cnt.most_common()


def heatmap_matrix(grid_rows: list, px: str = None, py: str = None,
                   metric: str = "is_score") -> dict:
    """全网格 IS 成绩 → 两参数维均值矩阵

    未指定维度时取互不相同的参数键中不同取值最多的前两个。
    返回 {"px","py","xs","ys","cells":{(x,y):mean},"counts":{..}}
    单参数网格退化为 1×N 行向量。
    """
    parsed = []
    keysets = Counter()
    for r in grid_rows:
        try:
            p = json.loads(r.get("params", "{}"))
        except json.JSONDecodeError:
            continue
        v = _f(r.get(metric))
        if v is None:
            continue
        parsed.append((p, v))
        keysets.update(p.keys())
    if not parsed or not keysets:
        return {"px": px, "py": py, "xs": [], "ys": [],
                "cells": {}, "counts": {}}

    if px is None or py is None:
        distinct = {k: len({p.get(k) for p, _ in parsed})
                    for k in keysets}
        order = sorted(distinct, key=lambda k: -distinct[k])
        px = px or (order[0] if order else None)
        py = py or (order[1] if len(order) > 1 else None)

    def _key(v):
        return json.dumps(v) if isinstance(v, (list, dict)) else v

    xs = sorted({_key(p.get(px)) for p, _ in parsed},
                key=lambda v: (isinstance(v, str), v))
    ys = sorted({_key(p.get(py)) for p, _ in parsed},
                key=lambda v: (isinstance(v, str), v)) if py else ["-"]
    acc = {}
    cnt = {}
    for p, v in parsed:
        k = (_key(p.get(px)), _key(p.get(py)) if py else "-")
        acc[k] = acc.get(k, 0.0) + v
        cnt[k] = cnt.get(k, 0) + 1
    cells = {k: acc[k] / cnt[k] for k in acc}
    return {"px": px, "py": py, "xs": xs, "ys": ys,
            "cells": cells, "counts": cnt}


def cell_color(v: float, vmin: float, vmax: float) -> str:
    """值 → 色阶 (红→灰→绿); vmin==vmax 时中性灰"""
    if vmax <= vmin:
        t = 0.5
    else:
        t = max(0.0, min(1.0, (v - vmin) / (vmax - vmin)))
    # t=0 → #d84a4a (红), t=0.5 → #555, t=1 → #3fae5a (绿)
    if t < 0.5:
        u = t / 0.5                     # u=0 极端红, u=1 中点
        r = int(0xd8 + (0x55 - 0xd8) * u)
        g = int(0x4a + (0x55 - 0x4a) * u)
        b = int(0x4a + (0x55 - 0x4a) * u)
    else:
        u = (t - 0.5) / 0.5             # u=0 中点, u=1 极端绿
        r = int(0x55 + (0x3f - 0x55) * u)
        g = int(0x55 + (0xae - 0x55) * u)
        b = int(0x55 + (0x5a - 0x55) * u)
    return f"#{r:02x}{g:02x}{b:02x}"


# ------------------------------------------------------------------
# HTML 渲染
# ------------------------------------------------------------------
def _fmt(v, pct=False):
    if v is None:
        return "-"
    return f"{v:.2f}%" if pct else f"{v:.4f}"


def render_html(summary_rows: list, grid_rows: list,
                title: str = "Walk-Forward Report") -> str:
    st = summary_stats(summary_rows)
    freq = param_frequency(summary_rows)
    hm = heatmap_matrix(grid_rows)

    cards = "".join(
        f'<div class="card"><div class="k">{k}</div>'
        f'<div class="v">{v}</div></div>'
        for k, v in [
            ("滚动窗数", st["windows"]),
            ("OOS 年化均值", _fmt(st["oos_ann_mean"], pct=True)),
            ("OOS 回撤最差", _fmt(st["oos_dd_worst"], pct=True)),
            ("OOS 胜率均值", _fmt(st["oos_win_mean"], pct=True)),
            ("IS→OOS 衰减比(中位)", _fmt(st["deg_median"])),
        ])

    freq_rows = "".join(
        f"<tr><td><code>{html.escape(p)}</code></td><td>{c}</td></tr>"
        for p, c in freq)

    # 热力图
    hm_html = "<p>无全网格数据 (wf_grid.csv 缺失或为空)</p>"
    if hm["xs"]:
        vals = list(hm["cells"].values())
        vmin, vmax = min(vals), max(vals)
        head = (f"<tr><th>{html.escape(str(hm['py']) or '')}\\"
                f"{html.escape(str(hm['px']))}</th>" +
                "".join(f"<th>{html.escape(str(x))}</th>"
                        for x in hm["xs"]) + "</tr>")
        body = ""
        for y in hm["ys"]:
            tds = []
            for x in hm["xs"]:
                v = hm["cells"].get((x, y))
                if v is None:
                    tds.append('<td class="na">-</td>')
                else:
                    n = hm["counts"][(x, y)]
                    tds.append(
                        f'<td style="background:{cell_color(v, vmin, vmax)}"'
                        f' title="n={n}">{v:.3f}</td>')
            body += (f"<tr><th>{html.escape(str(y))}</th>"
                     + "".join(tds) + "</tr>")
        hm_html = (f'<table class="hm">{head}{body}</table>'
                   f'<p class="note">格值=该参数组合跨全部滚动窗的 IS '
                   f'score 均值; 悬停见样本数 n</p>')

    # 窗明细
    keys = [k for k in (summary_rows[0].keys() if summary_rows else [])]
    det_head = "".join(f"<th>{html.escape(k)}</th>" for k in keys)
    det_body = ""
    for r in summary_rows:
        det_body += "<tr>" + "".join(
            f"<td>{html.escape(str(r.get(k, '')))}</td>" for k in keys) \
            + "</tr>"

    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
body {{ font-family: -apple-system, "Segoe UI", sans-serif;
       background:#1e1e24; color:#ddd; margin:24px; }}
h1 {{ font-size:20px; }} h2 {{ font-size:15px; color:#9ab; margin-top:28px; }}
.cards {{ display:flex; gap:12px; flex-wrap:wrap; }}
.card {{ background:#2a2a33; border-radius:8px; padding:12px 18px;
        min-width:130px; }}
.card .k {{ font-size:12px; color:#889; }}
.card .v {{ font-size:20px; font-weight:600; margin-top:4px; }}
table {{ border-collapse:collapse; font-size:12.5px; }}
th, td {{ border:1px solid #3a3a44; padding:4px 10px; text-align:right; }}
th {{ background:#2a2a33; }}
td.na {{ color:#556; }}
.hm td {{ color:#eee; text-shadow:0 1px 2px #0008; }}
.note {{ font-size:11.5px; color:#778; }}
code {{ font-size:12px; }}
</style></head><body>
<h1>{html.escape(title)}</h1>
<h2>汇总</h2><div class="cards">{cards}</div>
<h2>参数稳定性 (各窗最优组合频次)</h2>
<table><tr><th>参数组合</th><th>中选窗数</th></tr>{freq_rows}</table>
<h2>参数热力图 (全网格 IS score 均值)</h2>{hm_html}
<h2>逐窗明细 (IS vs OOS)</h2>
<table><tr>{det_head}</tr>{det_body}</table>
</body></html>"""


# ------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Walk-forward 分析面板")
    ap.add_argument("--dir", type=str,
                    default=os.path.join(ROOT, "wf_results"))
    ap.add_argument("--out", type=str, default=None,
                    help="输出 HTML 路径 (默认 <dir>/wf_report.html)")
    args = ap.parse_args()

    summary = load_rows(os.path.join(args.dir, "wf_summary.csv"))
    grid = load_rows(os.path.join(args.dir, "wf_grid.csv"))
    if not summary:
        raise SystemExit(f"[wf_report] 无数据: {args.dir}/wf_summary.csv")
    out = args.out or os.path.join(args.dir, "wf_report.html")
    doc = render_html(summary, grid)
    with open(out, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"[wf_report] {len(summary)} 窗 / {len(grid)} 网格行 → {out}")


if __name__ == "__main__":
    main()
