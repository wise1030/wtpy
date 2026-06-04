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
