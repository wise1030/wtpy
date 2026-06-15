#!/usr/bin/env python3
"""
run_matrix.py — 实验矩阵化工具
P1-A: 自动生成 (pair × profile × month) 笛卡尔积实验配置 + 并行跑 + 汇总

用法:
    python run_matrix.py -p l_pp --profiles auto mr --months 2609
    python run_matrix.py -p l_pp rb_hc --profiles auto mr trending --months 2509 2609
    python run_matrix.py -p l_pp --months 2609 --no-run   # 只显示将生成的组合
    python run_matrix.py -p l_pp rb_hc --months 2609 --parallel 4

输出:
    outputs_bt/matrix_{timestamp}/
      <pair>_<month>_<profile>/  ← 每个实验的输出目录 (由 run_multi 决定)
      matrix_summary.csv         ← 所有实验汇总 (net/fee/gross/winrate/dd 等)
      matrix_summary.json
"""

import os, sys, json, csv, itertools, subprocess, argparse, yaml, time
from pathlib import Path
from datetime import datetime
from collections import defaultdict

_PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_DIR = _PROJECT_ROOT / 'config'
EXPERIMENTS_DIR = CONFIG_DIR / 'experiments'
CONTRACTS_DIR = CONFIG_DIR / 'contracts'
PROFILES_DIR = CONFIG_DIR / 'profiles'

# 标准时间窗: 合约月份 → (start, end)
# 2509 → 2024/09/15~2025/09/14 左右 (2509合约生命周期)
# 2609 → 2025/09/15~2026/09/14
# 2601 → 2025/01~2025/12
DEFAULT_WINDOWS = {
    '2509': (202409150900, 202509141500),
    '2609': (202509150900, 202606051500),
    '2601': (202501200900, 202510151500),
    '2510': (202410160900, 202510151500),
}


# Profile 名映射: CLI 短名 → yaml profile 名
PROFILE_MAP = {
    'mr': 'mr_contract',
    'trending': 'trending_contract',
    'baseline': None,   # 不设 param_profile
    'auto': None,       # auto_profile 自动选择
}


def parse_args():
    parser = argparse.ArgumentParser(
        description='CTA Spread Arb - Experiment Matrix')
    parser.add_argument('-p', '--pairs', nargs='+', required=True,
                        help='Pair names (e.g., l_pp rb_hc)')
    parser.add_argument('--profiles', nargs='+', default=['auto'],
                        choices=['auto', 'mr', 'trending', 'baseline'],
                        help='profiles 列表 (默认: auto)')
    parser.add_argument('--months', nargs='+', required=True,
                        help='合约月份 (e.g., 2609 2509)')
    parser.add_argument('--parallel', type=int, default=2,
                        help='parallel backtests (default 2, rec 2-3)')
    parser.add_argument('--config-root', type=str, default=None)
    parser.add_argument('--capital', type=float, default=1000000)
    parser.add_argument('--slippage', type=int, default=0)
    parser.add_argument('--no-run', action='store_true',
                        help='Dry-run: 只显示组合不执行')
    parser.add_argument('--matrix-tag', type=str, default=None,
                        help='矩阵输出目录标签 (默认: auto timestamp)')
    return parser.parse_args()


def generate_experiment_yaml(pair, month, profile, template_cfg):
    """为 (pair, month, profile) 生成 experiment yaml"""
    # 从 pair 合约配置获取价格信息
    pair_cfg_path = CONTRACTS_DIR / f'{pair}.yaml'
    with open(pair_cfg_path) as f:
        pair_cfg = yaml.safe_load(f) or {}

    # 时间窗
    win = DEFAULT_WINDOWS.get(month, (202501200900, 202510151500))

    # 实例名
    inst_name = f'{pair}_{month}_m5'
    exp_name = f'matrix_{pair}_{month}_{profile}'

    # 构建 yaml
    exp = {
        'name': exp_name,
        'description': f'Matrix: {pair} {month} profile={profile}',
        'backtest': {
            'start': win[0],
            'end': win[1],
            'capital': args.capital if 'args' in dir() else 1000000,
            'slippage': 0,
        },
    }

    # auto_profile 配置
    if profile == 'auto':
        exp['auto_profile'] = {
            'enable': True,
            'trend_hurst': 0.55,
            'mr_hurst': 0.45,
            'trend_r2': 0.55,
            'lookback_days': 120,
            'storage_csv_dir': './storage/csv',
        }

    exp['instances'] = [{
        'name': inst_name,
        'contract': pair,
        'contract_month': month,
        'periods': {
            'main_period': 'm5',
            'main_bar_cnt': 200,
            'aux_periods': [{'period': 'd1', 'bar_cnt': 15}],
        },
        'signal_mode': 'adaptive',
    }]

    # profile 控制
    if profile == 'auto':
        exp['instances'][0]['auto_profile'] = True
    else:
        yaml_profile = PROFILE_MAP.get(profile, profile)
        if yaml_profile:
            exp['instances'][0]['param_profile'] = yaml_profile

    return exp, exp_name


