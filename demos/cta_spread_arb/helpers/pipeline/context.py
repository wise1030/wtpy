"""SignalContext — 信号上下文，在Pipeline Steps间传递的可变状态"""
import numpy as np


class SignalContext:
    """信号上下文 — 在Pipeline Steps间传递的可变状态"""

    def __init__(self, factor_signal, aux_bars: dict,
                 current_direction: int = 0,
                 spread_trend_filter: int = 0,
                 spread: np.ndarray = None,
                 coint_p: float = 1.0,
                 unrealized_pnl: float = 0.0,
                 current_hour: int = 0):
        # 输入(不可变)
        # P2-1修复: factor_signal可以是FactorOutput dataclass或dict，统一处理
        self.factor_signal = factor_signal
        self.aux_bars = aux_bars
        self.current_direction = current_direction
        self.spread_trend_filter = spread_trend_filter
        self.spread = spread
        self.coint_p = coint_p
        self.unrealized_pnl = unrealized_pnl
        self.current_hour = current_hour  # v7.2: 时段过滤

        # 中间状态(Steps可修改) — 从factor_signal提取
        if hasattr(factor_signal, 'total_score'):
            # FactorOutput dataclass
            self.total_score = factor_signal.total_score
            self.factor_scores = factor_signal.factor_scores
            self.factor_direction = factor_signal.direction
            self.factor_direction_types = factor_signal.factor_direction_types
            self.coint_p = factor_signal.coint_p  # 从FactorOutput覆盖
        else:
            # dict (兼容旧接口)
            self.total_score = factor_signal.get('total_score', 0)
            self.factor_scores = factor_signal.get('factor_scores', {})
            self.factor_direction = factor_signal.get('direction', 0)
            self.factor_direction_types = factor_signal.get('factor_direction_types', {})
            # BUG修复(P1-6): dict 输入路径同样要从 factor_signal 覆盖 coint_p,
            # 与 FactorOutput 分支保持一致, 否则 ctx.coint_p 永远是默认 1.0,
            # 导致 EntryJudgeStep 把所有 dict-输入信号判为 coint_blocked。
            if 'coint_p' in factor_signal:
                self.coint_p = factor_signal['coint_p']
        self.entry_signal = 0.0
        self.final_signal = 0.0
        self.regime = 'range'
        self.regime_confidence = 0.0
        self.regime_direction = 0

        # 过滤标记
        self.coint_decay_val = 1.0
        self.trend_confirmed = True
        self.mr_confirmed = True
        self.signal_persistent = True
        self.trend_blocked = False
        self.strong_signal = False

        # 出场判断结果
        self.should_trade = False
        self.should_exit = False
        self.should_reverse = False
        self.pnl_trailing_exit = False  # v7: 浮盈追踪止盈标记

        # 追踪止损状态
        self.bars_since_peak = 0
        self.peak_signal = 0.0

    def to_signal_result(self) -> 'SignalResult':
        """将SignalContext转换为结构化SignalResult

        方向决策统一: direction = sign(final_signal)
        不再有major_direction/factor_direction分裂。
        """
        from schemas import SignalResult
        return SignalResult(
            direction=int(np.sign(self.final_signal)) if abs(self.final_signal) > 0.01 else 0,
            entry_signal=float(self.final_signal),
            raw_signal=float(self.entry_signal),
            strength=float(abs(self.total_score)),
            should_trade=self.should_trade,
            should_exit=self.should_exit,
            should_reverse=self.should_reverse,
            regime=self.regime,
            regime_confidence=self.regime_confidence,
            regime_direction=self.regime_direction,
            pnl_trailing_exit=self.pnl_trailing_exit,
        )