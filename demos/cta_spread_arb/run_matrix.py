#!/usr/bin/env python3
"""
run_matrix.py — 统一实验矩阵化工具 (v2)

合并 run_multi.py + run_batch.py + 原 run_matrix.py 三个入口。

两种运行模式:
  --mode serial:   每个实验独立 WtBtEngine (进程隔离, 结果可靠, 慢)
  --mode session:  所有实验共用 1 个 WtBtEngine session (快, 验证独立性)
  --mode subprocess: 子进程隔离 (原 run_matrix 行为, 最可靠但最慢)

用法:
    # 矩阵模式: pair × profile × month 笛卡尔积
    python run_matrix.py -p l_pp --profiles auto mr --months 2609
    python run_matrix.py -p l_pp rb_hc --profiles auto mr trending --months 2509 2609
    python run_matrix.py -p l_pp --months 2609 --no-run   # 只显示组合

    # 直接指定实验列表 (兼容原 run_multi / run_batch)
    python run_matrix.py -e default rb_hc_2601 --mode session
    python run_matrix.py -e l_pp_2609 --mode serial

输出:
    matrix_results/matrix_summary_{tag}.csv  ← 汇总表
    outputs_bt/<exp_name>/                   ← 各实验 WT 输出
"""

import os, sys, json, csv, itertools, subprocess, argparse, yaml, time
from pathlib import Path
from datetime import datetime

_PROJECT_ROOT = Path(__file__).resolve().parent
for _sub in ('strategies', 'helpers', 'factors'):
    _sub_path = str(_PROJECT_ROOT / _sub)
    if _sub_path not in sys.path:
        sys.path.insert(0, _sub_path)

CONFIG_DIR = _PROJECT_ROOT / 'config'
EXPERIMENTS_DIR = CONFIG_DIR / 'experiments'
CONTRACTS_DIR = CONFIG_DIR / 'contracts'
MATRIX_DIR = _PROJECT_ROOT / 'matrix_results'

# 标准时间窗
DEFAULT_WINDOWS = {
    '2509': (202409150900, 202509141500),
    '2609': (202509150900, 202606051500),
    '2601': (202501200900, 202510151500),
    '2510': (202410160900, 202510151500),
}

PROFILE_MAP = {
    'mr': 'mr_contract',
    'trending': 'trending_contract',
    'baseline': None,
    'auto': None,
}


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(description='CTA Spread Arb - Unified Matrix Runner')
    # 矩阵模式
    p.add_argument('-p', '--pairs', nargs='+',
                   help='Pair names (e.g., l_pp rb_hc) — 矩阵模式')
    p.add_argument('--profiles', nargs='+', default=['auto'],
                   choices=['auto', 'mr', 'trending', 'baseline'])
    p.add_argument('--months', nargs='+',
                   help='合约月份 (e.g., 2609 2509) — 矩阵模式')
    # 直接实验列表模式
    p.add_argument('-e', '--experiments', nargs='*', default=None,
                   help='直接指定实验名列表 (空=扫描全部)')
    # 通用
    p.add_argument('--mode', choices=['serial', 'session', 'subprocess'],
                   default='serial',
                   help='serial=独立引擎 | session=共用引擎 | subprocess=子进程')
    p.add_argument('--config-root', type=str, default=None)
    p.add_argument('--capital', type=float, default=1000000)
    p.add_argument('--slippage', type=int, default=0)
    p.add_argument('--parallel', type=int, default=2,
                   help='subprocess 模式并行数')
    p.add_argument('--no-run', action='store_true',
                   help='Dry-run: 只显示组合不执行')
    p.add_argument('--no-merge', action='store_true',
                   help='不合并资金曲线')
    p.add_argument('--matrix-tag', type=str, default=None)
    return p.parse_args()


# ═══════════════════════════════════════════════════════════
# 实验发现
# ═══════════════════════════════════════════════════════════

def discover_experiments(config_root):
    """扫描 config/experiments/ 下所有 yaml"""
    exp_dir = Path(config_root) / 'experiments'
    if not exp_dir.is_dir():
        return []
    exps = []
    for f in sorted(exp_dir.iterdir()):
        if f.suffix in ('.yaml', '.yml'):
            exps.append(f.stem)
    return exps


