"""
ConfigManager — 统一配置管理

职责:
1. 加载和合并strategy_config.yaml + factor_config.yaml
2. 配置校验(必填字段、值域检查)
3. 提供结构化访问接口(返回SignalConfig/RiskConfig/RegimeConfig)
4. 支持运行时参数更新(如signal_mode切换)

优化: 返回结构化对象而非raw dict，消除下游isinstance(dict)兼容代码
"""
import yaml
import os
from schemas import SignalConfig, RiskConfig, RegimeConfig, ExecConfig, ArbCoreConfig


class ConfigManager:
    """统一配置管理器"""

    # 必填字段校验规则
    REQUIRED_STRATEGY_FIELDS = {
        'name': str,
        'legs': list,
        'periods': dict,
    }
    REQUIRED_LEG_FIELDS = {
        'code': str,
        'lots': (int, float),
    }

    def __init__(self, strategy_config_path: str, factor_config_path: str = None):
        self._strat_cfg = self._load_yaml(strategy_config_path)
        self._factor_cfg = None
        if factor_config_path and os.path.exists(factor_config_path):
            self._factor_cfg = self._load_yaml(factor_config_path)
        self._validate()

        # 结构化配置对象(一次性构建，消除下游isinstance检查)
        self._signal_cfg_obj = SignalConfig.from_yaml(self._strat_cfg.get('signal', {}))
        self._risk_cfg_obj = RiskConfig.from_yaml(self._strat_cfg.get('risk_control', {}))
        self._regime_cfg_obj = RegimeConfig.from_yaml(
            self._strat_cfg.get('regime', self._strat_cfg.get('adaptive', {})))
        # P1-2: 执行参数配置化
        self._exec_cfg_obj = ExecConfig.from_yaml(self._strat_cfg.get('execution', {}))

    @staticmethod
    def _load_yaml(path: str) -> dict:
        with open(path, 'r') as f:
            return yaml.full_load(f) or {}

    # 值域校验规则: {field: (min, max) or None}
    VALUE_RANGE_RULES = {
        'entry_threshold': (0.1, 1.0),
        'exit_threshold': (0.01, 0.5),
        'short_entry_multiplier': (0.5, 1.0),
        'signal_persist_bars': (1, 10),
        'max_hold_bars': (5, 500),
        'min_hold_bars': (1, 120),
        'cooldown_bars': (1, 120),
        'max_loss_per_pair': (100, 50000),
        'profit_take_threshold': (500, 10000),
        'profit_drawdown_ratio': (0.1, 0.9),
        'max_spread_deviation': (1.0, 10.0),
        'lots': (1, 100),
        'hurst_window': (10, 200),
        'adx_window': (5, 100),
        'vol_window': (10, 200),
        'regime_memory_bars': (1, 60),
    }

    def _validate(self):
        """校验配置完整性 + 值域范围"""
        for sub in self._strat_cfg.get('strategies', []):
            for field, ftype in self.REQUIRED_STRATEGY_FIELDS.items():
                if field not in sub:
                    raise ValueError(f"Strategy '{sub.get('name', '?')}' missing field: {field}")
                if not isinstance(sub[field], ftype):
                    raise ValueError(f"Strategy '{sub['name']}.{field}' must be {ftype}")
            for leg in sub['legs']:
                for field, ftype in self.REQUIRED_LEG_FIELDS.items():
                    if field not in leg:
                        raise ValueError(f"Strategy '{sub['name']}' leg missing field: {field}")
                # 值域校验: lots
                self._check_range(f"Strategy '{sub['name']}' leg.lots", leg.get('lots'), (1, 100))

        # 信号参数值域校验
        signal = self._strat_cfg.get('signal', {})
        for key, rng in self.VALUE_RANGE_RULES.items():
            if key in signal:
                self._check_range(f"signal.{key}", signal[key], rng)

        # 风控参数值域校验
        risk = self._strat_cfg.get('risk_control', {})
        for key, rng in self.VALUE_RANGE_RULES.items():
            if key in risk:
                self._check_range(f"risk_control.{key}", risk[key], rng)

        # Regime参数值域校验
        regime = self._strat_cfg.get('regime', {})
        for key, rng in self.VALUE_RANGE_RULES.items():
            if key in regime:
                self._check_range(f"regime.{key}", regime[key], rng)

    def _check_range(self, label: str, value, rng):
        """值域校验辅助"""
        if rng is None or value is None:
            return
        lo, hi = rng
        if not (lo <= value <= hi):
            raise ValueError(f"{label}={value} out of range [{lo}, {hi}]")

    # === 策略配置访问 ===

    @property
    def strategies(self) -> list:
        return self._strat_cfg.get('strategies', [])

    @property
    def signal_cfg(self) -> SignalConfig:
        """信号参数配置(结构化对象)"""
        return self._signal_cfg_obj

    @property
    def risk_cfg(self) -> RiskConfig:
        """风控参数配置(结构化对象)"""
        return self._risk_cfg_obj

    @property
    def regime_cfg(self) -> RegimeConfig:
        """Regime配置(结构化对象)"""
        return self._regime_cfg_obj

    @property
    def adaptive_cfg(self) -> RegimeConfig:
        """兼容旧接口: 返回regime配置"""
        return self._regime_cfg_obj

    @property
    def exec_cfg(self) -> ExecConfig:
        """执行参数配置(结构化对象) — P1-2"""
        return self._exec_cfg_obj

    # === 因子配置访问 ===

    @property
    def factor_cfg(self) -> dict:
        return self._factor_cfg or {}

    @property
    def enabled_factors(self) -> list:
        return [f for f in self.factor_cfg.get('factors', []) if f.get('enabled', False)]

    @property
    def signal_aggregation(self) -> dict:
        return self.factor_cfg.get('signal_aggregation', {})

    # === 便捷方法 ===

    def get_risk_param(self, key: str, default=None):
        """获取风控参数(从RiskConfig属性)"""
        return getattr(self._risk_cfg_obj, key, default)

    def get_sub_config(self, sub_name: str) -> dict:
        """按名称获取子策略配置"""
        for sub in self.strategies:
            if sub['name'] == sub_name:
                return sub
        return None