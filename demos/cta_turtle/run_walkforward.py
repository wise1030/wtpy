"""Walk-forward 滚动窗参数寻优 (P2)

对单品种在时间轴上切分 训练窗→验证窗 滚动序列:
训练窗内网格寻优 (subprocess 调 runBT.py, 按目标函数选最优组合),
随后在紧随的验证窗上做样本外(OOS)回测, 汇总各窗 OOS 表现。

用法:
  python run_walkforward.py --code SHFE.rb.HOT \
      --grid '{"stop_atr_mult":[1.5,2.0,2.5],"pyramid_layers":[3,4]}' \
      --start 201901010900 --end 202401010900 \
      --train-months 24 --test-months 6 --objective sharpe

产出: wf_results/wf_summary.csv + 终端 OOS 汇总。
注意: 网格×窗数 = 回测次数, 注意耗时; 纯逻辑函数可独立单测。
"""
import argparse
import csv
import itertools
import json
import os
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------
# 纯函数 (可单测)
# ---------------------------------------------------------------------
def parse_grid(spec: dict) -> list:
    """{字段: [候选...]} → 组合列表 [{字段: 值}] (笛卡尔积)"""
    if not spec:
        return [{}]
    keys = list(spec.keys())
    bad = [k for k in keys if not isinstance(spec[k], list) or not spec[k]]
    if bad:
        raise ValueError(f"网格值需为非空列表: {bad}")
    out = []
    for values in itertools.product(*(spec[k] for k in keys)):
        out.append(dict(zip(keys, values)))
    return out


def iter_windows(start: int, end: int, train_months: int,
                 test_months: int) -> list:
    """滚动窗切分。输入输出均为 yyyymmddhhmm 整数, 窗口按自然月对齐
    到当月 01 日 0900; 验证窗起点超出 end 或被截断退化(≤单点)的窗丢弃。"""
    def mk(y, m):
        # yyyymm01 + 0900 → y*1e8 + m*1e6 + 10900
        return y * 100000000 + m * 1000000 + 10900

    start_ym = start // 1000000   # yyyymm
    end_ym = end // 1000000
    if start_ym > end_ym:
        return []
    wins = []
    cur_y, cur_m = start_ym // 100, start_ym % 100
    while True:
        te_y, te_m = _add_months(cur_y, cur_m, train_months)
        te_end_y, te_end_m = _add_months(te_y, te_m, test_months)
        win_start = mk(cur_y, cur_m)
        train_end = mk(*_prev_month(te_y, te_m))   # 训练止于验证前一月
        test_start = mk(te_y, te_m)
        if test_start > end:
            break                                  # 验证窗起点越界
        test_end_raw = mk(*_prev_month(te_end_y, te_end_m))
        test_end = min(test_end_raw, end)
        if test_end > test_start:      # 退化为单点(被 end 截断)的窗丢弃
            wins.append({"train": (win_start, train_end),
                         "test": (test_start, test_end)})
        cur_y, cur_m = _add_months(cur_y, cur_m, test_months)
        if cur_y * 100 + cur_m > end_ym:
            break
    return wins


def _add_months(y, m, delta):
    t = (y * 12 + (m - 1)) + delta
    return t // 12, t % 12 + 1


def _prev_month(y, m):
    return _add_months(y, m, -1)


def objective(metrics: dict, name: str) -> float:
    """寻优目标函数; 数据缺失返回 -inf"""
    if not metrics:
        return float("-inf")
    ann = metrics.get("annual_return") or 0.0
    dd = metrics.get("max_falldown") or 0.0
    shp = metrics.get("sharpe_ratio") or 0.0
    if name == "sharpe":
        return float(shp)
    if name == "calmar":
        c = metrics.get("calmar_ratio")
        return float(c) if c is not None else \
            (ann / dd if dd > 0 else 0.0)
    if name == "return_over_dd":
        return ann / dd if dd > 0 else 0.0
    if name == "annual_return":
        return float(ann)
    raise ValueError(f"未知目标函数 '{name}'")


def merge_base_config(src_path: str, combo: dict, dst_path: str,
                      code: str = None):
    """源配置 + 参数组合 → 临时配置文件 (仅覆盖 base 段已知字段)"""
    with open(src_path, encoding='utf-8') as f:
        raw = yaml.safe_load(f) or {}
    base = dict(raw.get("base") or {})
    base.update(combo)
    raw["base"] = base
    if code:
        insts = [i for i in raw.get("instruments", [])]
        keep = [i for i in insts if i.get("code") == code]
        if keep:
            raw["instruments"] = keep
    with open(dst_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False)


# ---------------------------------------------------------------------
# 子进程执行
# ---------------------------------------------------------------------
def run_backtest(code: str, cfg_path: str, start: int, end: int,
                 capital: float, slippage: int) -> dict:
    """跑一次回测并读取 summary.json (复用 runBT 输出目录)"""
    stra_name = f"turtle_{code.replace('.', '_')}"
    cmd = [sys.executable, os.path.join(HERE, "runBT.py"),
           "--code", code, "--config", cfg_path,
           "--start", str(start), "--end", str(end),
           "--capital", str(capital), "--slippage", str(slippage)]
    r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True)
    summary = os.path.join(HERE, "outputs_bt", stra_name, "summary.json")
    if not os.path.exists(summary):
        raise RuntimeError(f"回测无产出: {code} "
                           f"{r.stdout[-300:]}\n{r.stderr[-300:]}")
    with open(summary, encoding='utf-8') as f:
        return json.load(f)