def generate_matrix_experiments(args):
    """从 (pair × month × profile) 生成实验名列表"""
    combos = list(itertools.product(args.pairs, args.months, args.profiles))
    print(f'[matrix] 组合数: {len(combos)} ({len(args.pairs)} pairs × '
          f'{len(args.months)} months × {len(args.profiles)} profiles)')
    for pair, month, profile in combos:
        print(f'  {pair:8} {month:6} {profile:10}')

    if args.no_run:
        print('[matrix] --no-run, 结束')
        sys.exit(0)

    exp_names = []
    for pair, month, profile in combos:
        exp_name = f'matrix_{pair}_{month}_{profile}'
        _write_matrix_experiment_yaml(pair, month, profile, exp_name, args)
        exp_names.append(exp_name)
    return exp_names


def _write_matrix_experiment_yaml(pair, month, profile, exp_name, args):
    """生成单个矩阵实验 yaml"""
    win = DEFAULT_WINDOWS.get(month, (202501200900, 202510151500))
    inst_name = f'{pair}_{month}_m5'

    exp = {
        'name': exp_name,
        'description': f'Matrix: {pair} {month} profile={profile}',
        'backtest': {
            'start': win[0], 'end': win[1],
            'capital': args.capital, 'slippage': args.slippage,
        },
    }

    if profile == 'auto':
        exp['auto_profile'] = {
            'enable': True, 'trend_hurst': 0.55, 'mr_hurst': 0.45,
            'trend_r2': 0.55, 'lookback_days': 120,
            'storage_csv_dir': './storage/csv',
        }

    inst = {
        'name': inst_name, 'contract': pair, 'contract_month': month,
        'periods': {
            'main_period': 'm5', 'main_bar_cnt': 200,
            'aux_periods': [{'period': 'd1', 'bar_cnt': 15}],
        },
        'signal_mode': 'adaptive',
    }

    if profile == 'auto':
        inst['auto_profile'] = True
    else:
        yaml_profile = PROFILE_MAP.get(profile, profile)
        if yaml_profile:
            inst['param_profile'] = yaml_profile

    exp['instances'] = [inst]

    exp_path = EXPERIMENTS_DIR / f'{exp_name}.yaml'
    with open(exp_path, 'w') as f:
        yaml.dump(exp, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    print(f'  ✓ {exp_path.name}')


# ═══════════════════════════════════════════════════════════
# 运行模式
# ═══════════════════════════════════════════════════════════

def _load_experiment_meta(config_root, experiment):
    exp_path = Path(config_root) / 'experiments' / f'{experiment}.yaml'
    with open(exp_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def _resolve_backtest_window(experiments, config_root, args):
    """取所有实验窗口的交集 (最早start, 最晚end)"""
    starts, ends = [], []
    for exp_name in experiments:
        meta = _load_experiment_meta(config_root, exp_name)
        bt = meta.get('backtest', {})
        starts.append(bt.get('start', 202501200900))
        ends.append(bt.get('end', 202512311500))
    return min(starts), max(ends)


def run_serial(experiments, config_root, args):
    """serial 模式: 每个实验独立 WtBtEngine"""
    from wtpy import WtBtEngine, EngineType
    from SpreadArbStrategy import SpreadArbStrategy

    results = {}
    for i, exp_name in enumerate(experiments, 1):
        print(f'\n[{i}/{len(experiments)}] {exp_name}')
        try:
            meta = _load_experiment_meta(config_root, exp_name)
            bt = meta.get('backtest', {})
            start = bt.get('start', 202501200900)
            end = bt.get('end', 202512311500)

            engine = WtBtEngine(EngineType.ET_CTA)
            engine.init('./common/', "configbt.yaml")
            engine.configBacktest(start, end)
            engine.configBTStorage(mode="csv", path="./storage/")
            engine.commitBTConfig()

            stra_name = f'spread_arb_{exp_name}'
            stra = SpreadArbStrategy.from_experiment(
                name=stra_name, experiment=exp_name,
                config_root=config_root, is_backtest=True)
            engine.set_cta_strategy(stra, slippage=args.slippage)
            engine.run_backtest(bAsync=False)

            output_dir = _PROJECT_ROOT / 'outputs_bt' / stra_name
            metrics = collect_metrics(str(output_dir) if output_dir.exists() else None)
            if metrics:
                results[exp_name] = metrics
                print(f'  -> OK (net={metrics.get("net_pnl", "?")})')
            else:
                print(f'  -> OK (no metrics)')

            engine.release_backtest()
        except Exception as e:
            print(f'  -> FAILED: {e}')
    return results


def run_session(experiments, config_root, args):
    """session 模式: 所有实验共用 1 个 WtBtEngine"""
    from wtpy import WtBtEngine, EngineType
    from SpreadArbStrategy import SpreadArbStrategy

    start, end = _resolve_backtest_window(experiments, config_root, args)
    print(f'[session] backtest window: {start} → {end}')

    engine = WtBtEngine(EngineType.ET_CTA)
    engine.init('./common/', "configbt.yaml")
    engine.configBacktest(start, end)
    engine.configBTStorage(mode="csv", path="./storage/")
    engine.commitBTConfig()

    strategies = []
    for exp_name in experiments:
        stra_name = f'spread_arb_{exp_name}'
        print(f'[session] registering: {stra_name}')
        stra = SpreadArbStrategy.from_experiment(
            name=stra_name, experiment=exp_name,
            config_root=config_root, is_backtest=True)
        engine.set_cta_strategy(stra, slippage=args.slippage)
        strategies.append((stra_name, stra))

    print(f'[session] {len(strategies)} strategies, running...')
    engine.run_backtest(bAsync=False)

    # 收集结果
    results = {}
    for stra_name, stra in strategies:
        output_dir = _PROJECT_ROOT / 'outputs_bt' / stra_name
        metrics = collect_metrics(str(output_dir) if output_dir.exists() else None)
        if metrics:
            results[stra_name.replace('spread_arb_', '')] = metrics

    engine.release_backtest()

    # 独立性验证
    names = [stra.core.name for _, stra in strategies]
    if len(names) != len(set(names)):
        print('\n  *** WARNING: ArbCore.name 冲突! ***')
    else:
        print(f'\n  ✅ {len(names)} 实例 name 唯一')
    return results


def run_subprocess(experiments, config_root, args):
    """subprocess 模式: 每个实验独立子进程 (原 run_matrix 行为)"""
    results = {}
    for i in range(0, len(experiments), args.parallel):
        batch = experiments[i:i + args.parallel]
        procs = []
        print(f'  批次 {i // args.parallel + 1}: {batch}')

        for exp_name in batch:
            # 直接用 runBT.py 逻辑: 子进程跑单个实验
            script = str(_PROJECT_ROOT / 'runBT.py')
            cmd = [sys.executable, script, '-e', exp_name,
                   '--config-root', str(config_root)]
            proc = subprocess.Popen(
                cmd, cwd=str(_PROJECT_ROOT),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            procs.append((exp_name, proc))

        for exp_name, proc in procs:
            stdout, stderr = proc.communicate()
            status = 'OK' if proc.returncode == 0 else 'FAIL'
            print(f'    {exp_name}: {status} (exit={proc.returncode})')
            if proc.returncode != 0 and stderr:
                print(f'    stderr: {stderr[-200:]}')

        for exp_name, proc in procs:
            if proc.returncode != 0:
                continue
            output_dir = _PROJECT_ROOT / 'outputs_bt' / f'spread_arb_{exp_name}'
            if not output_dir.exists():
                output_dir = _PROJECT_ROOT / 'outputs_bt' / exp_name
            metrics = collect_metrics(str(output_dir) if output_dir.exists() else None)
            if metrics:
                results[exp_name] = metrics
    return results


# ═══════════════════════════════════════════════════════════
# 指标收集
# ═══════════════════════════════════════════════════════════

def collect_metrics(output_dir):
    """从 WT 输出目录提取关键指标"""
    if not output_dir or not os.path.isdir(output_dir):
        return None
    metrics = {}

    # summary.json
    sp = os.path.join(output_dir, 'summary.json')
    if os.path.exists(sp):
        with open(sp) as f:
            s = json.load(f)
        for k in ['total_money', 'total_fee', 'total_net_pnl',
                  'total_return', 'annual_return', 'max_drawdown',
                  'win_ratio', 'profit_loss_ratio', 'total_round', 'total_trade']:
            if k in s:
                metrics[k] = s[k]

    # closes.csv
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
        metrics['avg_win'] = round(sum(p for p in profits if p > 0) / wins, 2) if wins else 0
        metrics['avg_loss'] = round(sum(p for p in profits if p < 0) / losses, 2) if losses else 0
        durations = [int(c['closebarno']) - int(c['openbarno']) for c in closes]
        if durations:
            durations.sort()
            metrics['median_hold_bars'] = durations[len(durations) // 2]

    # trades.csv
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
        metrics['net_pnl'] = round(metrics['gross_pnl'] - metrics['total_fee'], 2)
    elif 'total_fee' in metrics and 'total_net_pnl' in metrics:
        metrics['net_pnl'] = metrics['total_net_pnl']

    return metrics


# ═══════════════════════════════════════════════════════════
# 汇总输出
# ═══════════════════════════════════════════════════════════

def print_summary(results, experiments):
    """打印矩阵汇总表"""
    rows = []
    for exp_name in sorted(results):
        m = results[exp_name]
        rows.append({
            'experiment': exp_name,
            'closes': m.get('closes', '?'),
            'gross': m.get('gross_pnl', '?'),
            'fee': m.get('total_fee', '?'),
            'net': m.get('net_pnl', '?'),
            'winpct': m.get('win_rate', '?'),
            'avg_win': m.get('avg_win', '?'),
            'avg_loss': m.get('avg_loss', '?'),
            'hold': m.get('median_hold_bars', '?'),
        })

    if not rows:
        print("  (no results)")
        return rows

    hdr = (f"{'experiment':<35} {'closes':>7} {'gross':>8} {'fee':>8} "
           f"{'net':>8} {'win%':>6} {'avg_win':>9} {'avg_loss':>9} {'hold':>5}")
    print(hdr)
    print('-' * len(hdr))
    for r in rows:
        print(f"{r['experiment']:<35} {r['closes']:>7} {r['gross']:>8} "
              f"{r['fee']:>8} {r['net']:>8} {r['winpct']:>6} "
              f"{r['avg_win']:>9} {r['avg_loss']:>9} {r['hold']:>5}")
    return rows


def write_csv(rows, tag):
    csv_headers = ['experiment', 'closes', 'gross', 'fee', 'net',
                   'winpct', 'avg_win', 'avg_loss', 'hold']
    MATRIX_DIR.mkdir(exist_ok=True)
    csv_path = MATRIX_DIR / f'matrix_summary_{tag}.csv'
    with open(csv_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=csv_headers)
        w.writeheader()
        w.writerows(rows)
    print(f'\n[matrix] CSV: {csv_path}')
    return csv_path


def print_verdict(results):
    pos = [(k, v) for k, v in results.items() if v.get('net_pnl', -9999) > 0]
    neg = [(k, v) for k, v in results.items() if v.get('net_pnl', 9999) <= 0]
    print(f'\n[matrix] 正回报: {len(pos)} / {len(results)}')
    for k, v in sorted(pos):
        print(f'  ✅ {k}: net={v["net_pnl"]}')
    if neg:
        print(f'  负/0 回报: {len(neg)}')
        for k, v in sorted(neg):
            print(f'  ❌ {k}: net={v["net_pnl"]}')


# ═══════════════════════════════════════════════════════════
# main
# ═══════════════════════════════════════════════════════════

if __name__ == '__main__':
    args = parse_args()
    config_root = str(Path(args.config_root).resolve()) if args.config_root else str(CONFIG_DIR)

    # 确定实验列表
    if args.pairs and args.months:
        # 矩阵模式
        experiments = generate_matrix_experiments(args)
    elif args.experiments is not None:
        # 直接指定
        experiments = args.experiments if args.experiments else discover_experiments(config_root)
    else:
        print('[ERROR] 需要 -p/--pairs + --months (矩阵模式) 或 -e/--experiments (直接模式)')
        sys.exit(1)

    if not experiments:
        print('[ERROR] No experiments found')
        sys.exit(1)

    print(f'\n[matrix] {len(experiments)} experiments, mode={args.mode}')
    for e in experiments:
        print(f'  - {e}')

    if args.no_run:
        sys.exit(0)

    # 运行
    start_time = time.time()

    if args.mode == 'serial':
        results = run_serial(experiments, config_root, args)
    elif args.mode == 'session':
        results = run_session(experiments, config_root, args)
    else:  # subprocess
        results = run_subprocess(experiments, config_root, args)

    elapsed = time.time() - start_time
    print(f'\n[matrix] 运行完成, 耗时 {elapsed:.0f}s')

    # 汇总
    if results:
        print(f'\n{"=" * 90}')
        print('矩阵汇总')
        print(f'{"=" * 90}')
        rows = print_summary(results, experiments)

        tag = args.matrix_tag or datetime.now().strftime('%Y%m%d_%H%M%S')
        write_csv(rows, tag)
        print_verdict(results)

    # 失败报告
    failed = [e for e in experiments if e not in results]
    if failed:
        print(f'\n[matrix] {len(failed)} 实验失败:')
        for f in failed:
            print(f'  ⚠ {f}')

    print('\n[matrix] Done.')
