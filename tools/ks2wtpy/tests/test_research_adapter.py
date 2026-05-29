"""
p5-4: 投研服务化 only-read data service / notebook adapter — 测试

研究侧只读派生视图加载器：
- open_research_view(path)        — path 可为 metadata 文件 或 包含 research_view_metadata.json 的目录
- ResearchView.list_datasets()    — 返回 [(std_code, period, fmt)] 元组
- ResearchView.load(code, period) — 返回 dict(rows=List[Dict], columns=List[str], lineage=Dict)
                                     可选 as_dataframe=True 返回 pandas.DataFrame（若 pandas 可用）
- ResearchView.describe(code, period) — 返回 lineage 元数据（source_csv / quality_report / manifest 路径）
- ResearchView.lineage            — 顶层 lineage（authority_source / inputs）

只读铁律：
- 不写任何文件
- 找不到数据集 -> KeyError
- 元数据缺失 -> FileNotFoundError
- 默认 fmt='auto' -> 优先 parquet（若有），否则 csv
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


def _write_research_csv(path: Path, std_code: str, period: str, rows):
    header = "datetime,std_code,period,open,high,low,close,volume,turnover,open_interest\n"
    body = "".join(
        ",".join(str(c) for c in row) + "\n" for row in rows
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + body, encoding="utf-8")


def _make_view_dir(tmp_path: Path, *, with_parquet: bool = False) -> Path:
    out = tmp_path / "out"
    research = out / "research"
    research.mkdir(parents=True)

    csv1 = research / "SHFE.rb.HOT_day_research.csv"
    csv2 = research / "SHFE.rb.HOT_min5_research.csv"
    _write_research_csv(csv1, "SHFE.rb.HOT", "day", [
        ["2025-10-15 00:00:00", "SHFE.rb.HOT", "day", 3000, 3010, 2990, 3005, 12345, 37000000, 88888],
        ["2025-10-16 00:00:00", "SHFE.rb.HOT", "day", 3005, 3020, 3000, 3015, 23456, 70000000, 90000],
    ])
    _write_research_csv(csv2, "SHFE.rb.HOT", "min5", [
        ["2025-10-15 09:05:00", "SHFE.rb.HOT", "min5", 3000, 3001, 2999, 3000, 10, 30000, 88800],
    ])

    datasets = [
        {
            "std_code": "SHFE.rb.HOT",
            "period": "day",
            "source_csv": str(out / "csv" / "SHFE.rb.HOT_d.csv"),
            "derived_csv": str(csv1),
            "rows": 2,
            "parquet_status": "skipped",
        },
        {
            "std_code": "SHFE.rb.HOT",
            "period": "min5",
            "source_csv": str(out / "csv" / "SHFE.rb.HOT_m5.csv"),
            "derived_csv": str(csv2),
            "rows": 1,
            "parquet_status": "skipped",
        },
    ]
    formats = ["csv"]
    parquet_files = []
    if with_parquet:
        try:
            import pyarrow  # noqa: F401
        except ImportError:
            pytest.skip("pyarrow not installed, cannot make parquet fixture")
        import pyarrow as pa
        import pyarrow.parquet as pq
        pq_path = research / "SHFE.rb.HOT_day_research.parquet"
        pq.write_table(pa.table({
            "datetime": ["2025-10-15 00:00:00", "2025-10-16 00:00:00"],
            "std_code": ["SHFE.rb.HOT", "SHFE.rb.HOT"],
            "period": ["day", "day"],
            "open": [3000.0, 3005.0],
            "high": [3010.0, 3020.0],
            "low":  [2990.0, 3000.0],
            "close":[3005.0, 3015.0],
            "volume":   [12345, 23456],
            "turnover": [37000000.0, 70000000.0],
            "open_interest": [88888, 90000],
        }), str(pq_path))
        datasets[0]["derived_parquet"] = str(pq_path)
        datasets[0]["parquet_status"] = "written"
        parquet_files.append(str(pq_path))
        formats.append("parquet")

    metadata = {
        "version": 1,
        "generated_at": "2026-05-25T00:00:00",
        "scenario": "research_analysis",
        "authority_source": "wt_storage_converted_outputs",
        "derived_view_only": True,
        "formats": formats,
        "parquet": {
            "status": "available" if parquet_files else "unavailable",
            "files": parquet_files,
            "reason": "" if parquet_files else "no parquet files written",
        },
        "inputs": {
            "conversion_manifest": str(out / "conversion_manifest.json"),
            "quality_report": str(out / "quality_report.json"),
            "csv_dir": str(out / "csv"),
            "lmdb_dir": str(out / "lmdb"),
            "dsb_dir": str(out / "dsb"),
            "basefiles": str(out / "common"),
        },
        "datasets": datasets,
    }
    (research / "research_view_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return research


# ---------- open_research_view ----------

def test_open_research_view_from_dir(tmp_path):
    from research.adapter import open_research_view
    research = _make_view_dir(tmp_path)
    view = open_research_view(str(research))
    assert view.derived_view_only is True
    assert view.authority_source == "wt_storage_converted_outputs"


def test_open_research_view_from_metadata_file(tmp_path):
    from research.adapter import open_research_view
    research = _make_view_dir(tmp_path)
    view = open_research_view(str(research / "research_view_metadata.json"))
    assert view.scenario == "research_analysis"


def test_open_research_view_missing_metadata(tmp_path):
    from research.adapter import open_research_view
    with pytest.raises(FileNotFoundError):
        open_research_view(str(tmp_path / "nope"))


# ---------- list_datasets ----------

def test_list_datasets_returns_codes_periods(tmp_path):
    from research.adapter import open_research_view
    view = open_research_view(str(_make_view_dir(tmp_path)))
    items = view.list_datasets()
    assert ("SHFE.rb.HOT", "day") in [(c, p) for c, p, *_ in items]
    assert ("SHFE.rb.HOT", "min5") in [(c, p) for c, p, *_ in items]


# ---------- load (csv path) ----------

def test_load_csv_returns_rows_columns_lineage(tmp_path):
    from research.adapter import open_research_view
    view = open_research_view(str(_make_view_dir(tmp_path)))
    bundle = view.load("SHFE.rb.HOT", "day")
    assert isinstance(bundle, dict)
    assert "rows" in bundle and "columns" in bundle and "lineage" in bundle
    assert len(bundle["rows"]) == 2
    cols = bundle["columns"]
    assert cols[0] == "datetime"
    assert "close" in cols and "open_interest" in cols
    row0 = bundle["rows"][0]
    assert row0["std_code"] == "SHFE.rb.HOT"
    assert row0["period"] == "day"
    assert float(row0["close"]) == 3005.0
    assert bundle["lineage"]["derived_csv"].endswith("SHFE.rb.HOT_day_research.csv")
    assert bundle["lineage"]["source_csv"].endswith("SHFE.rb.HOT_d.csv")
    assert bundle["lineage"]["format_used"] == "csv"


def test_load_missing_dataset_raises_keyerror(tmp_path):
    from research.adapter import open_research_view
    view = open_research_view(str(_make_view_dir(tmp_path)))
    with pytest.raises(KeyError):
        view.load("SHFE.cu.HOT", "day")
    with pytest.raises(KeyError):
        view.load("SHFE.rb.HOT", "min1")


def test_load_explicit_csv_format(tmp_path):
    from research.adapter import open_research_view
    view = open_research_view(str(_make_view_dir(tmp_path)))
    bundle = view.load("SHFE.rb.HOT", "min5", fmt="csv")
    assert bundle["lineage"]["format_used"] == "csv"
    assert len(bundle["rows"]) == 1


# ---------- load (parquet path) ----------

def test_load_auto_prefers_parquet_when_available(tmp_path):
    pytest.importorskip("pyarrow")
    from research.adapter import open_research_view
    view = open_research_view(str(_make_view_dir(tmp_path, with_parquet=True)))
    bundle = view.load("SHFE.rb.HOT", "day")  # fmt='auto'
    assert bundle["lineage"]["format_used"] == "parquet"
    assert len(bundle["rows"]) == 2


def test_load_parquet_fallback_when_unavailable(tmp_path):
    """fmt='parquet' 但该 dataset 无 parquet -> 应明确 fail，不静默回 csv。"""
    from research.adapter import open_research_view
    view = open_research_view(str(_make_view_dir(tmp_path)))
    with pytest.raises((FileNotFoundError, RuntimeError)):
        view.load("SHFE.rb.HOT", "day", fmt="parquet")


# ---------- as_dataframe ----------

def test_load_as_dataframe(tmp_path):
    pd = pytest.importorskip("pandas")
    from research.adapter import open_research_view
    view = open_research_view(str(_make_view_dir(tmp_path)))
    df = view.load("SHFE.rb.HOT", "day", as_dataframe=True)
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 2
    assert set(["datetime", "std_code", "period", "open", "high", "low",
                "close", "volume", "turnover", "open_interest"]).issubset(df.columns)


# ---------- describe + lineage ----------

def test_describe_returns_per_dataset_lineage(tmp_path):
    from research.adapter import open_research_view
    view = open_research_view(str(_make_view_dir(tmp_path)))
    info = view.describe("SHFE.rb.HOT", "day")
    assert info["std_code"] == "SHFE.rb.HOT"
    assert info["period"] == "day"
    assert info["source_csv"].endswith("SHFE.rb.HOT_d.csv")
    assert info["derived_csv"].endswith("SHFE.rb.HOT_day_research.csv")


def test_top_level_lineage_exposes_inputs(tmp_path):
    from research.adapter import open_research_view
    view = open_research_view(str(_make_view_dir(tmp_path)))
    lineage = view.lineage
    assert lineage["authority_source"] == "wt_storage_converted_outputs"
    assert lineage["derived_view_only"] is True
    inputs = lineage["inputs"]
    assert inputs["conversion_manifest"].endswith("conversion_manifest.json")
    assert inputs["quality_report"].endswith("quality_report.json")
    assert "csv_dir" in inputs and "basefiles" in inputs


# ---------- read-only contract ----------

def test_adapter_does_not_write_anything(tmp_path):
    from research.adapter import open_research_view
    research = _make_view_dir(tmp_path)
    snapshot_before = {p: p.stat().st_mtime for p in research.rglob("*") if p.is_file()}
    view = open_research_view(str(research))
    _ = view.list_datasets()
    _ = view.load("SHFE.rb.HOT", "day")
    _ = view.describe("SHFE.rb.HOT", "min5")
    snapshot_after = {p: p.stat().st_mtime for p in research.rglob("*") if p.is_file()}
    assert snapshot_before == snapshot_after
    assert not (research / "_adapter_cache").exists()
