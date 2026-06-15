"""preflight — 实盘启动期静态自检 (R2-B)

职责: 在 WtEngine 启动之前验证环境/配置完整性。失败时打印明确原因并 sys.exit(2)。
不依赖 WT 运行时上下文 (CtaContext), 全部走文件系统/YAML/JSON 验证。

检查项:
  1. WT 根配置文件存在 (config.yaml, executers/tdtraders/tdparsers/
     filters/actpolicy/logcfg yaml)
  2. common/ basefiles 完整且 JSON 合法
     (commodities, contracts, holidays, hots, sessions, fees)
  3. storage/ 目录可写
  4. logs/ 目录可写 (DiagLogger 输出)
  5. 策略配置文件存在 (legacy 双 yaml 或 experiments/<name>.yaml)

设计原则:
  - 任何一项失败 -> 收集所有失败原因后 sys.exit(2), 不带病启动
  - 不输出任何凭据 (tdtraders/tdparsers 内的 user/pass 字段)
  - 退出码 0=ok, 2=preflight 失败
"""
from __future__ import annotations

import json
import os
import sys
from typing import List, Tuple

# common/ basefiles 必备清单 (config.yaml 引用)
COMMON_BASEFILES = [
    'commodities.json',
    'contracts.json',
    'holidays.json',
    'hots.json',
    'sessions.json',
    'fees.json',
]

# WT 框架 yaml 必备清单 (config.yaml 内 executers/parsers/traders/filters/bspolicy 引用)
WT_ROOT_YAMLS = [
    'config.yaml',
    'executers.yaml',
    'tdtraders.yaml',
    'tdparsers.yaml',
    'filters.yaml',
    'actpolicy.yaml',
    'logcfg.yaml',
]


def _check_file_exists(path: str, label: str) -> Tuple[bool, str]:
    """返回 (ok, msg)。"""
    if not os.path.exists(path):
        return False, f"[MISSING] {label}: {path}"
    if not os.path.isfile(path):
        return False, f"[NOT_FILE] {label}: {path}"
    return True, f"[OK] {label}"


def _check_json_valid(path: str, label: str) -> Tuple[bool, str]:
    ok, msg = _check_file_exists(path, label)
    if not ok:
        return False, msg
    try:
        with open(path, 'r', encoding='utf-8') as f:
            json.load(f)
        return True, f"[OK] {label} (json valid)"
    except json.JSONDecodeError as exc:
        return False, f"[INVALID_JSON] {label}: {path} :: {exc}"
    except UnicodeDecodeError:
        # WT 部分老 JSON 是 GBK, 兼容尝试
        try:
            with open(path, 'r', encoding='gbk') as f:
                json.load(f)
            return True, f"[OK] {label} (json valid, gbk)"
        except Exception as exc2:
            return False, f"[ENCODING_ERR] {label}: {path} :: {exc2}"


def _check_dir_writable(path: str, label: str) -> Tuple[bool, str]:
    if not os.path.exists(path):
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            return False, f"[MKDIR_FAIL] {label}: {path} :: {exc}"
    if not os.path.isdir(path):
        return False, f"[NOT_DIR] {label}: {path}"
    # 试写一个临时文件
    probe = os.path.join(path, '.preflight_probe')
    try:
        with open(probe, 'w') as f:
            f.write('preflight')
        os.remove(probe)
        return True, f"[OK] {label} (writable)"
    except OSError as exc:
        return False, f"[NOT_WRITABLE] {label}: {path} :: {exc}"


def _check_pair_economics(config_root: str, experiment: str) -> Tuple[bool, str]:
    """验证品种对 fee/notional 比率, 防止 rb/hc 类结构性亏损。

    从 experiment yaml 解析合约配置, 计算:
    - 单边 fee 估算 (按费率 × 名义价值 或 定额)
    - 单轮 fee / 最小 tick 价值 比率
    - 如果比率 > 1.5% → WARNING (类似 rb/hc 的 fee 侵蚀问题)

    Returns:
        (ok, msg): ok=False 表示有严重警告但不阻塞启动
    """
    import yaml as _yaml

    exp_path = os.path.join(config_root, 'experiments', f'{experiment}.yaml')
    try:
        with open(exp_path, 'r', encoding='utf-8') as f:
            exp_cfg = _yaml.safe_load(f) or {}
    except Exception as exc:
        return False, f"[PAIR_ECON_ERR] 无法读取实验配置: {exc}"

    instances = exp_cfg.get('instances', [])
    if not instances:
        return True, "[PAIR_ECON] 无 instances, 跳过"

    warnings = []
    for inst in instances:
        contract_name = inst.get('contract')
        if not contract_name:
            continue

        contract_path = os.path.join(config_root, 'contracts', f'{contract_name}.yaml')
        try:
            with open(contract_path, 'r', encoding='utf-8') as f:
                contract_cfg = _yaml.safe_load(f) or {}
        except Exception:
            warnings.append(f"{contract_name}: 合约配置不可读")
            continue

        legs = contract_cfg.get('legs_template', [])
        if len(legs) < 2:
            continue

        # 估算各腿 fee 和名义价值
        total_fee_est = 0.0
        total_notional = 0.0
        for leg in legs:
            mult = leg.get('multiplier', 10)
            lots = leg.get('lots', 1)
            fee_rate = leg.get('fee_rate', 0)  # 万分之 (0=定额)
            fee_fixed = leg.get('fee_fixed', 0)  # 定额 (元/手)
            ref_price = leg.get('ref_price', 3000)  # 参考价格

            notional = ref_price * mult * lots
            total_notional += notional

            if fee_rate > 0:
                total_fee_est += notional * fee_rate / 10000 * lots
            else:
                total_fee_est += fee_fixed * lots

        # 单轮 fee (开+平)
        round_fee = total_fee_est * 2

        # 最小 tick 价值
        min_tick_value = 0.0
        for leg in legs:
            mult = leg.get('multiplier', 10)
            lots = leg.get('lots', 1)
            tick_size = leg.get('tick_size', 1)
            min_tick_value += tick_size * mult * lots

        if min_tick_value > 0:
            fee_ratio = round_fee / min_tick_value
            if fee_ratio > 2.0:
                warnings.append(
                    f"{contract_name}: 单轮fee/最小tick={fee_ratio:.1f}x "
                    f"(fee≈{round_fee:.0f}, tick={min_tick_value:.0f}) — "
                    f"高fee侵蚀风险 (参考: rb/hc=2.5x, l/pp=1.0x)")

    if warnings:
        return True, f"[PAIR_ECON] 警告: {'; '.join(warnings)}"
    return True, "[PAIR_ECON] fee/notional 比率正常"


