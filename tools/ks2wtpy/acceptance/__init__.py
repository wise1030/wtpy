"""
ks2wtpy acceptance — P0 验收门禁。

子模块：
  - gates  : ManifestGate / QualityGate / ReadbackGate / PytestGate / PyCompileGate
  - report : build_acceptance_report() + write_acceptance_artifacts()
  - cli    : python -m acceptance --output <dir> ...

设计目标：把分散的产物（conversion_manifest.json / quality_report.json /
回归测试 / 编译 / WT loader 读回）汇成 *单一* acceptance_report，给出
overall_status，便于 CI 与人工双轨判定。

零依赖于外部网络；具体合约/HOT/_index 反例的 WT loader 读回走本地 wtpy 包。
"""

from . import gates  # noqa: F401
from .report import build_acceptance_report, write_acceptance_artifacts  # noqa: F401
