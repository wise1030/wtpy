"""
P0-A acceptance_report 生成器 — 测试

acceptance 子包应输出一个统一的 acceptance_report.json，结构：
{
  "version": int,
  "generated_at": ISO,
  "output_dir": str,
  "version_id": str | None,           # 来自 conversion_manifest.access_contract.version_id
  "overall_status": "pass" | "warn" | "fail",
  "gates": {
      "manifest":   {"status": ..., "summary": str, "details": {...}},
      "quality":    {"status": ..., "summary": str, "details": {...}},
      "readback":   {"status": ..., "summary": str, "details": {...}},
      "regression": {"status": ..., "summary": str, "details": {...}},
      "pycompile":  {"status": ..., "summary": str, "details": {...}},
      "s3_matrix":  {"status": ..., "summary": str, "details": {...}},   # 可选
  },
  "evidence": {"manifest_path":..., "quality_report_path":..., "pytest_log":..., ...}
}

每个 gate 的 status:
  - pass : 全部 OK
  - warn : 只有 warning（如 time_gaps/price_anomalies），默认不阻塞 overall
  - fail : 任一硬错误（manifest errors>0、duplicates、ohlc_errors、负值、负 lmdb_keys、readback 失败、回归失败等）
  - skip : 用户显式跳过 / 缺少前置物
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

KS2WTPY_DIR = Path(__file__).resolve().parents[1]
if str(KS2WTPY_DIR) not in sys.path:
    sys.path.insert(0, str(KS2WTPY_DIR))


def _make_min_output_dir(tmp_path: Path, *,
                          manifest_overrides=None,
                          quality_overrides=None,
                          add_csv=True) -> Path:
    """构造最小可验收输出目录（manifest+quality+一个 CSV）。"""
    out = tmp_path / "out"
    out.mkdir()
    (out / "csv").mkdir()
    (out / "common").mkdir()

    if add_csv:
        # 一个最小有效 CSV，1 条 m5 bar
        csv_text = (
            "date,time,open,high,low,close,volume,turnover,open_interest,diff\n"
            "20251015,0905,3000,3001,2999,3000,10,30000,123,0\n"
        )
        (out / "csv" / "SHFE.rb2510_m5.csv").write_text(csv_text, encoding="utf-8")

    manifest = {
        "version": 1,
        "generated_at": "2026-05-24T00:00:00",
        "mode": "full",
        "stats": {"csv_codes": 1, "lmdb_bars": 0, "errors": 0, "error_details": []},
        "outputs": {"csv": str(out / "csv")},
        "enabled_outputs": ["csv"],
        "access_contract": {
            "authority": "conversion_manifest.json",
            "version_id": "ks2wtpy-test-version-id",
            "quality_gate": {
                "quality_report": str(out / "quality_report.json"),
                "block_on_stats_errors": True,
                "block_on_live_preflight_failure": True,
            },
        },
        "access_scenarios": {
            "backtest": {
                "primary_storage": "csv",
                "basefiles": str(out / "common"),
            },
        },
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)
    (out / "conversion_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    quality = {
        "version": 1,
        "generated_at": "2026-05-24T00:00:00",
        "summary": {
            "csv_files": 1,
            "csv_rows": 1,
            "duplicates": 0,
            "out_of_order": 0,
            "ohlc_errors": 0,
            "negative_fields": 0,
            "parse_errors": 0,
            "time_gaps": 0,
            "open_interest_jumps": 0,
            "price_anomalies": 0,
            "day_minute_date_mismatches": 0,
            "lmdb_keys": 0,
            "dsb_files": 0,
            "dsb_bars": 0,
        },
        "files": [{
            "file": "SHFE.rb2510_m5.csv",
            "rows": 1,
            "duplicates": 0,
            "ohlc_errors": 0,
            "negative_fields": 0,
            "parse_errors": 0,
            "first_key": "20251015.0905",
            "last_key": "20251015.0905",
        }],
    }
    if quality_overrides:
        quality["summary"].update(quality_overrides.get("summary", {}))
        # ops-2: 让 summary 异常同步落到 files[0], 否则 QualityGate (启用白名单)
        # 走 actionable_counts 分桶, 全零 fixture 不能复现 fail/warn 语义
        _ANOMALY_KEYS = ("duplicates", "ohlc_errors", "negative_fields",
                         "parse_errors", "out_of_order", "time_gaps",
                         "price_anomalies", "open_interest_jumps",
                         "day_minute_date_mismatches")
        ov = quality_overrides.get("summary", {})
        for k in _ANOMALY_KEYS:
            if k in ov and quality["files"]:
                quality["files"][0][k] = ov[k]
    (out / "quality_report.json").write_text(
        json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out


# -------- 1. ManifestGate ----------------------------------------------------

def test_manifest_gate_pass(tmp_path):
    from acceptance import gates  # type: ignore

    out = _make_min_output_dir(tmp_path)
    g = gates.ManifestGate().run(str(out))
    assert g.status == "pass", g.summary
    assert g.details["stats"]["errors"] == 0
    assert g.details["version_id"] == "ks2wtpy-test-version-id"


def test_manifest_gate_fail_on_errors(tmp_path):
    from acceptance import gates  # type: ignore

    out = _make_min_output_dir(tmp_path, manifest_overrides={
        "stats": {"csv_codes": 1, "lmdb_bars": 0, "errors": 2,
                  "error_details": [{"code": "csv_write", "msg": "boom"}]},
    })
    g = gates.ManifestGate().run(str(out))
    assert g.status == "fail"
    assert g.details["stats"]["errors"] == 2
    assert "errors" in g.summary.lower()


def test_manifest_gate_fail_when_missing(tmp_path):
    from acceptance import gates  # type: ignore

    g = gates.ManifestGate().run(str(tmp_path / "no-such"))
    assert g.status == "fail"


# -------- 2. QualityGate -----------------------------------------------------

def test_quality_gate_pass(tmp_path):
    from acceptance import gates  # type: ignore

    out = _make_min_output_dir(tmp_path)
    g = gates.QualityGate().run(str(out))
    assert g.status == "pass", g.summary
    assert g.details["summary"]["duplicates"] == 0


def test_quality_gate_fail_on_duplicates(tmp_path):
    from acceptance import gates  # type: ignore

    out = _make_min_output_dir(tmp_path, quality_overrides={
        "summary": {"duplicates": 3},
    })
    g = gates.QualityGate().run(str(out))
    assert g.status == "fail"
    assert "duplicates" in g.summary.lower()


def test_quality_gate_warn_on_soft_anomaly(tmp_path):
    from acceptance import gates  # type: ignore

    out = _make_min_output_dir(tmp_path, quality_overrides={
        "summary": {"time_gaps": 5, "price_anomalies": 2,
                    "open_interest_jumps": 1, "day_minute_date_mismatches": 4},
    })
    g = gates.QualityGate().run(str(out))
    assert g.status == "warn", g.summary
    assert g.details["warnings"]


# -------- 3. ReadbackGate (CSV，离线) ----------------------------------------

def test_readback_gate_csv_pass(tmp_path):
    from acceptance import gates  # type: ignore

    out = _make_min_output_dir(tmp_path)
    g = gates.ReadbackGate(formats=["csv"], max_codes=4).run(str(out))
    assert g.status == "pass", g.summary
    assert g.details["csv"]["files_checked"] >= 1
    assert g.details["csv"]["failures"] == []


def test_readback_gate_csv_daily_no_time_col(tmp_path):
    """日线 CSV 不含 time 列也应通过。"""
    from acceptance import gates  # type: ignore

    out = _make_min_output_dir(tmp_path)
    (out / "csv" / "SHFE.rb.HOT_d.csv").write_text(
        "date,open,high,low,close,volume,turnover,open_interest\n"
        "20251015,3000,3010,2990,3005,1234,5000000,98765\n",
        encoding="utf-8",
    )
    g = gates.ReadbackGate(formats=["csv"], max_codes=10).run(str(out))
    assert g.status == "pass", g.summary
    assert g.details["csv"]["failures"] == []


def test_readback_gate_csv_minute_requires_time(tmp_path):
    """分钟线缺 time 列必须 fail。"""
    from acceptance import gates  # type: ignore

    out = _make_min_output_dir(tmp_path, add_csv=False)
    (out / "csv" / "SHFE.rb2510_m5.csv").write_text(
        "date,open,high,low,close,volume,turnover,open_interest\n"
        "20251015,3000,3010,2990,3005,10,30000,123\n",
        encoding="utf-8",
    )
    g = gates.ReadbackGate(formats=["csv"], max_codes=10).run(str(out))
    assert g.status == "fail", g.summary


def test_readback_gate_csv_date_time_string_format(tmp_path):
    """支持 WT loader 接受的 YYYY/MM/DD + HH:MM:SS 字符串格式。"""
    from acceptance import gates  # type: ignore

    out = _make_min_output_dir(tmp_path, add_csv=False)
    (out / "csv" / "SHFE.rb.2510_m5.csv").write_text(
        "date,time,open,high,low,close,volume,turnover,open_interest\n"
        "2024/10/16,09:00:00,3569.0,3572.0,3565.0,3572.0,28.0,0.0,333.0\n",
        encoding="utf-8",
    )
    (out / "csv" / "SHFE.rb.HOT_d.csv").write_text(
        "date,open,high,low,close,volume,turnover,open_interest\n"
        "2014/03/24,3314.0,3330.0,3266.0,3330.0,86810.0,2868577800.0,47616.0\n",
        encoding="utf-8",
    )
    g = gates.ReadbackGate(formats=["csv"], max_codes=10).run(str(out))
    assert g.status == "pass", g.summary


def test_readback_gate_csv_fail_on_bad_row(tmp_path):
    from acceptance import gates  # type: ignore

    out = _make_min_output_dir(tmp_path)
    # 写一个明显坏的 CSV：close 为非数值
    (out / "csv" / "SHFE.broken_m5.csv").write_text(
        "date,time,open,high,low,close,volume,turnover,open_interest,diff\n"
        "20251015,0905,abc,abc,abc,abc,10,0,0,0\n",
        encoding="utf-8",
    )
    g = gates.ReadbackGate(formats=["csv"], max_codes=10).run(str(out))
    assert g.status == "fail", g.summary
    assert any("broken" in f["file"] for f in g.details["csv"]["failures"])


def test_readback_gate_skip_when_format_missing(tmp_path):
    from acceptance import gates  # type: ignore

    out = _make_min_output_dir(tmp_path)
    g = gates.ReadbackGate(formats=["csv", "lmdb", "dsb"], max_codes=2).run(str(out))
    # csv pass，lmdb/dsb skip（目录不存在），整体不能 fail
    assert g.status in ("pass", "warn")
    assert g.details["lmdb"]["status"] == "skip"
    assert g.details["dsb"]["status"] == "skip"


# -------- 4. AcceptanceReport 聚合 ------------------------------------------

def test_acceptance_report_overall_pass(tmp_path):
    from acceptance.report import build_acceptance_report  # type: ignore

    out = _make_min_output_dir(tmp_path)
    report = build_acceptance_report(
        str(out),
        run_pytest=False,
        run_pycompile=False,
        run_freshness=False,
        run_basefiles_loadback=False,
        readback_formats=["csv"],
    )
    assert report["overall_status"] == "pass"
    gates = report["gates"]
    assert gates["manifest"]["status"] == "pass"
    assert gates["quality"]["status"] == "pass"
    assert gates["readback"]["status"] == "pass"
    assert report["version_id"] == "ks2wtpy-test-version-id"


def test_acceptance_report_overall_fail_when_manifest_errors(tmp_path):
    from acceptance.report import build_acceptance_report  # type: ignore

    out = _make_min_output_dir(tmp_path, manifest_overrides={
        "stats": {"csv_codes": 1, "lmdb_bars": 0, "errors": 1,
                  "error_details": [{"code": "x", "msg": "y"}]},
    })
    report = build_acceptance_report(
        str(out),
        run_pytest=False, run_pycompile=False, readback_formats=["csv"],
        run_basefiles_loadback=False,
    )
    assert report["overall_status"] == "fail"


def test_acceptance_report_warn_when_soft_anomaly_only(tmp_path):
    from acceptance.report import build_acceptance_report  # type: ignore

    out = _make_min_output_dir(tmp_path, quality_overrides={
        "summary": {"time_gaps": 5, "price_anomalies": 2},
    })
    report = build_acceptance_report(
        str(out),
        run_pytest=False, run_pycompile=False, readback_formats=["csv"],
        run_freshness=False, run_completeness=False,
        run_basefiles_loadback=False,
        fail_on_warn=False,
    )
    assert report["overall_status"] == "warn"

    report2 = build_acceptance_report(
        str(out),
        run_pytest=False, run_pycompile=False, readback_formats=["csv"],
        run_freshness=False, run_completeness=False,
        run_basefiles_loadback=False,
        fail_on_warn=True,
    )
    assert report2["overall_status"] == "fail"


def test_acceptance_report_writes_artifacts(tmp_path):
    from acceptance.report import write_acceptance_artifacts, build_acceptance_report  # type: ignore

    out = _make_min_output_dir(tmp_path)
    report = build_acceptance_report(
        str(out),
        run_pytest=False, run_pycompile=False, readback_formats=["csv"],
        run_basefiles_loadback=False,
    )
    json_path = tmp_path / "acceptance_report.json"
    md_path = tmp_path / "acceptance_report.md"
    write_acceptance_artifacts(report, json_path=str(json_path), md_path=str(md_path))

    assert json_path.exists() and md_path.exists()
    loaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert loaded["overall_status"] == report["overall_status"]
    md = md_path.read_text(encoding="utf-8")
    assert "Acceptance Report" in md
    assert "manifest" in md.lower()


# -------- 5. CLI 入口 --------------------------------------------------------

def test_cli_run_exits_zero_on_pass(tmp_path, capsys, monkeypatch):
    from acceptance import cli  # type: ignore

    out = _make_min_output_dir(tmp_path)
    json_path = tmp_path / "ar.json"
    md_path = tmp_path / "ar.md"
    rc = cli.main([
        "--output", str(out),
        "--no-pytest", "--no-pycompile", "--no-freshness", "--no-completeness",
        "--no-basefiles-loadback",
        "--readback", "csv",
        "--report-json", str(json_path),
        "--report-md", str(md_path),
    ])
    assert rc == 0
    assert json_path.exists()
    assert md_path.exists()


def test_cli_run_exits_nonzero_on_fail(tmp_path):
    from acceptance import cli  # type: ignore

    out = _make_min_output_dir(tmp_path, manifest_overrides={
        "stats": {"csv_codes": 1, "lmdb_bars": 0, "errors": 1,
                  "error_details": [{"code": "x", "msg": "y"}]},
    })
    rc = cli.main([
        "--output", str(out),
        "--no-pytest", "--no-pycompile", "--no-freshness", "--no-completeness",
        "--no-basefiles-loadback",
        "--readback", "csv",
    ])
    assert rc != 0


# -------- 6. s3_matrix gate 集成 -------------------------------------------

def test_acceptance_report_includes_s3_matrix_by_default(tmp_path):
    """默认应注册 s3_matrix gate；CSV-only 仓应 skip 而非 fail。"""
    from acceptance.report import build_acceptance_report  # type: ignore

    out = _make_min_output_dir(tmp_path)
    report = build_acceptance_report(
        str(out),
        run_pytest=False, run_pycompile=False, readback_formats=["csv"],
        run_freshness=False, run_completeness=False,
        run_basefiles_loadback=False,
    )
    assert "s3_matrix" in report["gates"], \
        "s3_matrix gate 应默认接入 acceptance_report"
    # CSV-only 仓 → 无 LMDB/DSB 输出 → skip 不阻塞 overall
    assert report["gates"]["s3_matrix"]["status"] in ("skip", "pass")
    assert report["overall_status"] in ("pass", "warn")


def test_acceptance_report_s3_matrix_can_be_disabled(tmp_path):
    from acceptance.report import build_acceptance_report  # type: ignore

    out = _make_min_output_dir(tmp_path)
    report = build_acceptance_report(
        str(out),
        run_pytest=False, run_pycompile=False, readback_formats=["csv"],
        run_s3_matrix=False,
        run_basefiles_loadback=False,
    )
    assert "s3_matrix" not in report["gates"], \
        "run_s3_matrix=False 时不应注册 s3_matrix gate"


def test_cli_no_s3_matrix_flag_drops_gate(tmp_path):
    from acceptance import cli  # type: ignore

    out = _make_min_output_dir(tmp_path)
    json_path = tmp_path / "ar.json"
    rc = cli.main([
        "--output", str(out),
        "--no-pytest", "--no-pycompile", "--no-freshness", "--no-completeness", "--no-s3-matrix",
        "--no-basefiles-loadback",
        "--readback", "csv",
        "--report-json", str(json_path),
        "--report-md", str(tmp_path / "ar.md"),
    ])
    assert rc == 0
    loaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert "s3_matrix" not in loaded["gates"]


def test_cli_s3_matrix_e2e_flag_propagates_without_running(tmp_path, monkeypatch):
    """--s3-matrix-e2e 应把 run_s3_e2e=True 传下去，但单测不实际发起 S3 请求：
    monkey-patch S3MatrixGate.run 验证参数。"""
    from acceptance import cli  # type: ignore
    from acceptance import s3_matrix as s3m  # type: ignore
    from acceptance.gates import GateResult

    captured = {}
    orig_init = s3m.S3MatrixGate.__init__

    def fake_init(self, **kwargs):
        captured.update(kwargs)
        orig_init(self, **kwargs)

    def fake_run(self, output_dir):
        return GateResult("s3_matrix", "skip",
                          "e2e disabled in test", details={"captured": True})

    monkeypatch.setattr(s3m.S3MatrixGate, "__init__", fake_init)
    monkeypatch.setattr(s3m.S3MatrixGate, "run", fake_run)

    out = _make_min_output_dir(tmp_path)
    rc = cli.main([
        "--output", str(out),
        "--no-pytest", "--no-pycompile", "--no-freshness", "--no-completeness",
        "--no-basefiles-loadback",
        "--readback", "csv",
        "--s3-matrix-e2e",
        "--s3-matrix-product", "SHFE.rb",
        "--s3-matrix-period", "d",
        "--s3-matrix-max-codes", "2",
        "--report-json", str(tmp_path / "ar.json"),
        "--report-md", str(tmp_path / "ar.md"),
    ])
    assert rc == 0
    assert captured.get("run_s3_e2e") is True
    assert captured.get("skip_s3") is False
    assert captured.get("e2e_product") == "SHFE.rb"
    assert captured.get("e2e_period") == "d"
    assert captured.get("max_codes") == 2
