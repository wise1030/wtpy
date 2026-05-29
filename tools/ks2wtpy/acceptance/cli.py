"""Acceptance CLI: python -m acceptance --output <dir> ..."""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from .report import build_acceptance_report, write_acceptance_artifacts


def _parse_formats(s: str) -> List[str]:
    s = (s or "").strip().lower()
    if not s or s == "all":
        return ["csv", "lmdb", "dsb"]
    out = []
    for tok in s.split(","):
        tok = tok.strip()
        if tok in ("csv", "lmdb", "dsb") and tok not in out:
            out.append(tok)
    return out or ["csv"]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m acceptance",
        description="ks2wtpy P0 acceptance runner",
    )
    p.add_argument("--output", required=True,
                   help="ks2wtpy 转换输出目录 (含 conversion_manifest.json)")
    p.add_argument("--readback", default="csv,lmdb,dsb",
                   help="读回格式，逗号分隔，默认 csv,lmdb,dsb")
    p.add_argument("--readback-codes", type=int, default=8,
                   help="每格式抽样合约/文件数 (默认 8)")
    p.add_argument("--readback-rows", type=int, default=5,
                   help="每文件读回前 N 行 (默认 5)")
    pytest_grp = p.add_mutually_exclusive_group()
    pytest_grp.add_argument("--pytest", dest="pytest", action="store_true",
                            default=True, help="跑回归测试 (默认)")
    pytest_grp.add_argument("--no-pytest", dest="pytest", action="store_false",
                            help="跳过 pytest")
    p.add_argument("--pytest-path", default=None,
                   help="自定义 pytest 路径 (相对 ks2wtpy)")
    pycomp_grp = p.add_mutually_exclusive_group()
    pycomp_grp.add_argument("--pycompile", dest="pycompile",
                            action="store_true", default=True)
    pycomp_grp.add_argument("--no-pycompile", dest="pycompile",
                            action="store_false")
    p.add_argument("--fail-on-warn", action="store_true",
                   help="任何 warn 也视为失败 (严格模式)")
    p.add_argument("--wt-readback", action="store_true",
                   help="启用 WT loader (WtDtServo) 读回 gate (HOT/specific/反例)")
    p.add_argument("--wt-readback-timeout", type=int, default=60,
                   help="单个 WT readback 子进程超时秒 (默认 60)")
    s3m = p.add_mutually_exclusive_group()
    s3m.add_argument("--s3-matrix", dest="s3_matrix", action="store_true",
                     default=True, help="启用 CSV/LMDB/DSB 跨格式一致性 gate (默认)")
    s3m.add_argument("--no-s3-matrix", dest="s3_matrix", action="store_false",
                     help="跳过 s3_matrix gate")
    p.add_argument("--s3-matrix-max-codes", type=int, default=4,
                   help="s3_matrix 最多核对合约数 (默认 4)")
    p.add_argument("--s3-matrix-e2e", action="store_true",
                   help="先用真实 S3 跑一轮三格式输出再核对 (需要 credentials)")
    p.add_argument("--s3-matrix-credentials", default=None,
                   help="credentials.json 路径，缺省读 env / ks2wtpy/credentials.json")
    p.add_argument("--s3-matrix-product", default="SHFE.rb",
                   help="--s3-matrix-e2e 使用的品种 (默认 SHFE.rb)")
    p.add_argument("--s3-matrix-period", default="d",
                   help="--s3-matrix-e2e 使用的周期 (默认 d)")
    p.add_argument("--s3-matrix-timeout", type=int, default=900,
                   help="--s3-matrix-e2e 单次转换超时秒 (默认 900)")
    fr = p.add_mutually_exclusive_group()
    fr.add_argument("--freshness", dest="freshness", action="store_true",
                    default=True, help="启用 freshness gate (默认开)")
    fr.add_argument("--no-freshness", dest="freshness", action="store_false",
                    help="跳过 freshness gate")
    p.add_argument("--freshness-max-age-hours", type=float, default=24.0,
                   help="freshness gate 最大容忍小时数 (日线 24, 分钟线 2)")
    p.add_argument("--freshness-write-live-gate", action="store_true",
                   default=True,
                   help="freshness gate 写 live_gate.json (默认开, 启动脚本读取)")
    p.add_argument("--freshness-write-preflight", action="store_true",
                   default=True,
                   help="freshness gate 写 live_preflight_report.json (默认开)")
    cp = p.add_mutually_exclusive_group()
    cp.add_argument("--completeness", dest="completeness", action="store_true",
                    default=True, help="启用 completeness gate (默认开)")
    cp.add_argument("--no-completeness", dest="completeness", action="store_false",
                    help="跳过 completeness gate")
    p.add_argument("--completeness-warn-pct", type=float, default=5.0,
                   help="completeness warn 阈值 (默认 5%%)")
    p.add_argument("--completeness-fail-pct", type=float, default=5.0,
                   help="completeness fail 阈值 (默认 5%%, > 此值即 fail)")
    bl = p.add_mutually_exclusive_group()
    bl.add_argument("--basefiles-loadback", dest="basefiles_loadback",
                    action="store_true", default=True,
                    help="启用 basefiles_loadback gate (默认开, 用 wtpy 原生 "
                         "ContractMgr/SessionMgr/ProductMgr loadback 校验)")
    bl.add_argument("--no-basefiles-loadback", dest="basefiles_loadback",
                    action="store_false",
                    help="跳过 basefiles_loadback gate")
    p.add_argument("--report-json", default=None,
                   help="输出 acceptance_report.json 路径；默认写入 <output>/")
    p.add_argument("--report-md", default=None,
                   help="输出 acceptance_report.md 路径；默认写入 <output>/")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    formats = _parse_formats(args.readback)

    report = build_acceptance_report(
        args.output,
        run_pytest=args.pytest,
        run_pycompile=args.pycompile,
        readback_formats=formats,
        readback_max_codes=args.readback_codes,
        readback_max_rows=args.readback_rows,
        pytest_path=args.pytest_path,
        run_wt_readback=args.wt_readback,
        wt_readback_timeout=args.wt_readback_timeout,
        run_s3_matrix=args.s3_matrix,
        s3_matrix_max_codes=args.s3_matrix_max_codes,
        s3_matrix_e2e=args.s3_matrix_e2e,
        s3_matrix_credentials_path=args.s3_matrix_credentials,
        s3_matrix_e2e_product=args.s3_matrix_product,
        s3_matrix_e2e_period=args.s3_matrix_period,
        s3_matrix_e2e_timeout=args.s3_matrix_timeout,
        run_freshness=args.freshness,
        freshness_max_age_hours=args.freshness_max_age_hours,
        freshness_write_live_gate=args.freshness_write_live_gate,
        freshness_write_preflight=args.freshness_write_preflight,
        run_completeness=args.completeness,
        completeness_warn_pct=args.completeness_warn_pct,
        completeness_fail_pct=args.completeness_fail_pct,
        run_basefiles_loadback=args.basefiles_loadback,
        fail_on_warn=args.fail_on_warn,
    )

    json_path = args.report_json or os.path.join(args.output, "acceptance_report.json")
    md_path = args.report_md or os.path.join(args.output, "acceptance_report.md")
    out = write_acceptance_artifacts(report, json_path=json_path, md_path=md_path)

    print(json.dumps({
        "overall_status": report["overall_status"],
        "version_id": report.get("version_id"),
        "report_json": out.get("json"),
        "report_md": out.get("md"),
        "gates": {n: g["status"] for n, g in report["gates"].items()},
    }, ensure_ascii=False, indent=2))

    if report["overall_status"] == "fail":
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
