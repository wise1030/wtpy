"""黄金回测纯逻辑测试 (R1): 指纹采集与基线比对 (不跑真实回测)"""
import json
import os

from conftest import *  # noqa: F401,F403
from research.golden_bt import collect, diff_baseline, HASH_FILES


def _mk_out(tmp_path, total=35.58, signal="x"):
    d = tmp_path / "out"
    d.mkdir(parents=True)
    for name in HASH_FILES:
        (d / name).write_text(signal + name, encoding="utf-8")
    (d / "summary.json").write_text(json.dumps({
        "total_return": total, "annual_return": 5.9,
        "max_falldown": 20.66, "sharpe_ratio": 0.26, "days": 1275}),
        encoding="utf-8")
    return d


def test_collect_and_diff_consistent(tmp_path):
    d = _mk_out(tmp_path)
    cur = collect(str(d))
    assert all(cur["hashes"].values())
    assert cur["summary"]["days"] == 1275
    assert diff_baseline(cur, collect(str(d))) == []


def test_diff_detects_drift(tmp_path):
    base = collect(str(_mk_out(tmp_path / "a")))
    cur = collect(str(_mk_out(tmp_path / "b", signal="y")))
    diffs = diff_baseline(base, cur)
    assert len(diffs) == len(HASH_FILES)      # 全部哈希漂移
    assert any("signals.csv" in d for d in diffs)


def test_diff_detects_summary_metric_drift(tmp_path):
    base = collect(str(_mk_out(tmp_path / "a")))
    cur = collect(str(_mk_out(tmp_path / "b", total=99.0)))
    diffs = diff_baseline(base, cur)
    assert any("total_return" in d for d in diffs)
    assert not any(k in d for d in diffs for k in ("signals", "funds"))


def test_missing_file_recorded_as_none(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    cur = collect(str(d))
    assert all(v is None for v in cur["hashes"].values())
