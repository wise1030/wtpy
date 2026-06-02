import yaml
import numpy as np
import importlib
import os
import inspect
import logging

logger = logging.getLogger(__name__)


def _auto_discover_factors():
    """自动扫描factors包中所有FactorBase子类"""
    factor_map = {}
    factors_dir = os.path.dirname(os.path.abspath(__file__))
    # 入口文件已将factors目录加入sys.path，无需再insert
    from FactorBase import FactorBase
    for fname in os.listdir(factors_dir):
        if fname.endswith('_factors.py') and not fname.startswith('_'):
            module_name = fname[:-3]
            try:
                mod = importlib.import_module(module_name)
                for attr_name in dir(mod):
                    attr = getattr(mod, attr_name)
                    if (inspect.isclass(attr)
                        and issubclass(attr, FactorBase)
                        and attr is not FactorBase
                        and not attr_name.startswith('_')):
                        factor_map[attr_name] = attr
            except Exception as e:
                logger.warning(f"Failed to import {module_name}: {e}")
    return factor_map


_FACTOR_MAP = _auto_discover_factors()

def get_factor_class(cls_name):
    return _FACTOR_MAP.get(cls_name)


# 模式权重倍率: {signal_mode: {direction_type: (weight_mult, flip)}}
# weight_mult: 权重放大/缩小倍率
# flip: 是否反转因子信号方向
#
# v3改造: adaptive模式不再"不调整"
# 原问题: adaptive模式下momentum和reversal权重都是1.0,
#   但实际MR因子权重0.85, 趋势0.15 → adaptive实际还是MR
# 改造: adaptive模式由FactorEngine.apply_regime_weights()根据regime动态调整
#   regime=trend → momentum权重↑, reversal权重↓
#   regime=mr → reversal权重↑, momentum权重↓
#   regime=range → 所有因子降权
# 固定模式(trend/mean_revert)保持不变
MODE_DIRECTION_MAP = {
    "trend": {
        "momentum": (1.5, False),   # 趋势模式: 动量因子放大权重, 不翻转(顺势)
        "reversal": (1.0, False),   # 趋势模式: 反转因子不翻转(spread偏高→正信号→做多顺势)
        "neutral":  (1.0, False),   # 中性因子不变
    },
    "mean_revert": {
        "momentum": (0.5, True),    # MR模式: 动量因子缩小权重, 翻转(spread上升→做空回归)
        "reversal": (1.5, True),    # MR模式: 反转因子放大权重, 翻转(spread偏高→做空回归)
        "neutral":  (1.0, False),
    },
    # P0-1修复: adaptive模式初始用range(保守)，后续由apply_regime_weights()动态调整
    "adaptive": {
        "momentum": (1.0, False),   # 初始保守，regime检测后动态覆盖
        "reversal": (1.0, False),
        "neutral":  (1.0, False),
    },
}

# adaptive模式regime驱动的权重调整配置
# P0-1修复: REGIME_WEIGHT_MAP不再硬编码，由FactorEngine.__init__从YAML配置构建。
# YAML配置(strategy_config.yaml regime节)字段映射:
#   trend_momentum_weight → trend regime momentum multiplier
#   trend_reversal_weight → trend regime reversal multiplier
#   mr_momentum_weight → mr regime momentum multiplier (flip=True)
#   mr_reversal_weight → mr regime reversal multiplier (flip=True)
#   range_momentum_weight → range regime momentum multiplier
#   range_reversal_weight → range regime reversal multiplier
#   range_neutral_weight → range regime neutral multiplier
#
# 保留REGIME_WEIGHT_MAP作为默认值(配置缺失时回退)。
# FactorEngine._build_regime_weight_map()从配置构建实际使用的映射。
REGIME_WEIGHT_MAP = {
    "trend": {
        "momentum": (1.5, False),
        "reversal": (0.5, False),
        "neutral":  (1.0, False),
    },
    "mr": {
        "momentum": (0.5, True),
        "reversal": (1.5, True),
        "neutral":  (1.0, False),
    },
    "range": {
        "momentum": (0.3, False),
        "reversal": (0.3, False),
        "neutral":  (0.5, False),
    },
}


