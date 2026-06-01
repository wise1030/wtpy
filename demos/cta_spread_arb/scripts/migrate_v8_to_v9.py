#!/usr/bin/env python3
"""
migrate_v8_to_v9.py — 把 v8.1 单文件配置拆成 v9 分层结构

用法:
    python scripts/migrate_v8_to_v9.py \\
        --strategy config/strategy_config.yaml \\
        --factor config/factor_config.yaml \\
        --out config/

输出:
    config/base/{signal,risk,regime,execution,factors}.yaml
    config/contracts/<auto-named>.yaml (从 strategies[0].legs 推断)
    config/profiles/<name>.yaml (从顶层 param_profiles 拆出)
    config/experiments/migrated.yaml (实例 + auto_profile 段 + backtest)

注意: 此脚本是一次性迁移辅助, 已迁移过的项目无需再跑。
仅做 mechanical 拆分, 不修改任何参数值。
"""
import argparse
import os
import sys
import yaml


def load_yaml(path):
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.full_load(f) or {}


def dump_yaml(path, data, header=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        if header:
            f.write(f"# {header}\n")
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, default_flow_style=False)
    print(f"  wrote: {path}")


def infer_contract_name(legs):
    """从 legs 推断合约名: rb+hc → rb_hc"""
    products = []
    for leg in legs:
        code = leg['code']  # e.g. SHFE.rb.2510
        parts = code.split('.')
        if len(parts) >= 2:
            products.append(parts[1])
    return '_'.join(products) if products else 'unnamed'


def split_code(code):
    """SHFE.rb.2510 → (SHFE, rb, 2510)"""
    parts = code.split('.')
    if len(parts) != 3:
        return ('', '', '')
    return tuple(parts)


def migrate(strat_path, factor_path, out_dir):
    strat = load_yaml(strat_path)
    factor = load_yaml(factor_path) if factor_path and os.path.exists(factor_path) else {}

    # === Layer 1: base/ ===
    base_dir = os.path.join(out_dir, 'base')
    dump_yaml(os.path.join(base_dir, 'signal.yaml'),
              {'signal': strat.get('signal', {})}, header='v9 base — 信号默认参数')
    dump_yaml(os.path.join(base_dir, 'risk.yaml'),
              {'risk_control': strat.get('risk_control', {})}, header='v9 base — 风控默认参数')
    dump_yaml(os.path.join(base_dir, 'regime.yaml'),
              {'regime': strat.get('regime', strat.get('adaptive', {}))},
              header='v9 base — Regime 检测默认参数')
    dump_yaml(os.path.join(base_dir, 'execution.yaml'),
              {'execution': strat.get('execution', {})}, header='v9 base — 执行默认参数')
    if factor:
        dump_yaml(os.path.join(base_dir, 'factors.yaml'), factor,
                  header='v9 base — 因子定义与聚合配置')

    # === Layer 2: contracts/ ===
    contracts_seen = {}  # name → {legs_template, spread_mode, type}
    for sub in strat.get('strategies', []):
        legs = sub.get('legs', [])
        if not legs:
            continue
        cname = infer_contract_name(legs)
        if cname in contracts_seen:
            continue
        legs_tpl = []
        for leg in legs:
            ex, sym, mon = split_code(leg['code'])
            legs_tpl.append({
                'exchange': ex,
                'symbol': sym,
                'stdCode_template': f'{ex}.{sym}.{{month}}',
                'lots': leg.get('lots', 1),
                'multiplier': leg.get('multiplier', 10),
                'ratio': leg.get('ratio', 1),
            })
        contracts_seen[cname] = {
            'name': cname,
            'spread_mode': sub.get('spread_mode', 'coint'),
            'type': sub.get('type', 'cross_commodity'),
            'legs_template': legs_tpl,
        }
    for cname, cdef in contracts_seen.items():
        dump_yaml(os.path.join(out_dir, 'contracts', f'{cname}.yaml'), cdef,
                  header=f'v9 contract — {cname}')

    # === Layer 3: profiles/ ===
    profiles = strat.get('param_profiles', {}) or {}
    for pname, pcfg in profiles.items():
        dump_yaml(os.path.join(out_dir, 'profiles', f'{pname}.yaml'),
                  {'overrides': pcfg}, header=f'v9 profile — {pname}')

    # === Layer 4: experiments/migrated.yaml ===
    instances = []
    for sub in strat.get('strategies', []):
        legs = sub.get('legs', [])
        if not legs:
            continue
        cname = infer_contract_name(legs)
        _, _, month = split_code(legs[0]['code'])
        inst = {
            'name': sub['name'],
            'contract': cname,
            'contract_month': month,
            'periods': sub.get('periods', {}),
            'signal_mode': sub.get('signal_mode', 'adaptive'),
        }
        if 'auto_profile' in sub:
            inst['auto_profile'] = sub['auto_profile']
        if 'param_profile' in sub:
            inst['param_profile'] = sub['param_profile']
        instances.append(inst)

    exp = {
        'description': 'Migrated from v8.1 strategy_config.yaml',
        'backtest': {
            'start': 202502030900,
            'end': 202512311500,
            'capital': 500000,
            'slippage': 0,
        },
        'auto_profile': strat.get('param_profile_auto', {}),
        'instances': instances,
    }
    dump_yaml(os.path.join(out_dir, 'experiments', 'migrated.yaml'), exp,
              header='v9 experiment — migrated from v8.1')

    print("\n=== Migration complete ===")
    print(f"  base/: signal/risk/regime/execution/factors")
    print(f"  contracts/: {list(contracts_seen.keys())}")
    print(f"  profiles/: {list(profiles.keys())}")
    print(f"  experiments/migrated.yaml: {len(instances)} instances")
    print("\nVerify by:  python runBT.py --experiment migrated")


def main():
    p = argparse.ArgumentParser(description='Migrate v8.1 single-file config to v9 layered config')
    p.add_argument('--strategy', required=True, help='v8.1 strategy_config.yaml path')
    p.add_argument('--factor', default=None, help='v8.1 factor_config.yaml path')
    p.add_argument('--out', required=True, help='v9 config/ output directory')
    args = p.parse_args()

    if not os.path.exists(args.strategy):
        print(f"ERROR: strategy config not found: {args.strategy}", file=sys.stderr)
        sys.exit(1)
    migrate(args.strategy, args.factor, args.out)


if __name__ == '__main__':
    main()
