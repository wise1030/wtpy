#!/usr/bin/env python3
"""
run_batch.py — v10 批量回测 + 结果合并

v10单pipeline模式下, 每个合约月份是独立的experiment yaml。
本脚本:
1. 扫描 config/experiments/ 下所有实验(或指定列表)
2. 逐个运行回测
3. 合并所有实验的资金曲线到一份汇总报告

用法:
    python run_batch.py                          # 跑所有experiment
    python run_batch.py -e default rb_hc_2601    # 指定experiment列表
    python run_batch.py --config-root ./config    # 指定config目录
"""
import os
import sys
import argparse
import json
import shutil
from datetime import datetime

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
for _sub in ('strategies', 'helpers', 'factors'):
    _sub_path = os.path.join(_PROJECT_ROOT, _sub)
    if _sub_path not in sys.path:
        sys.path.insert(0, _sub_path)


def discover_experiments(config_root: str) -> list:
    """扫描 config/experiments/ 下所有 yaml 文件"""
    exp_dir = os.path.join(config_root, 'experiments')
    if not os.path.isdir(exp_dir):
        return []
    experiments = []
    for f in sorted(os.listdir(exp_dir)):
        if f.endswith('.yaml') or f.endswith('.yml'):
            experiments.append(f.rsplit('.', 1)[0])
    return experiments


def run_single_backtest(experiment: str, config_root: str, output_dir: str,
                        capital: float = 1000000, slippage: int = 0):
    """运行单个experiment回测, 返回结果目录路径"""
    from wtpy import WtBtEngine, EngineType
    from wtpy.apps import WtBtAnalyst
    from SpreadArbStrategy import SpreadArbStrategy
    import yaml

    # 读取实验配置获取回测时段
    exp_path = os.path.join(config_root, 'experiments', f'{experiment}.yaml')
    with open(exp_path, 'r', encoding='utf-8') as f:
        exp_cfg = yaml.safe_load(f) or {}

    bt_meta = exp_cfg.get('backtest', {})
    start = bt_meta.get('start', 202501200900)
    end = bt_meta.get('end', 202512311500)
    bt_capital = bt_meta.get('capital', capital)
    bt_slippage = bt_meta.get('slippage', slippage)

    # 创建引擎
    engine = WtBtEngine(EngineType.ET_CTA)
    engine.init('./common/', "configbt.yaml")
    engine.configBacktest(start, end)
    engine.configBTStorage(mode="csv", path="./storage/")
    engine.commitBTConfig()

    # 创建策略
    stra_name = f'spread_arb_{experiment}'
    stra = SpreadArbStrategy.from_experiment(
        name=stra_name,
        experiment=experiment,
        config_root=config_root,
        is_backtest=True,
    )
    engine.set_cta_strategy(stra, slippage=bt_slippage)
    engine.run_backtest(bAsync=False)

    # WT outputs to ./outputs_bt/<stra_name>/
    wt_output = os.path.join(_PROJECT_ROOT, 'outputs_bt', stra_name)

    # 复制输出到实验专属目录
    exp_output = os.path.join(output_dir, experiment)
    if os.path.exists(exp_output):
        shutil.rmtree(exp_output)
    if os.path.exists(wt_output):
        shutil.copytree(wt_output, exp_output)
    else:
        print(f"  [WARN] WT output not found at {wt_output}")

    # 生成分析报告 (WtBtAnalyst在batch模式下跳过，merge_results直接读funds.csv)
    # 如需详细报告，可对单个experiment用 runBT.py

    engine.release_backtest()
    return exp_output


