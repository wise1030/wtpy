"""
ConfigResolver — v9 分层配置解析器

职责:
  从 config/experiments/<name>.yaml 出发, 按 4 级合并装配出一份与 v8.1
  ConfigManager 接口 100% 兼容的"解析后配置"。

合并层级 (优先级低→高):
  Layer 1: base/         全局默认 (signal/risk/regime/execution/factors)
  Layer 2: contracts/    合约模板 (legs_template + spread_mode + type)
  Layer 3: profiles/     参数覆盖包 (trending_contract / mr_contract / ...)
  Layer 4: experiments/  实例级 overrides + auto_profile 全局开关

输出:
  ResolvedConfig 对象, 暴露与 ConfigManager 等价的属性:
    strategies / signal_cfg / risk_cfg / regime_cfg / exec_cfg
    factor_cfg / enabled_factors / signal_aggregation
    + auto_profile 全局段 (param_profile_auto 字段, 兼容 v8.1)

设计原则:
  - 不在此处做 auto_profile 判定 (那是 helpers/auto_profile.py 的职责)
  - 不在此处做 leg 校验/合约月份扩展, 在 _expand_instance 中处理
  - 严格 deep merge: dict 递归合并, list/标量直接覆盖
  - 校验由 ConfigManager._validate 复用 (保持 v8.1 校验规则)
"""
import os
import copy
import yaml
from typing import Dict, List, Optional, Any
from schemas import SignalConfig, RiskConfig, RegimeConfig, ExecConfig, ArbCoreConfig


# ============================================================
# 工具: deep merge
# ============================================================
def deep_merge(base: dict, overlay: dict) -> dict:
    """递归合并 overlay 到 base 的副本上, 返回新 dict.
    
    规则:
      - 两边都是 dict → 递归
      - overlay 是 None → 跳过 (保留 base)
      - 其他 → overlay 完全覆盖 base (含 list)
    """
    if not overlay:
        return copy.deepcopy(base)
    out = copy.deepcopy(base)
    for k, v in overlay.items():
        if v is None:
            continue
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# ============================================================
# ResolvedConfig: 与 ConfigManager 接口兼容的解析产物
# ============================================================
class ResolvedConfig:
    """解析后的配置对象 — 接口对齐 ConfigManager"""

    def __init__(self,
                 experiment_name: str,
                 experiment_meta: dict,
                 strategies: list,
                 signal_dict: dict,
                 risk_dict: dict,
                 regime_dict: dict,
                 exec_dict: dict,
                 factor_cfg: dict,
                 auto_profile_global: dict,
                 param_profiles_compat: dict = None):
        self.experiment_name = experiment_name
        self.experiment_meta = experiment_meta  # backtest 段 + description 等

        # === 与 ConfigManager 对齐的属性 ===
        self._strategies = strategies  # 已展开后的 sub 列表 (含 legs 实例化)
        self._signal_cfg_obj = SignalConfig.from_yaml(signal_dict)
        self._risk_cfg_obj = RiskConfig.from_yaml(risk_dict)
        self._regime_cfg_obj = RegimeConfig.from_yaml(regime_dict)
        self._exec_cfg_obj = ExecConfig.from_yaml(exec_dict)
        self._factor_cfg = factor_cfg or {}
        # auto_profile 全局段, 名字保持 v8.1 兼容 (helpers/auto_profile.py 读它)
        self._auto_profile_global = auto_profile_global or {}
        # v8.1 兼容: param_profiles 段 (供 SpreadArbStrategy._resolve_sub_cfg 用)
        self._param_profiles_compat = param_profiles_compat or {}

        # === 原始 dict, 给 ConfigManager._validate / 调试用 ===
        self._strat_cfg_raw = {
            'signal': signal_dict,
            'risk_control': risk_dict,
            'regime': regime_dict,
            'execution': exec_dict,
            'strategies': strategies,
            'param_profile_auto': auto_profile_global,
        }

    # === 接口对齐 ConfigManager ===
    @property
    def strategies(self) -> list:
        return self._strategies

    @property
    def signal_cfg(self) -> SignalConfig:
        return self._signal_cfg_obj

    @property
    def risk_cfg(self) -> RiskConfig:
        return self._risk_cfg_obj

    @property
    def regime_cfg(self) -> RegimeConfig:
        return self._regime_cfg_obj

    @property
    def adaptive_cfg(self) -> RegimeConfig:
        """兼容旧接口"""
        return self._regime_cfg_obj

    @property
    def exec_cfg(self) -> ExecConfig:
        return self._exec_cfg_obj

    @property
    def factor_cfg(self) -> dict:
        return self._factor_cfg

    @property
    def enabled_factors(self) -> list:
        return [f for f in self._factor_cfg.get('factors', []) if f.get('enabled', False)]

    @property
    def signal_aggregation(self) -> dict:
        return self._factor_cfg.get('signal_aggregation', {})

    @property
    def auto_profile_cfg(self) -> dict:
        """v9 新增 — auto_profile 全局开关与阈值"""
        return self._auto_profile_global

    @property
    def param_profiles(self) -> dict:
        """v8.1 兼容 — param_profiles 段 (供 SpreadArbStrategy._resolve_sub_cfg)"""
        return self._param_profiles_compat

    def get_risk_param(self, key: str, default=None):
        return getattr(self._risk_cfg_obj, key, default)

    def get_sub_config(self, sub_name: str) -> Optional[dict]:
        for sub in self._strategies:
            if sub['name'] == sub_name:
                return sub
        return None

    # === v9 新增: 实验元信息 ===
    @property
    def backtest_meta(self) -> dict:
        """回测段 (start/end/capital/slippage)"""
        return self.experiment_meta.get('backtest', {})


