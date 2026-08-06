"""SignalPipeline — 信号生成管道(v4)

组合多个Step，顺序执行。

v4: regime检测由外部(ArbCore)前置完成，通过regime_dict传入。
RegimeDispatchStep不再内部调用RegimeDetector，直接使用传入的regime。
"""
import numpy as np
from .context import SignalContext
from .regime_dispatch import RegimeDispatchStep
from .regime_weight import RegimeWeightStep
from .persist_filter import PersistFilterStep
from .entry_judge import EntryJudgeStep
from .exit_judge import ExitJudgeStep


class SignalPipeline:
    """信号生成管道(v4) — 组合多个Step，顺序执行"""

    def __init__(self, steps: list):
        self._steps = steps

    def generate(self, factor_signal: dict, aux_bars: dict,
                 current_direction: int = 0,
                 spread_trend_filter: int = 0,
                 spread: np.ndarray = None,
                 coint_p: float = 1.0,
                 regime_dict: dict = None,
                 unrealized_pnl: float = 0.0,
                 current_hour: int = 0) -> dict:
        """生成信号 — regime_dict由外部传入(避免重复检测)"""
        ctx = SignalContext(
            factor_signal=factor_signal,
            aux_bars=aux_bars,
            current_direction=current_direction,
            spread_trend_filter=spread_trend_filter,
            spread=spread,
            coint_p=coint_p,
            unrealized_pnl=unrealized_pnl,
            current_hour=current_hour,
        )
        # 将外部regime结果注入context
        if regime_dict is not None:
            ctx.regime = regime_dict.get('regime', 'range')
            ctx.regime_confidence = regime_dict.get('confidence', 0.0)
            ctx.regime_direction = regime_dict.get('direction', 0)
        for step in self._steps:
            ctx = step.process(ctx)
        return ctx.to_signal_result()

    # 兼容旧接口
    def generate_with_regime(self, factor_signal: dict, aux_bars: dict,
                             current_direction: int = 0,
                             spread_trend_filter: int = 0,
                             spread: np.ndarray = None,
                             coint_p: float = 1.0,
                             regime_dict: dict = None,
                             unrealized_pnl: float = 0.0,
                             current_hour: int = 0) -> dict:
        """[DEPRECATED C-5] generate() 的逐参透传别名, 仅为兼容保留。
        新代码直接调 generate()。"""
        return self.generate(
            factor_signal, aux_bars, current_direction,
            spread_trend_filter=spread_trend_filter,
            spread=spread, coint_p=coint_p,
            regime_dict=regime_dict,
            unrealized_pnl=unrealized_pnl,
            current_hour=current_hour)

    # C-4(2026-07): 删除 regime/regime_direction properties —
    # 它们遍历 steps 读 RegimeDispatchStep 的镜像缓存(已删除),
    # regime 单一来源归 SignalGen(检测后缓存, generate_with_regime 兜底用)。

    def reset_trailing(self):
        for step in self._steps:
            if isinstance(step, (EntryJudgeStep, ExitJudgeStep)):
                if hasattr(step, 'reset_trailing'):
                    step.reset_trailing()
            if isinstance(step, PersistFilterStep):
                step.reset_history()

    def get_entry_threshold(self) -> float:
        """读取当前入场阈值(以EntryJudgeStep为准)"""
        for step in self._steps:
            if isinstance(step, EntryJudgeStep):
                return step._entry_threshold
        return 0.0

    def set_entry_threshold(self, value: float):
        """B11修复: 同步更新所有持有entry_threshold的step。

        auto_profile动态调整入口 — 此前 ArbCore._recheck_profile 直接
        给 SignalGen 赋普通属性, 既不传播到step, 首次读取还会 AttributeError。
        """
        for step in self._steps:
            if isinstance(step, (EntryJudgeStep, ExitJudgeStep, PersistFilterStep)):
                step._entry_threshold = value

    @staticmethod
    def from_config(signal_mode, entry_threshold, exit_threshold,
                    adaptive_cfg, signal_cfg):
        """从配置创建Pipeline

        REG-1修复: signal_cfg可能是SignalConfig dataclass或dict，统一处理
        A1: 移除未使用的 regime_detector/factor_engine 死参数。
        """
        # REG-1修复: 统一dataclass和dict访问
        def _cfg_get(cfg, key, default):
            if hasattr(cfg, key):
                return getattr(cfg, key, default)
            elif isinstance(cfg, dict):
                return cfg.get(key, default)
            return default

        persist_step = PersistFilterStep(
            signal_persist_bars=_cfg_get(signal_cfg, 'signal_persist_bars', 1),
            entry_threshold=entry_threshold,
        )
        steps = [
            RegimeDispatchStep(
                signal_mode=signal_mode,
            ),
            RegimeWeightStep(
                coint_decay=_cfg_get(signal_cfg, 'coint_decay', 0.7),
                momentum_decay=_cfg_get(signal_cfg, 'momentum_decay', 0.7),
            ),
            persist_step,
            EntryJudgeStep(
                entry_threshold=entry_threshold,
                short_entry_multiplier=_cfg_get(signal_cfg, 'short_entry_multiplier', 1.0),
                trend_counter_decay_factor=_cfg_get(signal_cfg, 'trend_counter_decay_factor', 0.5),
                coint_block_entry_p=_cfg_get(signal_cfg, 'coint_block_entry_p', 0.05),
                # v7.2: 时段过滤
                blocked_hours=_cfg_get(signal_cfg, 'blocked_hours', '14'),
            ),
            ExitJudgeStep(
                entry_threshold=entry_threshold,
                exit_threshold=exit_threshold,
                trailing_exit_bars=_cfg_get(signal_cfg, 'trailing_exit_bars', 6),
                trailing_decay_ratio=_cfg_get(signal_cfg, 'trailing_decay_ratio', 0.3),
                profit_protect_peak=_cfg_get(signal_cfg, 'profit_protect_peak', 0.7),
                profit_protect_floor=_cfg_get(signal_cfg, 'profit_protect_floor', 0.35),
                trend_trailing_ratio=_cfg_get(signal_cfg, 'trend_trailing_ratio', 0.5),
                trend_trailing_bars=_cfg_get(signal_cfg, 'trend_trailing_bars', 3),
                range_exit_threshold=_cfg_get(signal_cfg, 'range_exit_threshold', 0.4),
                short_entry_multiplier=_cfg_get(signal_cfg, 'short_entry_multiplier', 1.0),
                # B12修复: 反转额外缓冲倍数
                reverse_buffer_multiplier=_cfg_get(signal_cfg, 'reverse_buffer_multiplier', 1.2),
                # v7: 浮盈追踪止盈参数
                pnl_trailing_threshold=_cfg_get(signal_cfg, 'pnl_trailing_threshold', 500.0),
                pnl_trailing_tight=_cfg_get(signal_cfg, 'pnl_trailing_tight', 0.2),
                pnl_trailing_medium=_cfg_get(signal_cfg, 'pnl_trailing_medium', 0.3),
                pnl_trailing_wide=_cfg_get(signal_cfg, 'pnl_trailing_wide', 0.4),
                # C1修复: _mr_exit PnL 阈值参数化
                mr_profit_tier1=_cfg_get(signal_cfg, 'mr_profit_tier1', 300.0),
                mr_profit_tier2=_cfg_get(signal_cfg, 'mr_profit_tier2', 100.0),
                mr_loss_scale=_cfg_get(signal_cfg, 'mr_loss_scale', 4000.0),
                # C1修复: _pnl_trailing_check 分级边界参数化
                pnl_trailing_tier1=_cfg_get(signal_cfg, 'pnl_trailing_tier1', 1000.0),
                pnl_trailing_tier2=_cfg_get(signal_cfg, 'pnl_trailing_tier2', 2000.0),
                # B-3: 反转 veto 的时段过滤(与 EntryJudge 同配置)
                blocked_hours=_cfg_get(signal_cfg, 'blocked_hours', '14'),
            ),
        ]
        return SignalPipeline(steps)