def run_preflight(project_root: str,
                  use_legacy: bool,
                  strategy_config_path: 'str | None' = None,
                  factor_config_path: 'str | None' = None,
                  experiment: 'str | None' = None,
                  config_root: 'str | None' = None,
                  common_dir: 'str | None' = None,
                  verbose: bool = True) -> List[str]:
    """执行所有 preflight 检查。

    Args:
        project_root: cta_spread_arb 项目根
        use_legacy: True=走 legacy yaml; False=走 experiments/
        strategy_config_path, factor_config_path: legacy 模式
        experiment, config_root: v9 模式
        common_dir: basefiles 目录, 默认 <project_root>/../common/
        verbose: True 时打印每项结果

    Returns:
        失败原因列表; 空列表 = 全部通过
    """
    failures: List[str] = []
    results: List[str] = []

    # 1. WT 根配置 yaml
    for fn in WT_ROOT_YAMLS:
        ok, msg = _check_file_exists(os.path.join(project_root, fn),
                                     f"wt_yaml:{fn}")
        results.append(msg)
        if not ok:
            failures.append(msg)

    # 2. common/ basefiles
    if common_dir is None:
        common_dir = os.path.abspath(os.path.join(project_root, '..', 'common'))
    for fn in COMMON_BASEFILES:
        ok, msg = _check_json_valid(os.path.join(common_dir, fn),
                                    f"basefile:{fn}")
        results.append(msg)
        if not ok:
            failures.append(msg)

    # 3. storage/ 可写
    storage = os.path.join(project_root, 'storage')
    ok, msg = _check_dir_writable(storage, "storage_dir")
    results.append(msg)
    if not ok:
        failures.append(msg)

    # 4. logs/ 可写 (DiagLogger 输出根)
    logs = os.path.join(project_root, 'logs')
    ok, msg = _check_dir_writable(logs, "logs_dir")
    results.append(msg)
    if not ok:
        failures.append(msg)

    # 5. 策略配置
    if use_legacy:
        for label, path in [('strategy_config', strategy_config_path),
                            ('factor_config', factor_config_path)]:
            if not path:
                msg = f"[MISSING_ARG] {label}: 未提供路径"
                results.append(msg)
                failures.append(msg)
                continue
            ok, msg = _check_file_exists(path, f"strategy:{label}")
            results.append(msg)
            if not ok:
                failures.append(msg)
    else:
        # v9 experiments
        if not config_root:
            config_root = os.path.join(project_root, 'config')
        exp_path = os.path.join(config_root, 'experiments', f'{experiment}.yaml')
        ok, msg = _check_file_exists(exp_path, f"experiment:{experiment}")
        results.append(msg)
        if not ok:
            failures.append(msg)
        # base/ 五份基础配置 (signal/risk/exec/regime/factors)
        for fn in ['signal.yaml', 'risk.yaml', 'execution.yaml',
                   'regime.yaml', 'factors.yaml']:
            ok, msg = _check_file_exists(
                os.path.join(config_root, 'base', fn),
                f"base:{fn}")
            results.append(msg)
            if not ok:
                failures.append(msg)

        # 6. Pair economics check: 验证品种对 fee/notional 比率 (PairScreener 轻量集成)
        if experiment:
            pair_ok, pair_msg = _check_pair_economics(
                config_root, experiment)
            results.append(pair_msg)
            if not pair_ok:
                failures.append(pair_msg)

    if verbose:
        print("=" * 60)
        print("PREFLIGHT CHECK — cta_spread_arb runLive startup")
        print("=" * 60)
        for r in results:
            print(r)
        print("=" * 60)
        if failures:
            print(f"FAILED: {len(failures)} preflight check(s)")
        else:
            print(f"PASSED: {len(results)} check(s) ok")
        print("=" * 60)

    return failures


def run_or_exit(project_root: str, **kwargs) -> None:
    """便捷入口: 检查失败时 sys.exit(2)。"""
    failures = run_preflight(project_root, **kwargs)
    if failures:
        print("\n[FATAL] preflight failed, aborting startup:", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        sys.exit(2)
