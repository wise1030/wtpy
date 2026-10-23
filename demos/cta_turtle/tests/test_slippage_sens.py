"""滑点敏感性报告测试 — fallback 列 (P2-3) 与失败即中止语义"""
import csv
import json
import os
import sys

from conftest import *  # noqa: F401,F403
import run_slippage_sens as sens


def _patch_run_one(monkeypatch, tmp_path, fallback_by_slip):
    """伪造 run_one: 不落盘回测, 直接写 summary.json (按档位可选 fallback)"""
    def fake(code, slip, args):
        folder = tmp_path / f"slip{slip}"
        folder.mkdir(exist_ok=True)
        s = {"total_return": 1.0 * slip, "annual_return": 0.5,
             "max_falldown": 3.0, "sharpe_ratio": 0.4, "win_rate": 30.0}
        if slip in fallback_by_slip:
            s["fallback"] = fallback_by_slip[slip]
        with open(folder / "summary.json", "w", encoding="utf-8") as f:
            json.dump(s, f)
        return str(folder)
    monkeypatch.setattr(sens, "run_one", fake)
    monkeypatch.setattr(sens, "SENS_DIR", str(tmp_path))


def _run_main(monkeypatch, tmp_path, argv):
    monkeypatch.setattr(sys, "argv", argv)
    sens.main()


def test_report_has_fallback_column(tmp_path, monkeypatch):
    """正常 analyst 产出 (无 fallback 键) → False; 兜底 summary → True"""
    _patch_run_one(monkeypatch, tmp_path, {1: True})
    _run_main(monkeypatch, tmp_path,
              ["x", "--code", "SHFE.rb.HOT", "--slippages", "0,1"])
    with open(tmp_path / "sens_report.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [r["slippage_ticks"] for r in rows] == ["0", "1"]
    assert rows[0]["fallback"] == "False"   # 正常产出显式标记
    assert rows[1]["fallback"] == "True"    # funds.csv 兜底可见 (win_rate 不可比)


def test_report_all_failed_aborts(tmp_path, monkeypatch):
    """全部档位失败 → SystemExit, 不写陈旧报告 (2026-10-20 fail-fast 口径)"""
    def boom(code, slip, args):
        raise RuntimeError("bt failed")
    monkeypatch.setattr(sens, "run_one", boom)
    monkeypatch.setattr(sens, "SENS_DIR", str(tmp_path))
    try:
        _run_main(monkeypatch, tmp_path,
                  ["x", "--code", "SHFE.rb.HOT", "--slippages", "0"])
    except SystemExit:
        pass
    else:
        raise AssertionError("全部失败应 SystemExit")
    assert not (tmp_path / "sens_report.csv").exists()
