"""
SignalPipeline — 兼容性代理 [DEPRECATED: 仅保留 import 兼容]

实际运行时引用: SignalGen.py / test_p1_signal_decisions.py
  → from SignalPipeline import SignalPipeline
新代码请直接 from pipeline import SignalPipeline / from pipeline.pipeline import SignalPipeline

P2-1: 原SignalPipeline.py(584行)已拆分为pipeline/包:
  - context.py: SignalContext
  - regime_dispatch.py: RegimeDispatchStep
  - regime_weight.py: RegimeWeightStep
  - persist_filter.py: PersistFilterStep
  - entry_judge.py: EntryJudgeStep
  - exit_judge.py: ExitJudgeStep
  - pipeline.py: SignalPipeline
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