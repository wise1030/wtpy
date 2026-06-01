"""
SignalPipeline — 兼容性代理

P2-1: 原SignalPipeline.py(584行)已拆分为pipeline/包:
  - context.py: SignalContext
  - regime_dispatch.py: RegimeDispatchStep
  - regime_weight.py: RegimeWeightStep
  - persist_filter.py: PersistFilterStep
  - entry_judge.py: EntryJudgeStep
  - exit_judge.py: ExitJudgeStep
  - pipeline.py: SignalPipeline

本文件保留为兼容性入口，所有类从pipeline包导入。
"""
from pipeline import (
    SignalContext,
    RegimeDispatchStep,
    RegimeWeightStep,
    PersistFilterStep,
    EntryJudgeStep,
    ExitJudgeStep,
    SignalPipeline,
)

__all__ = [
    "SignalContext", "RegimeDispatchStep", "RegimeWeightStep",
    "PersistFilterStep", "EntryJudgeStep", "ExitJudgeStep",
    "SignalPipeline",
]