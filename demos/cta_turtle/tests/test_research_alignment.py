# -*- coding: utf-8 -*-
"""研究结果的共同窗口与交易日对齐。"""
import json

import numpy as np
import pytest

from research.run_h1_mix import _clip_result
from research.run_speed_study import blend_dated_series


def test_h1_result_clips_to_shared_days_without_tail_freeze():
    res = {"daily": [{"date": 1, "equity": 90}, {"date": 2, "equity": 100},
                     {"date": 3, "equity": 110}, {"date": 4, "equity": 120}],
           "eval_start": 1, "eval_end": 4, "n_days": 4}
    out = _clip_result(res, [2, 3])
    assert [r["date"] for r in out["daily"]] == [2, 3]
    assert out["eval_start"] == 2 and out["eval_end"] == 3
    assert out["n_days"] == 2
    assert res["n_days"] == 4                  # 原结果不被原地修改


def test_speed_blend_aligns_by_date_and_excludes_expired_series():
    per_instr = {
        "A": {"20/10": np.array([1.0, 2.0, 3.0]),
              "55/20": np.array([2.0, 4.0, 6.0])},
        "B": {"20/10": np.array([20.0, 30.0, 40.0]),
              "55/20": np.array([10.0, 20.0, 30.0])},
    }
    dates = {"A": np.array([1, 2, 3]), "B": np.array([2, 3, 4])}
    got = blend_dated_series(per_instr, dates, ["20/10"])
    # day1 仅 A=1；day2=(2+20)/2；day3=(3+30)/2；day4 仅 B=40。
    assert np.allclose(got, [1.0, 11.0, 16.5, 40.0])

    both = blend_dated_series(per_instr, dates, ["20/10", "55/20"])
    # 每个 code 先对速度平均，再按日期等权，不按数组尾长错位。
    assert np.allclose(both, [1.5, 9.0, 14.75, 35.0])


def test_caliber_aggregator_rejects_mismatched_spec(tmp_path, monkeypatch):
    import research.run_caliber_ab as cab
    out = tmp_path / "matrix_results"
    out.mkdir()
    monkeypatch.setattr(cab, "ROOT", str(tmp_path))
    monkeypatch.setattr(cab, "VARIANTS", {
        "close_baseline": ("", "base", ""),
        "trigger_baseline": ("trigger", "trigger", "")})

    def doc(config_sha):
        return {"config_sha256": config_sha, "capital": 60,
                "start": 1, "end": 2, "slippage_ticks": 1,
                "margin_usage": 0.6, "period": "d1", "codes": None,
                "priority": "strength", "max_units_total": 6,
                "max_units_per_group": 2, "entry_mode": "close",
                "constrained": {"metrics": {
                    "total_return": 0.1, "max_drawdown": 0.1,
                    "sharpe_ratio": 0.2}, "trades": 1, "rejected": 0}}

    (out / "portfolio_bt.json").write_text(json.dumps(doc("A")))
    (out / "portfolio_bt_trigger.json").write_text(json.dumps(doc("B")))
    with pytest.raises(SystemExit, match="异口径"):
        cab.main()


def test_portfolio_oos_window_validation():
    from research.run_portfolio_oos import validate_windows
    validate_windows(20190101, 20200101, 20200101, 20210101)
    with pytest.raises(ValueError, match="train_start"):
        validate_windows(20200101, 20190101, 20200101, 20210101)
    with pytest.raises(ValueError, match="train_start"):
        validate_windows(20190101, 20200101, 20190101, 20210101)