METRIC_KEYS = ("total_return", "annual_return", "max_falldown",
               "sharpe_ratio", "win_rate")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", type=str, required=True)
    ap.add_argument("--config", type=str,
                    default=os.path.join(HERE, "config",
                                         "turtle_config.yaml"))
    ap.add_argument("--grid", type=str, required=True,
                    help="JSON 字符串或 .json 文件路径: "
                         "{base字段: [候选列表]}")
    ap.add_argument("--start", type=int, default=201901010900)
    ap.add_argument("--end", type=int, default=202401010900)
    ap.add_argument("--train-months", type=int, default=24)
    ap.add_argument("--test-months", type=int, default=6)
    ap.add_argument("--objective", type=str, default="sharpe",
                    choices=["sharpe", "calmar", "return_over_dd",
                             "annual_return"])
    ap.add_argument("--capital", type=float, default=5000000)
    ap.add_argument("--slippage", type=int, default=0)
    args = ap.parse_args()

    grid_src = args.grid
    if os.path.exists(grid_src):
        with open(grid_src, encoding='utf-8') as f:
            spec = json.load(f)
    else:
        spec = json.loads(grid_src)
    combos = parse_grid(spec)
    windows = iter_windows(args.start, args.end,
                           args.train_months, args.test_months)
    if not windows:
        raise SystemExit("窗口切分为空: 检查 --start/--end/月数")
    total_bt = len(windows) * (len(combos) + 1)
    print(f"[wf] {len(windows)} 窗 x ({len(combos)} 组合+1 OOS) "
          f"≈ {total_bt} 次回测")

    out_dir = os.path.join(HERE, "wf_results")
    os.makedirs(out_dir, exist_ok=True)
    rows = []
    grid_rows = []          # 全网格 IS 成绩 (wf_report.py 热力图数据源)
    tmp_cfg = os.path.join(tempfile.mkdtemp(prefix="wfcfg_"), "combo.yaml")

    for i, w in enumerate(windows):
        tr_s, tr_e = w["train"]
        te_s, te_e = w["test"]
        print(f"[wf] 窗口{i + 1}/{len(windows)} "
              f"train={tr_s}~{tr_e} test={te_s}~{te_e}")
        best, best_score = None, float("-inf")
        for combo in combos:
            merge_base_config(args.config, combo, tmp_cfg, code=args.code)
            try:
                m = run_backtest(args.code, tmp_cfg, tr_s, tr_e,
                                 args.capital, args.slippage)
            except RuntimeError as e:
                print(f"[wf]   组合失败 {combo}: {e}")
                continue
            score = objective(m, args.objective)
            grow = {"window": i + 1, "train": f"{tr_s}~{tr_e}",
                    "params": json.dumps(combo, ensure_ascii=False),
                    "is_score": round(score, 4)}
            grow.update({f"is_{k}": m.get(k) for k in METRIC_KEYS})
            grid_rows.append(grow)
            if score > best_score:
                best_score, best = score, (combo, m)
        if best is None:
            print("[wf]   该窗全部组合失败, 跳过")
            continue
        combo, is_m = best
        print(f"[wf]   best={combo} score={best_score:.4f}")
        merge_base_config(args.config, combo, tmp_cfg, code=args.code)
        oos = run_backtest(args.code, tmp_cfg, te_s, te_e,
                           args.capital, args.slippage)
        row = {"window": i + 1,
               "train": f"{tr_s}~{tr_e}",
               "test": f"{te_s}~{te_e}",
               "params": json.dumps(combo, ensure_ascii=False),
               "is_score": round(best_score, 4)}
        row.update({f"is_{k}": is_m.get(k) for k in METRIC_KEYS})
        row.update({f"oos_{k}": oos.get(k) for k in METRIC_KEYS})
        rows.append(row)

    out_csv = os.path.join(out_dir, "wf_summary.csv")
    with open(out_csv, "w", newline='', encoding='utf-8') as f:
        wtr = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        wtr.writeheader()
        wtr.writerows(rows)
    print(f"[wf] report -> {out_csv}")

    if grid_rows:
        grid_csv = os.path.join(out_dir, "wf_grid.csv")
        with open(grid_csv, "w", newline='', encoding='utf-8') as f:
            wtr = csv.DictWriter(f, fieldnames=list(grid_rows[0].keys()))
            wtr.writeheader()
            wtr.writerows(grid_rows)
        print(f"[wf] grid dump -> {grid_csv} "
              f"(wf_report.py 热力图数据源)")

    oos_ann = [r["oos_annual_return"] for r in rows
               if r.get("oos_annual_return") is not None]
    oos_dd = [r["oos_max_falldown"] for r in rows
              if r.get("oos_max_falldown") is not None]
    if oos_ann:
        print(f"[wf] OOS 年化均值={sum(oos_ann) / len(oos_ann):.4f} "
              f"(n={len(oos_ann)})")
    if oos_dd:
        print(f"[wf] OOS 最大回撤最差={max(oos_dd):.2f}%")


if __name__ == "__main__":
    main()
