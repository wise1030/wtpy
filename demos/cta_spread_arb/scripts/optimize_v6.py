#!/usr/bin/env python3
"""
参数优化 v6 - 重构版

改进:
1. subprocess隔离运行，绕过WtBtEngine @singleton限制
2. 断点续跑：已完成的组合跳过
3. 精简参数空间：先粗搜再细搜
4. 进度实时输出，不会卡住
5. 结果按sharpe排序，输出TOP 10
"""
import sys, os, json, time, shutil
import yaml
from itertools import product
from subprocess import run, TimeoutExpired

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(BASE, 'config')
STRAT_CFG = os.path.join(CONFIG_DIR, 'strategy_config.yaml')
FACTOR_CFG = os.path.join(CONFIG_DIR, 'factor_config.yaml')
RESULTS_FILE = os.path.join(BASE, 'opt_results_v6.json')
RUNNER = os.path.join(BASE, '_runner.py')
CHECKPOINT_FILE = os.path.join(BASE, 'opt_v6_checkpoint.json')

# 粗搜参数空间 — 只搜最敏感的3个参数
PARAM_GRID = {
    'entry_threshold':   [0.20, 0.25, 0.30, 0.35, 0.40, 0.45],
    'exit_threshold':    [0.05, 0.08, 0.10, 0.12, 0.15],
    'direction_threshold': [0.10, 0.15, 0.20, 0.25],
}
# 6*5*4 = 120 种组合（比之前900少很多）

# 固定参数
FIXED_PARAMS = {
    'signal_mode': 'adaptive',
    'max_spread_deviation': 3.0,
    'max_loss_per_pair': 5000,
}

TIMEOUT_PER_RUN = 180  # 每次回测超时3分钟


def modify_yaml(params):
    """修改配置文件"""
    with open(FACTOR_CFG, 'r') as f:
        fc = yaml.full_load(f)
    fc['signal_aggregation']['entry_threshold'] = params['entry_threshold']
    fc['signal_aggregation']['exit_threshold'] = params['exit_threshold']
    fc['signal_aggregation']['direction_threshold'] = params['direction_threshold']
    with open(FACTOR_CFG, 'w') as f:
        yaml.dump(fc, f, allow_unicode=True, default_flow_style=False)

    with open(STRAT_CFG, 'r') as f:
        sc = yaml.full_load(f)
    for s in sc.get('strategies', []):
        s['signal_mode'] = params['signal_mode']
    sc['risk_control']['max_spread_deviation'] = params['max_spread_deviation']
    sc['risk_control']['max_loss_per_pair'] = params['max_loss_per_pair']
    sc['risk_control']['trailing_decay_ratio'] = params.get('trailing_decay_ratio', 0.3)
    if 'adaptive' not in sc:
        sc['adaptive'] = {}
    sc['adaptive']['hl_mr_threshold'] = params.get('adaptive_hl_mr_threshold', 15.0)
    sc['adaptive']['hl_trend_threshold'] = params.get('adaptive_hl_trend_threshold', 30.0)
    sc['adaptive']['trend_z_threshold'] = params.get('adaptive_trend_z_threshold', 1.0)
    with open(STRAT_CFG, 'w') as f:
        yaml.dump(sc, f, allow_unicode=True, default_flow_style=False)


def param_key(params):
    """生成参数组合的唯一key，用于断点续跑"""
    return f"e={params['entry_threshold']}_x={params['exit_threshold']}_d={params['direction_threshold']}_m={params['signal_mode']}"


def load_checkpoint():
    """加载断点"""
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE, 'r') as f:
            return json.load(f)
    return {'completed': {}, 'results': []}


def save_checkpoint(cp):
    """保存断点"""
    with open(CHECKPOINT_FILE, 'w') as f:
        json.dump(cp, f, indent=2, ensure_ascii=False)


