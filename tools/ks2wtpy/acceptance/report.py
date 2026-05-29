"""Acceptance report 聚合器。"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .gates import (
    GateResult, ManifestGate, QualityGate, ReadbackGate,
    PytestGate, PyCompileGate,
)

logger = logging.getLogger(__name__)

REPORT_VERSION = 1


def _resolve_ks2wtpy_dir() -> str:
    """acceptance 子包所在的 ks2wtpy 根目录。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _rollup(gates: Dict[str, GateResult], *, fail_on_warn: bool) -> str:
    statuses = {g.status for g in gates.values()}
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "fail" if fail_on_warn else "warn"
    if statuses and statuses <= {"pass", "skip"} and "pass" in statuses:
        return "pass"
    if statuses == {"skip"}:
        return "skip"
    return "pass"  # 兜底


def build_acceptance_report(
    output_dir: str,
    *,
    run_pytest: bool = True,
    run_pycompile: bool = True,
    readback_formats: Optional[List[str]] = None,
    readback_max_codes: int = 8,
    readback_max_rows: int = 5,
    pytest_path: Optional[str] = None,
    pytest_extra: Optional[List[str]] = None,
    extra_gates: Optional[Dict[str, GateResult]] = None,
    run_wt_readback: bool = False,
    wt_readback_timeout: int = 60,
    run_s3_matrix: bool = True,
    s3_matrix_max_codes: int = 4,
    s3_matrix_e2e: bool = False,
    s3_matrix_credentials_path: Optional[str] = None,
    s3_matrix_e2e_product: str = "SHFE.rb",
    s3_matrix_e2e_period: str = "d",
    s3_matrix_e2e_timeout: int = 900,
    run_freshness: bool = True,
    freshness_max_age_hours: float = 24.0,
    freshness_write_live_gate: bool = True,
    freshness_write_preflight: bool = True,
    run_completeness: bool = True,
    completeness_warn_pct: float = 5.0,
    completeness_fail_pct: float = 5.0,
    run_basefiles_loadback: bool = True,
    fail_on_warn: bool = False,
    ks2wtpy_dir: Optional[str] = None,
) -> Dict:
    output_dir = os.path.abspath(output_dir)
    ks2wtpy_dir = ks2wtpy_dir or _resolve_ks2wtpy_dir()
    readback_formats = readback_formats or ["csv", "lmdb", "dsb"]

    gates: Dict[str, GateResult] = {}
    gates["manifest"] = ManifestGate().run(output_dir)
    gates["quality"] = QualityGate().run(output_dir)
    gates["readback"] = ReadbackGate(formats=readback_formats,
                                      max_codes=readback_max_codes,
                                      max_rows=readback_max_rows).run(output_dir)
    if run_wt_readback:
        # 延迟 import：避免无 wtpy 环境时崩
        from .wt_readback import WtReadbackGate
        gates["wt_readback"] = WtReadbackGate(timeout=wt_readback_timeout).run(output_dir)
    if run_s3_matrix:
        from .s3_matrix import S3MatrixGate
        gates["s3_matrix"] = S3MatrixGate(
            skip_s3=not s3_matrix_e2e,
            max_codes=s3_matrix_max_codes,
            credentials_path=s3_matrix_credentials_path,
            run_s3_e2e=s3_matrix_e2e,
            e2e_product=s3_matrix_e2e_product,
            e2e_period=s3_matrix_e2e_period,
            e2e_timeout=s3_matrix_e2e_timeout,
        ).run(output_dir)
    if run_freshness:
        from .freshness import FreshnessGate
        gates["freshness"] = FreshnessGate(
            max_age_hours=freshness_max_age_hours,
            write_live_gate=freshness_write_live_gate,
            write_preflight=freshness_write_preflight,
        ).run(output_dir)
    if run_completeness:
        from .completeness import CompletenessGate
        gates["completeness"] = CompletenessGate(
            warn_pct=completeness_warn_pct,
            fail_pct=completeness_fail_pct,
        ).run(output_dir)
    if run_basefiles_loadback:
        from .basefiles_loadback import BasefilesLoadbackGate
        gates["basefiles_loadback"] = BasefilesLoadbackGate().run(output_dir)
    if run_pycompile:
        gates["pycompile"] = PyCompileGate().run(ks2wtpy_dir)
    else:
        gates["pycompile"] = GateResult("pycompile", "skip",
                                        "用户跳过 (--no-pycompile)")
    if run_pytest:
        gates["regression"] = PytestGate(test_path=pytest_path,
                                          extra_args=pytest_extra).run(ks2wtpy_dir)
    else:
        gates["regression"] = GateResult("regression", "skip",
                                          "用户跳过 (--no-pytest)")

    if extra_gates:
        for k, v in extra_gates.items():
            gates[k] = v

    # 取 version_id（manifest gate 已读过）
    version_id = (gates["manifest"].details or {}).get("version_id")

    overall = _rollup(gates, fail_on_warn=fail_on_warn)

    report = {
        "version": REPORT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "output_dir": output_dir,
        "ks2wtpy_dir": ks2wtpy_dir,
        "version_id": version_id,
        "overall_status": overall,
        "fail_on_warn": fail_on_warn,
        "gates": {name: g.to_dict() for name, g in gates.items()},
        "evidence": {
            "manifest_path": os.path.join(output_dir, "conversion_manifest.json"),
            "quality_report_path": os.path.join(output_dir, "quality_report.json"),
            "live_preflight_path": os.path.join(output_dir, "live_preflight_report.json"),
            "live_gate_path": os.path.join(output_dir, "live_gate.json"),
            "live_alerts_path": os.path.join(output_dir, "live_alerts.jsonl"),
        },
    }
    return report


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------

