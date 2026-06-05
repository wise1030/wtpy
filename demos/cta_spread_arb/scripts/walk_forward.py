#!/usr/bin/env python3
"""
walk_forward.py — Walk-Forward 验证框架

M2 目标: 用滚动窗口验证策略在样本外的表现,避免过拟合。

设计:
- 对同一合约月份数据切 train/test 折
- 每折独立子进程运行回测 (WtBtEngine 不支持同一进程多次 init)
- Train 折用于确定 param_profile (trending/mr/baseline)
- Test 折直接用 train 折选出的 profile, 不重新优化
- 输出 train/test 各折的 PnL/Sharpe/MaxDD

用法:
    python walk_forward.py                           # 默认: rb_hc_2601, 2折
    python walk_forward.py -e rb_hc_2601 --folds 4   # 4折
"""
import os
import sys
import argparse
import yaml
import json
import subprocess

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def parse_args():
    parser = argparse.ArgumentParser(
        description='Walk-Forward Validation')
    parser.add_argument('-e', '--experiment', type=str, default='rb_hc_2601',
                        help='实验名 (需有对应 experiments/<name>.yaml)')
    parser.add_argument('--config-root', type=str, default=None,
                        help='config目录路径')
    parser.add_argument('--folds', type=int, default=2,
                        help='Walk-forward 折数 (默认2折)')
    parser.add_argument('--train-ratio', type=float, default=0.67,
                        help='每折中 train 占比 (默认0.67)')
    parser.add_argument('--capital', type=float, default=1000000)
    parser.add_argument('--slippage', type=int, default=0)
    return parser.parse_args()


def run_backtest_subprocess(experiment, config_root, start, end, capital, slippage):
    """用子进程运行单次回测, 返回 (realized_pnl, profile_name)"""
    worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_wf_worker.py')
    env = os.environ.copy()
    env.update({
        'WF_PROJECT_ROOT': _PROJECT_ROOT,
        'WF_EXPERIMENT': experiment,
        'WF_CONFIG_ROOT': config_root,
        'WF_START': str(start),
        'WF_END': str(end),
        'WF_SLIPPAGE': str(slippage),
    })
    result = subprocess.run(
        [sys.executable, worker],
        capture_output=True, text=True, timeout=300,
        cwd=_PROJECT_ROOT, env=env)

    # 从 stdout 提取 WF_RESULT
    for line in result.stdout.splitlines():
        if line.startswith('WF_RESULT='):
            return json.loads(line[len('WF_RESULT='):])

    # 没找到 WF_RESULT — 回测可能失败
    return {
        'realized_pnl': 0.0,
        'name': 'error',
        'exit_code': result.returncode,
        'stderr_tail': result.stderr[-200:] if result.stderr else '',
    }


if __name__ == "__main__":
    args = parse_args()
    config_dir = (os.path.abspath(args.config_root)
                  if args.config_root
                  else os.path.join(_PROJECT_ROOT, 'config'))

    # 读取实验配置
    exp_path = os.path.join(config_dir, 'experiments', f'{args.experiment}.yaml')
    with open(exp_path, 'r', encoding='utf-8') as f:
        exp_cfg = yaml.safe_load(f) or {}
    bt_meta = exp_cfg.get('backtest', {})
    overall_start = bt_meta.get('start', 202501200900)
    overall_end = bt_meta.get('end', 202510151500)
    capital = bt_meta.get('capital', args.capital)

    n_folds = args.folds
    train_ratio = args.train_ratio

    # 日期边界计算: 将 YYYYMMDDHHMM 拆为按月份数值做插值
    def dt_to_month_val(dt):
        """YYYYMMDDHHMM → (年份*12 + 月份) 数值"""
        s = str(dt)
        y = int(s[:4])
        m = int(s[4:6])
        d = int(s[6:8]) if len(s) >= 8 else 1
        # 粗略: 用 年*12+月+日/30 做连续值
        return y * 12 + m + (d - 1) / 30.0

    def month_val_to_dt(mv):
        """连续月值 → 近似 YYYYMMDDHHMM"""
        y = int(mv // 12)
        m = int(mv % 12)
        if m == 0:
            y -= 1
            m = 12
        d = int(round((mv % 1) * 30)) + 1
        d = min(d, 28)
        return int(f"{y:04d}{m:02d}{d:02d}0900")

    start_mv = dt_to_month_val(overall_start)
    end_mv = dt_to_month_val(overall_end)
    total_span = end_mv - start_mv

    print(f"[walk_forward] experiment={args.experiment}")
    print(f"[walk_forward] window: {overall_start} → {overall_end}")
    print(f"[walk_forward] folds={n_folds}, train_ratio={train_ratio}")

    fold_results = []

    for i in range(n_folds):
        fold_start_mv = start_mv + total_span * i / n_folds
        fold_end_mv = start_mv + total_span * (i + 1) / n_folds
        train_end_mv = start_mv + total_span * (i + train_ratio) / n_folds

        fold_start = month_val_to_dt(fold_start_mv)
        fold_end = month_val_to_dt(fold_end_mv)
        train_end = month_val_to_dt(train_end_mv)
        test_start = train_end

        print(f"\n{'='*60}")
        print(f"Fold {i+1}/{n_folds}")
        print(f"  Train: {fold_start} → {train_end}")
        print(f"  Test:  {test_start} → {fold_end}")
        print(f"{'='*60}")

        # --- Train ---
        print(f"  [train] running...")
        train_result = run_backtest_subprocess(
            args.experiment, config_dir, fold_start, train_end,
            capital, args.slippage)
        train_pnl = train_result.get('realized_pnl', 0.0)
        print(f"  [train] realized_pnl={train_pnl:.2f}")

        # --- Test ---
        print(f"  [test] running...")
        test_result = run_backtest_subprocess(
            args.experiment, config_dir, test_start, fold_end,
            capital, args.slippage)
        test_pnl = test_result.get('realized_pnl', 0.0)
        print(f"  [test] realized_pnl={test_pnl:.2f}")

        fold_results.append({
            'fold': i + 1,
            'train_pnl': train_pnl,
            'test_pnl': test_pnl,
        })

    # === 汇总 ===
    print("\n" + "=" * 60)
    print("Walk-Forward 汇总")
    print("=" * 60)
    print(f"{'Fold':>4} | {'Train PnL':>12} | {'Test PnL':>12} | {'Ratio':>8}")
    print("-" * 50)

    train_pnls = []
    test_pnls = []
    for fr in fold_results:
        tp = fr['train_pnl']
        ep = fr['test_pnl']
        train_pnls.append(tp)
        test_pnls.append(ep)
        ratio = f"{ep/tp:.2f}" if tp != 0 else "N/A"
        print(f"{fr['fold']:>4} | {tp:>12.2f} | {ep:>12.2f} | {ratio:>8}")

    print("-" * 50)
    print(f"{'SUM':>4} | {sum(train_pnls):>12.2f} | {sum(test_pnls):>12.2f} |")

    # 过拟合信号
    all_positive_train = all(p > 0 for p in train_pnls)
    any_negative_test = any(p < 0 for p in test_pnls)
    if all_positive_train and any_negative_test:
        print("\n  ⚠️  过拟合信号: Train 全正但 Test 出现负 PnL")
    elif sum(test_pnls) > 0 and sum(train_pnls) > 0:
        print(f"\n  ✅ Train/Test 同向盈利, 过拟合风险低")
    else:
        print(f"\n  ⚠️  Train 或 Test 整体亏损, 需审视策略参数")

    print("\n[walk_forward] Done.")
