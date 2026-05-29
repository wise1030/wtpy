"""Phase 5-3 [P0] 实盘 gate: 启动脚本入口 (live_gate)

启动脚本契约 (供 K8s/systemd/shell 直接调用):

    python -m acceptance.live_gate --output <dir> --max-age-hours 24
    exit 0  → ok (启动)
    exit 1  → warn (默认非阻断, --strict 则阻断)
    exit 2  → fail (硬阻断)
    --strict: warn 升 fail

设计: 轻量 preflight, 只跑 freshness gate, 不跑 pytest/pycompile/wt_readback。
副产物: ``live_gate.json`` (FreshnessGate 落地) — shell 可 jq 读 ``.ok``。
stdout: 单行 JSON (jq friendly), 含 ok/status/exit_code/summary。

未来 p5-2 完整性 gate 可在此挂接, 但 freshness 是最小可用集。

Shell 启动示例:

    if ! python -m acceptance.live_gate --output /data/wt --max-age-hours 24; then
        echo "数据 preflight 失败, 不启动策略" >&2
        exit 1
    fi
    exec python run_strategy.py
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

from .completeness import CompletenessGate
from .freshness import FreshnessGate


# 退出码契约
EXIT_OK = 0
EXIT_WARN = 1
EXIT_FAIL = 2


def _status_to_exit(status: str, *, strict: bool) -> int:
    if status == "pass":
        return EXIT_OK
    if status == "warn":
        return EXIT_FAIL if strict else EXIT_WARN
    # fail / 未知 → fail
    return EXIT_FAIL


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m acceptance.live_gate",
        description=(
            "ks2wtpy 实盘启动 preflight gate. "
            "exit 0=ok, 1=warn, 2=fail (--strict 把 warn 升为 fail). "
            "stdout 为单行 JSON 供 shell/jq 解析."
        ),
    )
    p.add_argument("--output", required=True,
                   help="ks2wtpy 转换输出目录 (含 conversion_manifest.json)")
    p.add_argument("--max-age-hours", type=float, default=24.0,
                   help="新鲜度容忍上限 (小时), 日线 24, 分钟线推荐 2")
    p.add_argument("--strict", action="store_true",
                   help="warn 升 fail (生产环境推荐开启)")
    p.add_argument("--no-live-gate-json", action="store_true",
                   help="禁用 live_gate.json 落地 (调试用, 默认写)")
    p.add_argument("--no-preflight-json", action="store_true",
                   help="禁用 live_preflight_report.json 落地 (默认写)")
    cg = p.add_mutually_exclusive_group()
    cg.add_argument("--completeness", dest="completeness", action="store_true",
                    default=True, help="同时跑 completeness gate (默认开)")
    cg.add_argument("--no-completeness", dest="completeness",
                    action="store_false", help="跳过 completeness gate")
    p.add_argument("--completeness-warn-pct", type=float, default=5.0,
                   help="completeness warn 阈值 (默认 5%%)")
    p.add_argument("--completeness-fail-pct", type=float, default=5.0,
                   help="completeness fail 阈值 (默认 5%%)")
    return p


def _combine_status(*statuses: str) -> str:
    """聚合多 gate 状态: 任一 fail→fail; 任一 warn→warn; 全 skip→skip; 否则 pass。"""
    s = set(statuses)
    if "fail" in s:
        return "fail"
    if "warn" in s:
        return "warn"
    if s == {"skip"}:
        return "skip"
    return "pass"


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    fresh_gate = FreshnessGate(
        max_age_hours=args.max_age_hours,
        write_live_gate=not args.no_live_gate_json,
        write_preflight=not args.no_preflight_json,
    )
    fresh_r = fresh_gate.run(args.output)

    comp_r = None
    if args.completeness:
        comp_gate = CompletenessGate(
            warn_pct=args.completeness_warn_pct,
            fail_pct=args.completeness_fail_pct,
        )
        comp_r = comp_gate.run(args.output)

    # 聚合
    statuses = [fresh_r.status]
    if comp_r is not None:
        statuses.append(comp_r.status)
    overall = _combine_status(*statuses)
    exit_code = _status_to_exit(overall, strict=args.strict)

    # stdout: 单行 JSON, 便于 `python -m acceptance.live_gate ... | jq .ok`
    gates_payload: Dict[str, Any] = {"freshness": {
        "status": fresh_r.status,
        "summary": fresh_r.summary,
        "details": fresh_r.details,
    }}
    if comp_r is not None:
        gates_payload["completeness"] = {
            "status": comp_r.status,
            "summary": comp_r.summary,
            "details": comp_r.details,
        }
    payload = {
        "ok": exit_code == EXIT_OK,
        "status": overall,
        "exit_code": exit_code,
        "gate": "live_gate",
        "summary": f"freshness={fresh_r.status}" + (
            f", completeness={comp_r.status}" if comp_r else ""),
        "max_age_hours": args.max_age_hours,
        "strict": args.strict,
        "gates": gates_payload,
        # 兼容 p5-1 旧契约: 顶层 details 暴露 freshness 全 details
        "details": fresh_r.details,
    }
    print(json.dumps(payload, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
