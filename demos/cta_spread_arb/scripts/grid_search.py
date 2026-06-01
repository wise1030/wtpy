#!/usr/bin/env python3
"""参数网格搜索 v2 - 正确处理嵌套YAML结构
- entry_threshold 写入 factor_config.yaml -> signal_aggregation.entry_threshold
- risk_control.* 写入 strategy_config.yaml -> risk_control.xxx
- 结果写入文件，避免终端输出限制
"""
import os, sys, json, time, itertools, copy
import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STRAT_CFG = os.path.join(BASE_DIR, "config/strategy_config.yaml")
FACTOR_CFG = os.path.join(BASE_DIR, "config/factor_config.yaml")
RESULT_FILE = os.path.join(BASE_DIR, "grid_search_results.json")
PROGRESS_FILE = os.path.join(BASE_DIR, "grid_progress.txt")

# ============ 参数网格 ============
# key -> (target_file, yaml_path)
# target_file: 'strat' or 'factor'
# yaml_path: 嵌套路径，如 ['risk_control', 'max_loss_per_pair']
PARAM_MAP = {
    "entry_threshold":      ("factor", ["signal_aggregation", "entry_threshold"]),
    "max_loss_per_pair":    ("strat",  ["risk_control", "max_loss_per_pair"]),
    "max_hold_bars":        ("strat",  ["risk_control", "max_hold_bars"]),
    "cooldown_bars":        ("strat",  ["risk_control", "cooldown_bars"]),
    "trailing_decay_ratio": ("strat",  ["risk_control", "trailing_decay_ratio"]),
}

GRID = {
    "entry_threshold":      [0.30, 0.40, 0.50, 0.60],
    "max_loss_per_pair":    [3000, 5000, 8000],
    "max_hold_bars":        [15, 20, 30],
    "cooldown_bars":        [15, 25],
    "trailing_decay_ratio": [0.3, 0.5, 0.7],
}

def load_yaml(path):
    with open(path, 'r') as f:
        return yaml.safe_load(f)

def save_yaml(path, data):
    with open(path, 'w') as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

def set_nested(d, path, value):
    """设置嵌套字典值，如 set_nested(d, ['risk_control','max_loss'], 3000)"""
    for key in path[:-1]:
        d = d.setdefault(key, {})
    d[path[-1]] = value

def write_configs(params):
    """根据参数映射写入正确的YAML位置"""
    strat_cfg = load_yaml(STRAT_CFG)
    factor_cfg = load_yaml(FACTOR_CFG)
    
    for param_name, value in params.items():
        target, path = PARAM_MAP[param_name]
        if target == "strat":
            set_nested(strat_cfg, path, value)
        elif target == "factor":
            set_nested(factor_cfg, path, value)
    
    save_yaml(STRAT_CFG, strat_cfg)
    save_yaml(FACTOR_CFG, factor_cfg)

def run_backtest():
    import subprocess
    env = os.environ.copy()
    env["PYTHONPATH"] = BASE_DIR + ":" + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, os.path.join(BASE_DIR, "runBT.py")],
        cwd=BASE_DIR,
        capture_output=True, text=True, timeout=300,
        env=env
    )
    summary_path = os.path.join(BASE_DIR, "outputs_bt/spread_arb/summary.json")
    if os.path.exists(summary_path):
        with open(summary_path, 'r') as f:
            return json.load(f)
    return None

def extract_metrics(summary):
    if not summary:
        return None
    try:
        return {
            "total_return": round(summary.get("total_return", 0), 4),
            "sharpe": round(summary.get("sharpe_ratio", 0), 4),
            "max_drawdown": round(summary.get("max_drawdown", 0), 4),
            "win_rate": round(summary.get("win_rate", 0), 4),
            "total_trades": summary.get("total_trades", 0),
            "profit_factor": round(summary.get("profit_factor", 0), 4),
        }
    except Exception as e:
        return {"error": str(e)}

def main():
    keys = list(GRID.keys())
    values = list(GRID.values())
    combos = list(itertools.product(*values))
    total = len(combos)
    
    # 备份原始配置
    orig_strat = load_yaml(STRAT_CFG)
    orig_factor = load_yaml(FACTOR_CFG)
    
    results = []
    best_by_return = None
    best_by_sharpe = None
    
    start_time = time.time()
    
    with open(PROGRESS_FILE, 'w') as pf:
        pf.write(f"START: {total} combinations (v2 - fixed YAML nesting)\n")
    
    for i, combo in enumerate(combos):
        params = dict(zip(keys, combo))
        
        # 写入配置(正确处理嵌套)
        write_configs(params)
        
        # 跑回测
        try:
            summary = run_backtest()
            metrics = extract_metrics(summary)
        except Exception as e:
            metrics = {"error": str(e)}
        
        record = {
            "params": {k: v for k, v in params.items()},
            "metrics": metrics,
            "index": i,
        }
        results.append(record)
        
        if metrics and "error" not in metrics:
            if best_by_return is None or metrics["total_return"] > best_by_return["metrics"]["total_return"]:
                best_by_return = record
            if best_by_sharpe is None or metrics["sharpe"] > best_by_sharpe["metrics"]["sharpe"]:
                best_by_sharpe = record
        
        # 每5组写入中间结果
        if (i + 1) % 5 == 0 or i == total - 1:
            elapsed = time.time() - start_time
            with open(RESULT_FILE, 'w') as f:
                json.dump({
                    "total_combos": total,
                    "completed": i + 1,
                    "elapsed_sec": round(elapsed, 1),
                    "best_by_return": best_by_return,
                    "best_by_sharpe": best_by_sharpe,
                    "all_results": results,
                }, f, indent=2, ensure_ascii=False)
            
            br_ret = best_by_return['metrics']['total_return'] if best_by_return else 'N/A'
            bs_sh = best_by_sharpe['metrics']['sharpe'] if best_by_sharpe else 'N/A'
            with open(PROGRESS_FILE, 'w') as pf:
                pf.write(f"[{i+1}/{total}] {elapsed:.0f}s | best_ret={br_ret} | best_sharpe={bs_sh}\n")
    
    # 还原原始配置
    save_yaml(STRAT_CFG, orig_strat)
    save_yaml(FACTOR_CFG, orig_factor)
    
    elapsed = time.time() - start_time
    with open(PROGRESS_FILE, 'a') as pf:
        pf.write(f"DONE: {total} combos in {elapsed:.0f}s\n")
        if best_by_return:
            pf.write(f"Best Return: {best_by_return['metrics']} | {best_by_return['params']}\n")
        if best_by_sharpe:
            pf.write(f"Best Sharpe: {best_by_sharpe['metrics']} | {best_by_sharpe['params']}\n")

if __name__ == "__main__":
    main()