class FactorEngine:
    """
    因子引擎 v3 — regime驱动的自适应权重

    v3核心改动(相对v2):
    1. adaptive模式不再"不调整" — 根据regime动态调整因子权重
    2. 新增apply_regime_weights()方法 — 由SignalPipeline调用
    3. REGIME_WEIGHT_MAP替代adaptive条目 — regime=trend/mr/range各有权重配置
    4. 固定模式(trend/mean_revert)行为不变 — 兼容旧配置

    权重调整逻辑:
    - 固定模式(trend/mean_revert): 用MODE_DIRECTION_MAP, 初始化时确定
    - adaptive模式: 用REGIME_WEIGHT_MAP, 每次calculate前由外部调用apply_regime_weights()
    """

    def __init__(self, config_path: str, signal_cfg=None,
                 regime_cfg=None):
        self._factors = []
        self._base_weights = []      # 配置文件原始权重
        self._effective_weights = []  # 模式/regime调整后权重
        self._flip_flags = []         # 是否翻转
        self._current_regime = 'range'  # 当前regime缓存
        self._cfg = self._load_yaml(config_path)
        # REG-1修复: signal_cfg可能是SignalConfig dataclass或dict，统一处理
        # 优先从signal_cfg(strategy_config)读取, 回退到factor_config的signal_aggregation
        # v8.1: direction_threshold 已废除（dead 参数），不再读取
        if signal_cfg is not None:
            if hasattr(signal_cfg, 'entry_threshold'):
                # SignalConfig dataclass — 属性访问
                self._entry_threshold = signal_cfg.entry_threshold
                self._exit_threshold = signal_cfg.exit_threshold
                self._signal_mode = getattr(signal_cfg, 'signal_mode', 'adaptive')
            else:
                # dict — .get()访问
                self._entry_threshold = signal_cfg.get('entry_threshold', 0.3)
                self._exit_threshold = signal_cfg.get('exit_threshold', 0.05)
                self._signal_mode = signal_cfg.get('signal_mode', 'adaptive')
        else:
            agg = self._cfg.get('signal_aggregation', {})
            self._entry_threshold = agg.get('entry_threshold', 0.3)
            self._exit_threshold = agg.get('exit_threshold', 0.05)
            self._signal_mode = agg.get('signal_mode', 'adaptive')
        # P0-1修复: 从YAML配置构建regime权重映射，不再使用硬编码REGIME_WEIGHT_MAP
        self._regime_weight_map = self._build_regime_weight_map(regime_cfg)
        self._init_factors()
        self._apply_mode_direction()

    def _load_yaml(self, config_path: str) -> dict:
        with open(config_path, 'r') as f:
            return yaml.full_load(f)

    def _init_factors(self):
        for item in self._cfg.get('factors', []):
            if not item.get('enabled', False):
                continue
            cls_name = item['class']
            params = item.get('params', {})
            weight = item.get('weight', 0)
            if weight == 0:
                continue
            cls = get_factor_class(cls_name)
            if cls is None:
                logger.warning(f"Factor class '{cls_name}' not found, skipping")
                continue
            factor = cls(item['name'], params)
            self._factors.append(factor)
            self._base_weights.append(weight)

    def _build_regime_weight_map(self, regime_cfg) -> dict:
        """P0-1修复 + 趋势逻辑扩展: 从YAML配置构建regime权重映射

        REG-1修复: regime_cfg可能是RegimeConfig dataclass或dict，统一处理

        YAML字段(strategy_config.yaml regime节):
          权重乘数:
            trend_momentum_weight, trend_reversal_weight,
            mr_momentum_weight, mr_reversal_weight,
            range_momentum_weight, range_reversal_weight, range_neutral_weight
          翻转标志(新增):
            trend_momentum_flip, trend_reversal_flip,
            mr_momentum_flip, mr_reversal_flip,
            range_momentum_flip, range_reversal_flip, range_neutral_flip

        翻转规则(flip)现在可通过YAML配置:
        - 默认: trend不翻转, mr翻转momentum+reversal, range不翻转
        - 趋势逻辑模式: trend_reversal_flip=True → reversal因子顺势翻转

        Args:
            regime_cfg: RegimeConfig dataclass或dict
        Returns:
            dict: {regime: {direction_type: (mult, flip)}}"""
        if not regime_cfg:
            return REGIME_WEIGHT_MAP  # 回退到硬编码默认值

        # REG-1修复: 统一dataclass和dict访问
        def _get(cfg, key, default):
            if hasattr(cfg, key):
                return getattr(cfg, key, default)
            elif isinstance(cfg, dict):
                return cfg.get(key, default)
            return default

        return {
            "trend": {
                "momentum": (_get(regime_cfg, 'trend_momentum_weight', 1.5),
                             _get(regime_cfg, 'trend_momentum_flip', False)),
                "reversal": (_get(regime_cfg, 'trend_reversal_weight', 0.5),
                             _get(regime_cfg, 'trend_reversal_flip', False)),
                "neutral":  (1.0, False),
            },
            "mr": {
                "momentum": (_get(regime_cfg, 'mr_momentum_weight', 0.5),
                             _get(regime_cfg, 'mr_momentum_flip', True)),
                "reversal": (_get(regime_cfg, 'mr_reversal_weight', 1.5),
                             _get(regime_cfg, 'mr_reversal_flip', True)),
                "neutral":  (1.0, False),
            },
            "range": {
                "momentum": (_get(regime_cfg, 'range_momentum_weight', 0.3),
                             _get(regime_cfg, 'range_momentum_flip', False)),
                "reversal": (_get(regime_cfg, 'range_reversal_weight', 0.3),
                             _get(regime_cfg, 'range_reversal_flip', False)),
                "neutral":  (_get(regime_cfg, 'range_neutral_weight', 0.5), False),
            },
        }

    def _apply_mode_direction(self):
        """根据signal_mode和因子direction_type计算有效权重和翻转标志
        
        固定模式(trend/mean_revert): 用MODE_DIRECTION_MAP
        adaptive模式: 初始用range(保守), 后续由apply_regime_weights()动态调整
        """
        if self._signal_mode == 'adaptive':
            # P0-1修复: 使用实例变量而非硬编码REGIME_WEIGHT_MAP
            mode_cfg = self._regime_weight_map.get('range', {})
        else:
            mode_cfg = MODE_DIRECTION_MAP.get(self._signal_mode, MODE_DIRECTION_MAP["trend"])
        self._effective_weights = []
        self._flip_flags = []
        for factor, base_w in zip(self._factors, self._base_weights):
            dt = factor.direction_type
            mult, flip = mode_cfg.get(dt, (1.0, False))
            self._effective_weights.append(base_w * mult)
            self._flip_flags.append(flip)

    def apply_regime_weights(self, regime: str):
        """根据regime动态调整因子权重(仅adaptive模式有效)
        
        由ArbCore在每次process前调用。
        根据当前regime(trend/mr/range)调整:
        - momentum因子: trend↑, mr↓, range↓↓
        - reversal因子: trend↓, mr↑, range↓↓
        - neutral因子: 不变或微调
        
        Args:
            regime: "trend" / "mr" / "range"
        """
        if self._signal_mode != 'adaptive':
            return  # 固定模式不调整
        self._current_regime = regime
        # P0-1修复: 使用实例变量而非硬编码REGIME_WEIGHT_MAP
        regime_cfg = self._regime_weight_map.get(regime, self._regime_weight_map['range'])
        self._effective_weights = []
        self._flip_flags = []
        for factor, base_w in zip(self._factors, self._base_weights):
            dt = factor.direction_type
            mult, flip = regime_cfg.get(dt, (1.0, False))
            self._effective_weights.append(base_w * mult)
            self._flip_flags.append(flip)

    def refit_coint(self, spread: np.ndarray, prices: dict = None,
                    volumes: dict = None, aux_bars: dict = None,
                    **kwargs):
        """仅触发协整refit，不计算其他因子(避免双重计算和_rank_window污染)"""
        for factor in self._factors:
            if hasattr(factor, 'refit'):
                try:
                    factor.refit(spread, prices=prices, volumes=volumes,
                                aux_bars=aux_bars, **kwargs)
                except Exception:
                    pass

    def calculate(self, spread: np.ndarray, prices: dict = None,
                  volumes: dict = None, aux_bars: dict = None,
                  **kwargs) -> dict:
        scores = {}
        failed_factors = []
        for factor in self._factors:
            try:
                val = factor.calculate(
                    spread, prices=prices, volumes=volumes,
                    aux_bars=aux_bars, **kwargs)
                scores[factor.name] = max(-1.0, min(1.0, float(val)))
            except Exception as e:
                # 异常因子标记为None，不参与加权(避免0.0拉低总信号)
                scores[factor.name] = None
                failed_factors.append(factor.name)

        # 加权求和: 用有效权重 + 方向翻转
        # 异常因子(None)跳过，不参与加权
        total_score = 0.0
        active_weight = 0.0
        for i, (factor, eff_w, flip) in enumerate(
                zip(self._factors, self._effective_weights, self._flip_flags)):
            if factor.name not in scores:
                continue
            s = scores[factor.name]
            if s is None:
                continue  # 跳过异常因子
            if flip:
                s = -s
            total_score += s * eff_w
            active_weight += abs(eff_w)  # 用abs确保权重正确归一化

        if active_weight > 0:
            total_score /= active_weight
        total_score = max(-1.0, min(1.0, total_score))

        # v8.1: direction 简化为 sign(total_score)。
        # 历史 direction_threshold 参数已废除——下游 ctx.factor_direction 实际从未被
        # 任何 SignalPipeline 步骤读取（仅 ArbCore DiagLogger 用 signal.direction，
        # 那是 SignalGen 输出的 sign(entry_signal)，与本字段无关）。
        # 保留 direction 字段仅为 FactorOutput schema 完整性。
        direction = 1 if total_score > 0 else (-1 if total_score < 0 else 0)

        # 协整p值(通过正规接口获取，不直接访问私有属性)
        coint_p = 1.0
        for factor in self._factors:
            if hasattr(factor, 'get_coint_p'):
                coint_p = factor.get_coint_p()
                break

        # 异常因子在输出中替换为0.0(供外部读取)，但已不参与加权
        output_scores = {k: (v if v is not None else 0.0) for k, v in scores.items()}

        # CODE-1修复: 移除try/except ImportError，schemas.py在同目录下始终可用
        from schemas import FactorOutput
        fo = FactorOutput(
            total_score=float(total_score),
            factor_scores=output_scores,
            direction=direction,
            coint_p=float(coint_p),
            signal_mode=self._signal_mode,
            failed_factors=failed_factors,
            regime=self._current_regime,
            factor_direction_types={f.name: f.direction_type for f in self._factors},
        )
        # P2-1修复: 直接返回FactorOutput对象，不再调用to_dict()
        return fo

    def get_enabled_factor_names(self) -> list:
        return [f.name for f in self._factors]

    def reset_refit(self):
        for factor in self._factors:
            if hasattr(factor, 'reset_refit'):
                factor.reset_refit()

    def get_coint_params(self) -> tuple:
        for factor in self._factors:
            if hasattr(factor, 'get_coint_params'):
                return factor.get_coint_params()
        return (None, None, 1.0)

    @property
    def entry_threshold(self) -> float:
        return self._entry_threshold

    @property
    def exit_threshold(self) -> float:
        return self._exit_threshold

    @property
    def signal_mode(self) -> str:
        return self._signal_mode

    def set_signal_mode(self, mode: str):
        """运行时切换信号模式(如adaptive动态切换)"""
        if mode not in MODE_DIRECTION_MAP:
            logger.warning(f"Unknown signal_mode '{mode}', keeping '{self._signal_mode}'")
            return
        self._signal_mode = mode
        self._apply_mode_direction()