def merge_results(output_dir: str, experiments: list, capital: float = 1000000):
    """合并多个实验的资金曲线"""
    import pandas as pd

    all_funds = {}
    for exp in experiments:
        funds_path = os.path.join(output_dir, exp, 'funds.csv')
        if not os.path.exists(funds_path):
            print(f"  [WARN] {exp}: funds.csv not found, skipping")
            continue
        df = pd.read_csv(funds_path)
        all_funds[exp] = df

    if not all_funds:
        print("[ERROR] No funds.csv found for any experiment")
        return

    # 合并: 按日期对齐, dynbalance 相加
    merged = None
    for exp, df in all_funds.items():
        df = df[['date', 'dynbalance']].copy()
        df.columns = ['date', f'pnl_{exp}']
        if merged is None:
            merged = df
        else:
            merged = merged.merge(df, on='date', how='outer')

    merged = merged.sort_values('date').fillna(0)
    pnl_cols = [c for c in merged.columns if c.startswith('pnl_')]
    merged['total_pnl'] = merged[pnl_cols].sum(axis=1)
    merged['capital'] = capital + merged['total_pnl']
    merged['return'] = merged['capital'] / capital - 1

    # 计算汇总指标
    final_return = merged['return'].iloc[-1]
    days = len(merged)
    annual_return = (1 + final_return) ** (240 / max(days, 1)) - 1

    # 最大回撤
    cummax = merged['capital'].cummax()
    drawdown = (merged['capital'] - cummax) / cummax
    max_dd = drawdown.min()

    # 胜率: 按日盈亏计算
    daily_pnl = merged['total_pnl'].diff().dropna()
    win_rate = (daily_pnl > 0).mean() * 100 if len(daily_pnl) > 0 else 0

    summary = {
        'capital': capital,
        'days': days,
        'total_return': float(final_return),
        'annual_return': float(annual_return),
        'max_drawdown': float(max_dd),
        'win_rate': float(win_rate),
        'experiments': experiments,
        'per_experiment_pnl': {
            exp: float(merged[f'pnl_{exp}'].iloc[-1])
            for exp in all_funds.keys()
        }
    }

    # 保存
    merged_path = os.path.join(output_dir, 'merged_funds.csv')
    merged.to_csv(merged_path, index=False)

    summary_path = os.path.join(output_dir, 'summary.json')
    with open(summary_path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*60}")
    print(f"合并回测结果 ({len(experiments)} experiments)")
    print(f"{'='*60}")
    print(f"  总收益:   {final_return*100:+.2f}%")
    print(f"  年化收益: {annual_return*100:+.2f}%")
    print(f"  最大回撤: {max_dd*100:.2f}%")
    print(f"  日胜率:   {win_rate:.1f}%")
    print(f"  Calmar:   {annual_return/max(abs(max_dd),1e-9):.2f}")
    print(f"\n  各实验PnL:")
    for exp, pnl in summary['per_experiment_pnl'].items():
        print(f"    {exp}: {pnl:+,.2f}")
    print(f"\n  合并资金曲线: {merged_path}")
    print(f"  汇总指标:     {summary_path}")


def main():
    parser = argparse.ArgumentParser(description='CTA Spread Arb Batch Backtest (v10)')
    parser.add_argument('-e', '--experiments', nargs='*', default=None,
                        help='指定experiment列表(空则扫描全部)')
    parser.add_argument('--config-root', type=str, default=None,
                        help='config目录路径')
    parser.add_argument('--capital', type=float, default=1000000, help='初始资金')
    parser.add_argument('--slippage', type=int, default=0, help='滑点')
    parser.add_argument('--no-merge', action='store_true',
                        help='不合并结果, 只跑各实验')
    args = parser.parse_args()

    config_root = os.path.abspath(args.config_root) if args.config_root else \
        os.path.join(_PROJECT_ROOT, 'config')

    # 发现实验
    if args.experiments:
        experiments = args.experiments
    else:
        experiments = discover_experiments(config_root)

    if not experiments:
        print("[ERROR] No experiments found")
        return

    print(f"Found {len(experiments)} experiments: {experiments}")

    # 输出目录
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_dir = os.path.join(_PROJECT_ROOT, 'backtest_results', f'batch_{timestamp}')
    os.makedirs(output_dir, exist_ok=True)

    # 逐个回测
    for i, exp in enumerate(experiments, 1):
        print(f"\n[{i}/{len(experiments)}] Running experiment: {exp}")
        try:
            run_single_backtest(
                experiment=exp,
                config_root=config_root,
                output_dir=output_dir,
                capital=args.capital,
                slippage=args.slippage,
            )
            print(f"  -> OK")
        except Exception as e:
            print(f"  -> FAILED: {e}")

    # 合并结果
    if not args.no_merge and len(experiments) > 1:
        merge_results(output_dir, experiments, args.capital)
    elif len(experiments) == 1:
        print(f"\n单实验, 无需合并。结果在: {output_dir}/{experiments[0]}/")


if __name__ == '__main__':
    main()
