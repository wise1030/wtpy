"""runBT analyst 失败兜底测试 (2026-10-20)"""
import json

from conftest import *  # noqa: F401,F403
from runBT import _fallback_summary


def test_fallback_summary_from_funds(tmp_path):
    folder = tmp_path / "turtle_X"
    folder.mkdir()
    (folder / "funds.csv").write_text(
        "date,closeprofit,positionprofit,dynbalance,fee\n"
        "20230103,0,0,0,0\n"
        "20230104,10000,0,10000,0\n"
        "20230105,10000,10000,20000,0\n", encoding="utf-8")
    assert _fallback_summary("turtle_X", 1_000_000, str(folder))
    s = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    assert s["fallback"] is True
    assert s["total_return"] == 2.0
    assert s["days"] == 3
    assert s["name"] == "turtle_X"
    assert s["max_falldown"] >= 0.0


def test_fallback_summary_missing_or_short(tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()
    assert not _fallback_summary("x", 1_000_000, str(folder))
    (folder / "funds.csv").write_text("date,dynbalance\n20230103,0\n",
                                      encoding="utf-8")
    assert not _fallback_summary("x", 1_000_000, str(folder))
