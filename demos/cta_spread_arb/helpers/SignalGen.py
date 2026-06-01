"""
SignalGen — 信号生成器(v4)

v4: regime检测从Pipeline中解耦，由SubStrategy前置调用。
Pipeline只负责信号处理(权重调整+持续性+出场)，不再检测regime。
SignalGen新增detect_regime()和generate_with_regime()方法。

注意: 入口文件已统一设置sys.path，子模块无需再自行sys.path.insert。
"""
import numpy as np
from schemas import SignalResult
from SignalPipeline import SignalPipeline
from utils import simple_regime


class SignalGen:
    """信号生成器: 封装RegimeDetector + SignalPipeline"""

    def __init__(self, pipeline: SignalPipeline, regime_detector=None):
        self._pipeline = pipeline
        self._regime_detector = regime_detector
        self._last_regime = 'range'
        self._last_regime_direction = 0
        self._last_regime_confidence = 0.0

    @classmethod
    def from_config(cls, signal_mode, entry_threshold, exit_threshold,
                    adaptive_cfg, signal_cfg,
                    regime_detector=None, factor_engine=None):
        """从配置创建SignalGen(内部创建Pipeline)

        factor_engine参数保留但不再传给Pipeline(P0-2/ARCH-5: Pipeline不再需要)。
        """
        pipeline = SignalPipeline.from_config(
            signal_mode, entry_threshold, exit_threshold,
            adaptive_cfg, signal_cfg)
        return cls(pipeline, regime_detector=regime_detector)

    @property
    def regime(self) -> str:
        return self._last_regime

    @property
    def regime_direction(self) -> int:
        return self._last_regime_direction

    @property
    def regime_confidence(self) -> float:
        return self._last_regime_confidence

    def detect_regime(self, spread, aux_bars: dict = None,
                      coint_p: float = 1.0) -> dict:
        """前置regime检测 — 在FactorEngine.calculate()之前调用

        Returns:
            dict with keys: regime, confidence, direction
        """
        if self._regime_detector is not None and spread is not None:
            result = self._regime_detector.detect(spread)
            regime_dict = result.to_dict()
        else:
            regime_dict = simple_regime(spread, coint_p)

        self._last_regime = regime_dict.get('regime', 'range')
        self._last_regime_direction = regime_dict.get('direction', 0)
        self._last_regime_confidence = regime_dict.get('confidence', 0.0)
        return regime_dict

    def generate(self, factor_signal: dict, aux_bars: dict,
                 current_direction: int = 0,
                 spread_trend_filter: int = 0,
                 spread=None,
                 coint_p: float = 1.0,
                 unrealized_pnl: float = 0.0) -> SignalResult:
        """生成信号(内部自动检测regime — 兼容旧接口)"""
        regime_dict = self.detect_regime(spread, aux_bars, coint_p)
        return self.generate_with_regime(
            factor_signal, aux_bars, current_direction,
            spread_trend_filter=spread_trend_filter,
            spread=spread, coint_p=coint_p,
            regime_dict=regime_dict,
            unrealized_pnl=unrealized_pnl)

    def generate_with_regime(self, factor_signal: dict, aux_bars: dict,
                             current_direction: int = 0,
                             spread_trend_filter: int = 0,
                             spread=None,
                             coint_p: float = 1.0,
                             regime_dict: dict = None,
                             unrealized_pnl: float = 0.0,
                             current_hour: int = 0) -> SignalResult:
        """生成信号(使用已检测的regime — 避免重复检测)"""
        if regime_dict is None:
            regime_dict = {'regime': self._last_regime,
                          'confidence': self._last_regime_confidence,
                          'direction': self._last_regime_direction}
        raw = self._pipeline.generate_with_regime(
            factor_signal, aux_bars, current_direction,
            spread_trend_filter=spread_trend_filter,
            spread=spread, coint_p=coint_p,
            regime_dict=regime_dict,
            unrealized_pnl=unrealized_pnl,
            current_hour=current_hour)
        # CODE-7修复: Pipeline始终返回SignalResult对象，移除dict兼容死代码
        return raw

    def reset_trailing(self):
        self._pipeline.reset_trailing()