_BADGE = {"pass": "✅", "warn": "⚠️", "fail": "❌", "skip": "⏭"}


def _render_markdown(report: Dict) -> str:
    lines: List[str] = []
    badge = _BADGE.get(report["overall_status"], "❓")
    lines.append(f"# Acceptance Report {badge} {report['overall_status'].upper()}")
    lines.append("")
    lines.append(f"- generated_at: {report['generated_at']}")
    lines.append(f"- output_dir: `{report['output_dir']}`")
    lines.append(f"- version_id: `{report.get('version_id') or '-'}`")
    lines.append(f"- fail_on_warn: {report.get('fail_on_warn')}")
    lines.append("")
    lines.append("## Gates")
    lines.append("")
    lines.append("| Gate | Status | Summary |")
    lines.append("|------|--------|---------|")
    for name, g in report["gates"].items():
        b = _BADGE.get(g["status"], "❓")
        summary = (g.get("summary") or "").replace("|", "\\|")
        lines.append(f"| {name} | {b} {g['status']} | {summary} |")
    lines.append("")
    lines.append("## Evidence")
    for k, v in (report.get("evidence") or {}).items():
        lines.append(f"- {k}: `{v}`")
    lines.append("")
    # 错误折叠
    for name, g in report["gates"].items():
        if g["status"] in ("fail", "warn"):
            lines.append(f"### {name} ({g['status']}) details")
            try:
                snippet = json.dumps(g.get("details", {}), ensure_ascii=False,
                                     indent=2)[:4000]
            except Exception as e:  # noqa: BLE001
                logger.debug("JSON 序列化 details 失败, 降级为 str: %s", e)
                snippet = str(g.get("details"))[:4000]
            lines.append("```json")
            lines.append(snippet)
            lines.append("```")
            lines.append("")
    return "\n".join(lines) + "\n"


def write_acceptance_artifacts(
    report: Dict,
    *,
    json_path: Optional[str] = None,
    md_path: Optional[str] = None,
) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if json_path:
        os.makedirs(os.path.dirname(os.path.abspath(json_path)) or ".",
                    exist_ok=True)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        out["json"] = os.path.abspath(json_path)
    if md_path:
        os.makedirs(os.path.dirname(os.path.abspath(md_path)) or ".",
                    exist_ok=True)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(_render_markdown(report))
        out["md"] = os.path.abspath(md_path)
    return out