def collect_metrics(output_dir):
    """从 runs 输出目录提取关键指标"""
    metrics = {}

    # summary.json (如果有)
    sp = os.path.join(output_dir, 'summary.json')
    if os.path.exists(sp):
        with open(sp) as f:
            s = json.load(f)
        metrics.update({
            k: s.get(k) for k in [
                'total_money', 'total_fee', 'total_net_pnl',
                'total_return', 'annual_return', 'max_drawdown',
                'win_ratio', 'profit_loss_ratio',
                'total_round', 'total_trade',
            ]
        })

    # closes.csv 详算
    cp = os.path.join(output_dir, 'closes.csv')
    if os.path.exists(cp):
        with open(cp) as f:
            closes = list(csv.DictReader(f))
        profits = [float(c['profit']) for c in closes]
        wins = sum(1 for p in profits if p > 0)
        losses = sum(1 for p in profits if p < 0)
        nc = len(closes)
        metrics['closes'] = nc
        metrics['wins'] = wins
        metrics['losses'] = losses
        metrics['gross_pnl'] = round(sum(profits), 2)
        metrics['win_rate'] = round(wins / nc * 100, 1) if nc else 0
        metrics['avg_win'] = round(
            sum(p for p in profits if p > 0) / wins, 2) if wins else 0
        metrics['avg_loss'] = round(
            sum(p for p in profits if p < 0) / losses, 2) if losses else 0

        # 持仓时长
        durations = [
            int(c['closebarno']) - int(c['openbarno'])
            for c in closes
        ]
        if durations:
            durations.sort()
            metrics['median_hold_bars'] = durations[len(durations)//2]

    # trades.csv fee
    tp = os.path.join(output_dir, 'trades.csv')
    if os.path.exists(tp):
        with open(tp) as f:
            trades = list(csv.DictReader(f))
        fees = [float(t['fee']) for t in trades if t.get('fee')]
        if fees:
            metrics['total_fee'] = round(sum(fees), 2)
            metrics['avg_fee_per_trade'] = round(sum(fees) / len(fees), 3)

    # net
    if 'gross_pnl' in metrics and 'total_fee' in metrics:
        metrics['net_pnl'] = round(
            metrics['gross_pnl'] - metrics['total_fee'], 2)
    elif 'total_fee' in metrics and 'total_net_pnl' in metrics:
        metrics['net_pnl'] = metrics['total_net_pnl']

    return metrics


def print_matrix_summary(results):
    """打印矩阵汇总表"""
    rows = []
    for (pair, month, profile), metrics in sorted(results.items()):
        rows.append({
            'pair': pair,
            'month': month,
            'profile': profile,
            'closes': metrics.get('closes', '?'),
            'gross': metrics.get('gross_pnl', '?'),
            'fee': metrics.get('total_fee', '?'),
            'net': metrics.get('net_pnl', '?'),
            'winpct': metrics.get('win_rate', '?'),
            'avg_win': metrics.get('avg_win', '?'),
            'avg_loss': metrics.get('avg_loss', '?'),
            'hold': metrics.get('median_hold_bars', '?'),
        })

    if not rows:
        print("  (no results)")
        return rows

    hdr = (f"{'pair':<8} {'month':<6} {'profile':<10} "
           f"{'closes':>7} {'gross':>8} {'fee':>8} {'net':>8} "
           f"{'win%':>6} {'avg_win':>9} {'avg_loss':>9} {'hold':>5}")
    print(hdr)
    print('-' * len(hdr))
    for r in rows:
        print(f"{r['pair']:<8} {r['month']:<6} {r['profile']:<10} "
              f"{r['closes']:>7} {r['gross']:>8} {r['fee']:>8} {r['net']:>8} "
              f"{r['winpct']:>6} {r['avg_win']:>9} {r['avg_loss']:>9} "
              f"{r['hold']:>5}")

    return rows


if __name__ == '__main__':
    args = parse_args()

    config_root = Path(args.config_root) if args.config_root else CONFIG_DIR
    matrix_exp_dir = config_root / 'experiments'

    # 1. 生成矩阵组合
    combos = list(itertools.product(args.pairs, args.months, args.profiles))
    print(f'[matrix] 组合数: {len(combos)} ({len(args.pairs)} pairs × '
          f'{len(args.months)} months × {len(args.profiles)} profiles)')
    for pair, month, profile in combos:
        print(f'  {pair:8} {month:6} {profile:10}')

    if args.no_run:
        print('[matrix] --no-run, 结束')
        sys.exit(0)

    # 2. 生成 experiment yamls
    print(f'\n[matrix] 生成实验配置...')
    exp_names = []
    for pair, month, profile in combos:
        exp_cfg, exp_name = generate_experiment_yaml(
            pair, month, profile, {})
        exp_cfg['backtest']['capital'] = args.capital
        exp_cfg['backtest']['slippage'] = args.slippage

        exp_path = matrix_exp_dir / f'{exp_name}.yaml'
        with open(exp_path, 'w') as f:
            yaml.dump(exp_cfg, f, default_flow_style=False,
                      sort_keys=False, allow_unicode=True)
        exp_names.append(exp_name)
        print(f'  ✓ {exp_path.name}')

    # 3. 并行执行 (子进程)
    print(f'\n[matrix] 执行 {len(exp_names)} 实验 (并行 {args.parallel})...')
    start_time = time.time()

    # 分批并行 (每组 N 个)
    all_metrics = {}
    batch_results = {}
    for i in range(0, len(exp_names), args.parallel):
        batch = exp_names[i:i + args.parallel]
        procs = []
        print(f'  批次 {i//args.parallel + 1}: {batch}')

        for exp_name in batch:
            cmd = [
                sys.executable, str(_PROJECT_ROOT / 'run_multi.py'),
                '-e', exp_name,
                '--config-root', str(matrix_exp_dir.parent),
            ]
            if args.capital != 1000000:
                cmd.extend(['--capital', str(args.capital)])
            if args.slippage != 0:
                cmd.extend(['--slippage', str(args.slippage)])

            proc = subprocess.Popen(
                cmd, cwd=str(_PROJECT_ROOT),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True)
            procs.append((exp_name, proc))

        # 等待当前批次完成
        for exp_name, proc in procs:
            stdout, stderr = proc.communicate()
            status = 'OK' if proc.returncode == 0 else 'FAIL'
            print(f'    {exp_name}: {status} (exit={proc.returncode}) '
                  f'stdout={len(stdout)}B stderr={len(stderr)}B')
            if proc.returncode != 0:
                print(f'    stderr: {stderr[-200:]}')

        # 收集结果
        for exp_name, proc in procs:
            if proc.returncode != 0:
                continue
            # run_multi 输出到 outputs_bt/{exp_name}/
            output_dir = _PROJECT_ROOT / 'outputs_bt' / f'spread_arb_{exp_name}'
            metrics = collect_metrics(str(output_dir) if output_dir.exists()
                                      else str(_PROJECT_ROOT / 'outputs_bt' / exp_name))
            if metrics:
                batch_results[exp_name] = metrics

    elapsed = time.time() - start_time
    print(f'\n[matrix] 运行完成, 耗时 {elapsed:.0f}s')

    # 4. 汇总输出
    # 把 exp_name 拆回 (pair, month, profile)
    results = {}
    for exp_name in batch_results:
        # 从已知 pairs 和 months 中匹配
        matched_pair = None
        matched_month = None
        for p in args.pairs:
            if p in exp_name:
                matched_pair = p
                break
        for m in args.months:
            if m in exp_name:
                matched_month = m
                break
        # profile 是剩余后缀
        # exp_name: matrix_{pair}_{month}_{profile}
        if matched_pair and matched_month:
            suffix = exp_name.replace('matrix_', '')
            profile = suffix.replace(f'{matched_pair}_', '').replace(f'{matched_month}_', '')
        else:
            matched_pair, matched_month, profile = exp_name, '?', '?'
        results[(matched_pair, matched_month, profile)] = batch_results[exp_name]

    if results:
        print(f'\n{"=" * 90}')
        print('矩阵汇总')
        print(f'{"=" * 90}')
        rows = print_matrix_summary(results)
    else:
        rows = []

    # 报告失败实验
    failed = [e for e in exp_names if e not in batch_results]
    if failed:
        print(f'\n[matrix] {len(failed)} 实验失败 (通常是数据缺失):')
        for f in failed:
            print(f'  ⚠ {f}')

    # 写 CSV
    csv_headers = ['pair', 'month', 'profile',
                   'closes', 'gross', 'fee', 'net',
                   'winpct', 'avg_win', 'avg_loss', 'hold']
    matrix_tag = args.matrix_tag or datetime.now().strftime('%Y%m%d_%H%M%S')
    matrix_dir = _PROJECT_ROOT / 'matrix_results'
    matrix_dir.mkdir(exist_ok=True)
    csv_path = matrix_dir / f'matrix_summary_{matrix_tag}.csv'
    if rows:
        with open(csv_path, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=csv_headers)
            w.writeheader()
            w.writerows(rows)
        print(f'\n[matrix] CSV: {csv_path}')

    # 铸 net 正/负 结论
    pos = [(k, v) for k, v in results.items()
           if v.get('net_pnl', -9999) > 0]
    neg = [(k, v) for k, v in results.items()
           if v.get('net_pnl', 9999) <= 0]
    print(f'\n[matrix] 正回报: {len(pos)} / {len(results)}')
    for pair, month, profile in sorted(k for k, _ in pos):
        v = results[(pair, month, profile)]
        print(f'  ✅ {pair} {month} {profile}: net={v["net_pnl"]}')
    if neg:
        print(f'  负/0 回报: {len(neg)}')
        for pair, month, profile in sorted(k for k, _ in neg):
            v = results[(pair, month, profile)]
            print(f'  ❌ {pair} {month} {profile}: net={v["net_pnl"]}')