def run_single(params, result_file):
    """运行单次回测"""
    if os.path.exists(result_file):
        os.remove(result_file)

    try:
        proc = run(
            [sys.executable, RUNNER, json.dumps(params)],
            capture_output=True, text=True, timeout=TIMEOUT_PER_RUN, cwd=BASE
        )
    except TimeoutExpired:
        return {'params': params, 'sharpe': -999, 'win_rate': 0,
                'total_return': 0, 'error': 'timeout'}
    except Exception as e:
        return {'params': params, 'sharpe': -999, 'win_rate': 0,
                'total_return': 0, 'error': str(e)}

    if os.path.exists(result_file):
        try:
            with open(result_file) as f:
                r = json.load(f)
            r['params'] = params
            return r
        except Exception:
            pass

    return {'params': params, 'sharpe': -999, 'win_rate': 0,
            'total_return': 0, 'error': 'no_result'}


def main():
    combos = list(product(
        PARAM_GRID['entry_threshold'],
        PARAM_GRID['exit_threshold'],
        PARAM_GRID['direction_threshold'],
    ))
    total = len(combos)

    # 加载断点
    cp = load_checkpoint()
    completed_keys = set(cp['completed'].keys())
    results = cp['results']

    # 过滤已完成的组合
    pending = []
    for combo in combos:
        params = {
            'entry_threshold': combo[0],
            'exit_threshold': combo[1],
            'direction_threshold': combo[2],
            **FIXED_PARAMS,
        }
        key = param_key(params)
        if key not in completed_keys:
            pending.append(params)

    print(f"Total: {total}, Completed: {len(completed_keys)}, Pending: {len(pending)}")

    if not pending:
        print("All combinations already completed!")
    else:
        t0 = time.time()
        result_file = os.path.join(BASE, '_result.json')

        for i, params in enumerate(pending):
            key = param_key(params)

            # 修改配置
            modify_yaml(params)

            # 运行回测
            r = run_single(params, result_file)
            results.append(r)
            cp['completed'][key] = r.get('sharpe', -999)
            cp['results'] = results

            # 每5个组合保存一次断点
            if (i + 1) % 5 == 0 or i == len(pending) - 1:
                save_checkpoint(cp)

            # 进度输出
            elapsed = time.time() - t0
            done = len(completed_keys) + i + 1
            eta = elapsed / (i + 1) * (len(pending) - i - 1)
            valid = [r for r in results if r.get('sharpe', -999) not in (9999, -999)]
            if valid:
                best = max(valid, key=lambda x: x.get('sharpe', -999))
                print(f"[{done}/{total}] {elapsed:.0f}s ETA {eta:.0f}s | "
                      f"Best S={best['sharpe']:.2f} W={best.get('win_rate',0):.1f}% | "
                      f"key={key}")
            else:
                print(f"[{done}/{total}] {elapsed:.0f}s ETA {eta:.0f}s | key={key}")

    # 保存最终结果
    with open(RESULTS_FILE, 'w') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    # 排序输出
    traded = [r for r in results if r.get('sharpe', -999) not in (9999, -999)]
    traded.sort(key=lambda x: x.get('sharpe', -999), reverse=True)

    print(f"\n===== TOP 10 (traded) =====")
    for rank, r in enumerate(traded[:10], 1):
        p = r['params']
        print(f"#{rank} S={r['sharpe']:.2f} W={r.get('win_rate',0):.1f}% "
              f"R={r.get('total_return',0):.2f}% MD={r.get('max_falldown',0):.1f}% | "
              f"e={p['entry_threshold']} x={p['exit_threshold']} d={p['direction_threshold']}")

    no_trade = [r for r in results if r.get('sharpe', -999) == 9999]
    print(f"\nNo-trade combos: {len(no_trade)}/{total}")
    print(f"Traded combos: {len(traded)}/{total}")

    if traded:
        best = traded[0]
        print(f"\nBEST: {json.dumps(best, indent=2, ensure_ascii=False)}")

        # 自动应用最佳参数到配置文件
        print(f"\nApplying best params to config files...")
        modify_yaml(best['params'])
        print("Done! Config files updated.")


if __name__ == '__main__':
    main()

