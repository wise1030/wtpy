"""
SignalPipeline — 信号生成管道包

Step顺序:
1. RegimeDispatch — regime检测 + 信号分派
2. RegimeWeight — regime驱动的权重调整 + 趋势/MR确认
3. PersistFilter — 信号持续性过滤
4. EntryJudge — 入场判断(衰减+阈值+门控)
5. ExitJudge — regime驱动的出场策略

SignalContext在Steps间传递，最终转换为SignalResult
"""
from .context import SignalContext
from .regime_dispatch import RegimeDispatchStep
from .regime_weight import RegimeWeightStep
from .persist_filter import PersistFilterStep
from .entry_judge import EntryJudgeStep
from .exit_judge import ExitJudgeStep
from .pipeline import SignalPipeline

__all__ = [
    "SignalContext", "RegimeDispatchStep", "RegimeWeightStep",
    "PersistFilterStep", "EntryJudgeStep", "ExitJudgeStep",
    "SignalPipeline",
]