# ============================================================
# ConfigResolver: 主入口
# ============================================================
class ConfigResolver:
    """4 级配置合并解析器"""

    def __init__(self, config_root: str):
        """
        config_root: 指向 config/ 目录的绝对路径
        """
        self.root = os.path.abspath(config_root)
        if not os.path.isdir(self.root):
            raise FileNotFoundError(f"config root not found: {self.root}")

        # 校验子目录存在
        for sub in ('base', 'contracts', 'profiles', 'experiments'):
            p = os.path.join(self.root, sub)
            if not os.path.isdir(p):
                raise FileNotFoundError(f"missing config subdir: {p}")

    # ---------- 内部工具 ----------
    @staticmethod
    def _load_yaml(path: str) -> dict:
        if not os.path.exists(path):
            return {}
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.full_load(f) or {}

    def _path(self, *parts) -> str:
        return os.path.join(self.root, *parts)

    # ---------- Layer 1: base ----------
    def _load_base(self) -> Dict[str, dict]:
        return {
            'signal':     self._load_yaml(self._path('base', 'signal.yaml')).get('signal', {}),
            'risk_control': self._load_yaml(self._path('base', 'risk.yaml')).get('risk_control', {}),
            'regime':     self._load_yaml(self._path('base', 'regime.yaml')).get('regime', {}),
            'execution':  self._load_yaml(self._path('base', 'execution.yaml')).get('execution', {}),
            'factors':    self._load_yaml(self._path('base', 'factors.yaml')),
        }

    # ---------- Layer 2: contracts ----------
    def _load_contract(self, name: str) -> dict:
        path = self._path('contracts', f'{name}.yaml')
        cfg = self._load_yaml(path)
        if not cfg:
            raise FileNotFoundError(f"contract '{name}' not found at {path}")
        return cfg

    # ---------- Layer 3: profile ----------
    def _load_profile(self, name: str) -> dict:
        """加载 profile 文件, 返回 overrides 段"""
        path = self._path('profiles', f'{name}.yaml')
        cfg = self._load_yaml(path)
        if not cfg:
            raise FileNotFoundError(f"profile '{name}' not found at {path}")
        return cfg.get('overrides', {})

    # ---------- Layer 4: experiment ----------
    def _load_experiment(self, name: str) -> dict:
        path = self._path('experiments', f'{name}.yaml')
        cfg = self._load_yaml(path)
        if not cfg:
            raise FileNotFoundError(f"experiment '{name}' not found at {path}")
        return cfg

    # ---------- 合约模板渲染 ----------
    @staticmethod
    def _render_legs(contract_def: dict, month: str) -> list:
        """根据 contracts/<x>.yaml 的 legs_template + 合约月份, 生成 v8.1 兼容的 legs[]"""
        legs = []
        for tpl in contract_def.get('legs_template', []):
            stdcode_tpl = tpl.get('stdCode_template')
            if not stdcode_tpl:
                # fallback: 用 exchange + symbol 拼
                stdcode_tpl = "{exchange}.{symbol}.{month}"
            code = stdcode_tpl.format(
                exchange=tpl.get('exchange', ''),
                symbol=tpl.get('symbol', ''),
                month=month,
            )
            legs.append({
                'code': code,
                'lots': tpl.get('lots', 1),
                'multiplier': tpl.get('multiplier', 10),
                'ratio': tpl.get('ratio', 1),
            })
        return legs

    # ---------- 单 instance 展开为 v8.1 sub 结构 ----------
    def _expand_instance(self, inst: dict) -> dict:
        """把 experiments[].instances[i] 展开为 v8.1 strategies[i] 等价结构"""
        contract_name = inst.get('contract')
        if not contract_name:
            raise ValueError(f"instance '{inst.get('name')}' missing 'contract' field")
        contract_def = self._load_contract(contract_name)

        month = inst.get('contract_month')
        if not month:
            raise ValueError(f"instance '{inst.get('name')}' missing 'contract_month' field")
        month = str(month)  # 兼容 yaml 把 2510 解析成 int

        legs = self._render_legs(contract_def, month)

        sub = {
            'name': inst['name'],
            'legs': legs,
            'periods': inst.get('periods', {}),
            'signal_mode': inst.get('signal_mode', 'adaptive'),
            'spread_mode': contract_def.get('spread_mode', 'coint'),
            'type': contract_def.get('type', 'cross_commodity'),
        }
        # auto_profile 直传 (auto_profile.py 在 runtime 读)
        if 'auto_profile' in inst:
            sub['auto_profile'] = inst['auto_profile']
        # 显式 param_profile 优先于 auto
        if 'param_profile' in inst:
            sub['param_profile'] = inst['param_profile']
        return sub

    # ---------- 主入口 ----------
    def resolve(self, experiment: str = 'default') -> ResolvedConfig:
        """解析一份实验配置 → ResolvedConfig"""
        # === Layer 1: base ===
        base = self._load_base()

        # === Layer 4: experiment (含 instances + auto_profile 全局段 + backtest) ===
        exp = self._load_experiment(experiment)
        instances = exp.get('instances', [])
        if not instances:
            raise ValueError(f"experiment '{experiment}' has no instances")

        auto_profile_global = exp.get('auto_profile', {})

        # === instance 级展开 + profile 应用 ===
        # 注意: profile 的 signal overrides 只影响该 instance 的运行时参数,
        # 但 v8.1 把 param_profiles 放在全局, 由 ArbCore.__init__ 时按 name 查找应用.
        # 这里我们保留 v8.1 行为, 把 profiles 装回 strategies[] 兼容字段.
        param_profiles_compat = self._build_profiles_compat(auto_profile_global, instances)

        strategies = [self._expand_instance(inst) for inst in instances]

        # === 全局 signal/risk/regime/execution 不被 instance 覆盖, 保持 v8.1 行为 ===
        # (v8.1 也是: strategies[].signal_mode 只影响信号模式选择, 不修改 signal 段)
        # C-2(2026-07): experiment 级 signal/risk_control/regime 覆盖此前被静默忽略,
        # 改为显式报错 — "YAML写了不生效"是调参实验的隐形炸弹
        for section in ('signal', 'risk_control', 'regime'):
            if exp.get(section):
                raise ValueError(
                    f"experiment '{experiment}' 包含顶层 '{section}:' 段, 但该段会被"
                    f"静默忽略 (v8.1 行为: 全局段只来自 base/)。请改用以下方式之一:\n"
                    f"  1. 修改 config/base/{section}.yaml (影响所有实验)\n"
                    f"  2. instances[].signal_overrides / risk_overrides (单实例覆盖)\n"
                    f"  3. instances[].param_profile 引用 profiles/*.yaml")
        signal_dict = base['signal']
        risk_dict = base['risk_control']
        regime_dict = base['regime']
        # C3: experiment 级 execution overrides 支持(用于 router on/off 等开关).
        exec_dict = deep_merge(base['execution'], exp.get('execution', {}) or {})

        # === auto_profile 全局段写入 strategies 同级, 兼容 v8.1 ConfigManager 读取 ===
        # v8.1 helpers/auto_profile.py 读的是 strat_cfg['param_profile_auto'] 和
        # strat_cfg['param_profiles'], 这里通过 ResolvedConfig 暴露
        # (后续 P1-d 调整 helpers/auto_profile.py 改读 ResolvedConfig.auto_profile_cfg)

        return ResolvedConfig(
            experiment_name=experiment,
            experiment_meta=exp,
            strategies=strategies,
            signal_dict=signal_dict,
            risk_dict=risk_dict,
            regime_dict=regime_dict,
            exec_dict=exec_dict,
            factor_cfg=base['factors'],
            auto_profile_global=auto_profile_global,
            param_profiles_compat=param_profiles_compat,
        )

    def _build_profiles_compat(self, auto_global: dict, instances: list) -> Dict[str, dict]:
        """把 profiles/*.yaml 全部加载, 装成 v8.1 param_profiles 兼容结构.
        
        v8.1 期待: {profile_name: {signal: {...}, risk_control: {...}}}
        """
        # 收集所有要用到的 profile 名:
        # (a) instance 显式 param_profile
        # (b) auto_profile 全局开 → 把 trending_contract / mr_contract 全部预加载
        needed = set()
        for inst in instances:
            if 'param_profile' in inst:
                needed.add(inst['param_profile'])
        if auto_global.get('enable'):
            # auto 模式下两个候选 profile 都要预加载
            needed.add('trending_contract')
            needed.add('mr_contract')

        out = {}
        prof_dir = os.path.join(self.root, 'profiles')
        for name in needed:
            path = os.path.join(prof_dir, f'{name}.yaml')
            if not os.path.exists(path):
                continue  # 缺失不报错, 与 v8.1 行为一致
            cfg = self._load_yaml(path)
            out[name] = cfg.get('overrides', {})
        return out


# ============================================================
# 便捷函数
# ============================================================
def resolve_experiment(config_root: str, experiment: str = 'default') -> ResolvedConfig:
    """便捷入口: ConfigResolver(root).resolve(experiment)"""
    return ConfigResolver(config_root).resolve(experiment)
