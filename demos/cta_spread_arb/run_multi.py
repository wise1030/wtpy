#!/usr/bin/env python3
"""
run_multi.py — 多 ArbCore 实例并行回测

M1 验证: 在同一 WtBtEngine session 内注册 2 个 SpreadArbStrategy 实例,
验证 StateManager key / ArbIndexWriter DSB 路径 / RiskMgr peak 独立性。

用法:
    python run_multi.py                                    # 默认: rb_hc_2510 + rb_hc_2601
    python run_multi.py -e default rb_hc_2601             # 指定实验列表
    python run_multi.py --config-root ./config             # 指定config目录
    python run_multi.py --capital 2000000                  # 初始资金
"""
import os
import sys
import argparse
import yaml

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
for _sub in ('strategies', 'helpers', 'factors'):
    _sub_path = os.path.join(_PROJECT_ROOT, _sub)
    if _sub_path not in sys.path:
        sys.path.insert(0, _sub_path)

from wtpy import WtBtEngine, EngineType
from wtpy.apps import WtBtAnalyst
from SpreadArbStrategy import SpreadArbStrategy


def parse_args():
    parser = argparse.ArgumentParser(
        description='CTA Spread Arb - Multi-instance Backtest')
    parser.add_argument('-e', '--experiments', nargs='+',
                        default=['default', 'rb_hc_2601'],
                        help='实验名列表 (对应 config/experiments/<name>.yaml)')
    parser.add_argument('--config-root', type=str, default=None,
                        help='config目录路径；默认使用项目根 config/')
    parser.add_argument('--start', type=int, default=None,
                        help='回测起始 YYYYMMDDHHMM')
    parser.add_argument('--end', type=int, default=None,
                        help='回测结束 YYYYMMDDHHMM')
    parser.add_argument('--capital', type=float, default=1000000,
                        help='初始资金')
    parser.add_argument('--slippage', type=int, default=0, help='滑点')
    return parser.parse_args()


def load_experiment_meta(config_root, experiment):
    """读取实验 yaml 的 backtest 段"""
    exp_path = os.path.join(config_root, 'experiments', f'{experiment}.yaml')
    with open(exp_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


if __name__ == "__main__":
    args = parse_args()
    config_dir = (os.path.abspath(args.config_root)
                  if args.config_root
                  else os.path.join(_PROJECT_ROOT, 'config'))

    # 从所有实验中取回测窗口: CLI > 所有实验的交集 > 默认
    experiments = args.experiments
    exp_metas = {}
    for exp_name in experiments:
        exp_metas[exp_name] = load_experiment_meta(config_dir, exp_name)

    # 取最早的 start 和最晚的 end (覆盖所有实验窗口)
    starts, ends = [], []
    for meta in exp_metas.values():
        bt = meta.get('backtest', {})
        starts.append(bt.get('start', 202501200900))
        ends.append(bt.get('end', 202510151500))

    start = args.start or min(starts)
    end = args.end or max(ends)

    print(f"[run_multi] experiments={experiments}")
    print(f"[run_multi] backtest window: {start} → {end}")

    # === 初始化引擎 (共用 1 个 session) ===
    engine = WtBtEngine(EngineType.ET_CTA)
    engine.init('./common/', "configbt.yaml")
    engine.configBacktest(start, end)
    engine.configBTStorage(mode="csv", path="./storage/")
    engine.commitBTConfig()

    # === 注册多个策略实例 ===
    strategies = []
    for exp_name in experiments:
        stra_name = f'spread_arb_{exp_name}'
        print(f"[run_multi] registering strategy: {stra_name}")
        stra = SpreadArbStrategy.from_experiment(
            name=stra_name,
            experiment=exp_name,
            config_root=config_dir,
            is_backtest=True,
        )
        engine.set_cta_strategy(stra, slippage=args.slippage)
        strategies.append((stra_name, stra))

    print(f"[run_multi] {len(strategies)} strategies registered, running backtest...")
    engine.run_backtest(bAsync=False)

    # === 分析结果 (每个策略独立分析) ===
    analyst = WtBtAnalyst()
    for stra_name, stra in strategies:
        print(f"[run_multi] analyzing: {stra_name}")
        analyst.add_strategy(
            stra_name, folder="./outputs_bt/",
            init_capital=args.capital, rf=0.02, annual_trading_days=240)
    analyst.run_new()

    engine.release_backtest()

    # === 独立性验证摘要 ===
    print("\n" + "=" * 60)
    print("M1 独立性验证摘要")
    print("=" * 60)
    for stra_name, stra in strategies:
        core = stra.core
        sm = core.sm
        rm = core.rm
        print(f"\n  Strategy: {stra_name}")
        print(f"    ArbCore.name         = {core.name}")
        print(f"    StateManager._sub_name = {sm._sub_name}")
        print(f"    StateManager._keys   = {sm._keys or '(empty)'}")
        print(f"    RiskMgr._realized_pnl= {rm._realized_pnl:.2f}")
        print(f"    RiskMgr._cooldown    = {rm._cooldown}")
        print(f"    RiskMgr._hold_bars   = {rm._hold_bars}")

    # 交叉验证: 两个实例的 sub_name 必须不同
    names = [stra.core.name for _, stra in strategies]
    if len(names) != len(set(names)):
        print("\n  *** WARNING: ArbCore.name 冲突! ***")
    else:
        print(f"\n  ✅ {len(names)} 实例 name 唯一: {names}")

    print("\n[run_multi] Done.")
