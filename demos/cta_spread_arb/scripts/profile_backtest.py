"""P3-6: cProfile 包装回测主流程,导出 .prof + top N 文字报告。

用法:
    python scripts/profile_backtest.py --experiment profile_p3_6

输出:
    outputs_bt/profile_p3_6/
      profile.prof           — pstats 原始数据
      profile_top_cumtime.txt — top 50 by cumtime
      profile_top_tottime.txt — top 50 by tottime
      profile_top_ncalls.txt  — top 50 by ncalls
      profile_callers_top10.txt — top 10 cumtime 函数的 callers
"""
import argparse
import cProfile
import os
import pstats
import sys
from pathlib import Path

# 项目根路径注入
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _sub in ("strategies", "helpers", "factors", ""):
    p = _PROJECT_ROOT / _sub if _sub else _PROJECT_ROOT
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)


def run_backtest(experiment: str):
    """复刻 runBT.py 主流程,不依赖 argparse。"""
    import yaml
    from wtpy import WtBtEngine, EngineType
    from wtpy.apps import WtBtAnalyst
    from SpreadArbStrategy import SpreadArbStrategy

    config_dir = _PROJECT_ROOT / "config"
    exp_path = config_dir / "experiments" / f"{experiment}.yaml"
    with open(exp_path, "r", encoding="utf-8") as f:
        exp_cfg = yaml.safe_load(f) or {}
    bt_meta = exp_cfg.get("backtest", {})
    start = bt_meta.get("start", 202504010900)
    end = bt_meta.get("end", 202504151500)
    capital = bt_meta.get("capital", 1000000)
    slippage = bt_meta.get("slippage", 0)

    # 切到项目根目录(WtBtEngine 用相对路径)
    os.chdir(str(_PROJECT_ROOT))

    engine = WtBtEngine(EngineType.ET_CTA)
    engine.init("./common/", "configbt.yaml")
    engine.configBacktest(start, end)
    engine.configBTStorage(mode="csv", path="./storage/")
    engine.commitBTConfig()

    stra_name = f"spread_arb_{experiment}"
    straInfo = SpreadArbStrategy.from_experiment(
        name=stra_name,
        experiment=experiment,
        config_root=str(config_dir),
        is_backtest=True,
    )
    engine.set_cta_strategy(straInfo, slippage=slippage)
    engine.run_backtest(bAsync=False)

    # 不跑 analyst (它有自己的耗时,且不在策略热路径)
    engine.release_backtest()
    return stra_name


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment", default="profile_p3_6")
    parser.add_argument("--out-dir", default=None,
                        help="输出目录,默认 outputs_bt/<experiment>/")
    args = parser.parse_args()

    out_dir = Path(args.out_dir) if args.out_dir else (
        _PROJECT_ROOT / "outputs_bt" / args.experiment
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    prof_file = out_dir / "profile.prof"

    print(f"[P3-6] profile experiment={args.experiment}")
    print(f"[P3-6] output dir={out_dir}")

    pr = cProfile.Profile()
    pr.enable()
    try:
        stra_name = run_backtest(args.experiment)
    finally:
        pr.disable()

    pr.dump_stats(str(prof_file))
    print(f"[P3-6] prof saved: {prof_file}")

    # ---------- 文字报告 ----------
    # cumtime: 找慢的整段函数
    with open(out_dir / "profile_top_cumtime.txt", "w", encoding="utf-8") as f:
        st = pstats.Stats(str(prof_file), stream=f).strip_dirs().sort_stats("cumulative")
        st.print_stats(50)
    # tottime: 找自身耗时大的叶子热点
    with open(out_dir / "profile_top_tottime.txt", "w", encoding="utf-8") as f:
        st = pstats.Stats(str(prof_file), stream=f).strip_dirs().sort_stats("tottime")
        st.print_stats(50)
    # ncalls: 找调用次数极多的函数(可能可以缓存)
    with open(out_dir / "profile_top_ncalls.txt", "w", encoding="utf-8") as f:
        st = pstats.Stats(str(prof_file), stream=f).strip_dirs().sort_stats("ncalls")
        st.print_stats(50)
    # callers: top cumtime 的调用方
    with open(out_dir / "profile_callers_top10.txt", "w", encoding="utf-8") as f:
        st = pstats.Stats(str(prof_file), stream=f).strip_dirs().sort_stats("cumulative")
        st.print_callers(10)

    print(f"[P3-6] reports done in {out_dir}")
    print(f"  - profile_top_cumtime.txt")
    print(f"  - profile_top_tottime.txt")
    print(f"  - profile_top_ncalls.txt")
    print(f"  - profile_callers_top10.txt")


if __name__ == "__main__":
    main()
