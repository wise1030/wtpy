"""Walk-forward 纯函数测试 (网格/窗口/目标函数/配置合并)"""
import os

import pytest
import yaml

from conftest import *  # noqa: F401,F403
from run_walkforward import (parse_grid, iter_windows, objective,
                             merge_base_config, _add_months, _prev_month)


# ---------- 网格 ----------

def test_parse_grid_cartesian():
    combos = parse_grid({"a": [1, 2], "b": ["x", "y"]})
    assert len(combos) == 4
    assert {"a": 1, "b": "x"} in combos
    assert {"a": 2, "b": "y"} in combos


def test_parse_grid_empty_and_errors():
    assert parse_grid({}) == [{}]
    with pytest.raises(ValueError, match="非空列表"):
        parse_grid({"a": []})
    with pytest.raises(ValueError, match="非空列表"):
        parse_grid({"a": 3})


# ---------- 月度工具与滚动窗 ----------

def test_month_helpers():
    assert _add_months(2020, 1, 13) == (2021, 2)
    assert _add_months(2020, 11, 3) == (2021, 2)
    assert _prev_month(2020, 1) == (2019, 12)


def test_iter_windows_basic_rolling():
    wins = iter_windows(201901010900, 202101010900,
                        train_months=12, test_months=6)
    # 起点19-01: ①训练19-01~19-12/验证20-01~20-06 ②步进6月
    # ③验证21-01起被 end 截断退化 → 共2个有效窗
    assert len(wins) == 2
    s, e = wins[0]["train"]
    assert (s, e) == (201901010900, 201912010900)
    s, e = wins[0]["test"]
    assert (s, e) == (202001010900, 202006010900)
    # 第二窗起点前移一个 test 步长
    s2, _ = wins[1]["train"]
    assert s2 == 201907010900


def test_iter_windows_truncated_by_end():
    wins = iter_windows(202001010900, 202007150900,
                        train_months=6, test_months=6)
    # 验证窗起点 2020-07 ≤ end → 1窗; 其验证止于 min(2020-12, end)
    assert len(wins) >= 1
    ts, te = wins[0]["test"]
    assert ts == 202007010900 and te <= 202007150900


def test_iter_windows_invalid_range():
    assert iter_windows(202101010900, 202001010900, 12, 6) == []


# ---------- 目标函数 ----------

def test_objective_variants():
    m = {"annual_return": 0.2, "max_falldown": 10.0,
         "sharpe_ratio": 1.5}
    assert objective(m, "sharpe") == 1.5
    assert abs(objective(m, "return_over_dd") - 0.02) < 1e-9
    assert objective({}, "sharpe") == float("-inf")
    with pytest.raises(ValueError):
        objective(m, "nope")


# ---------- 配置合并 ----------

def test_merge_base_config(tmp_path):
    src = tmp_path / "cfg.yaml"
    src.write_text(
        "base:\n  unit_var: 200000\n  stop_atr_mult: 2.0\n"
        "instruments:\n"
        "  - code: SHFE.rb.HOT\n    system: S1\n"
        "  - code: DCE.i.HOT\n    system: S2\n",
        encoding="utf-8")
    dst = tmp_path / "combo.yaml"
    merge_base_config(str(src), {"stop_atr_mult": 2.5}, str(dst),
                      code="SHFE.rb.HOT")
    with open(dst, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    assert raw["base"]["stop_atr_mult"] == 2.5
    assert raw["base"]["unit_var"] == 200000          # 未覆盖项保留
    assert [i["code"] for i in raw["instruments"]] == \
        ["SHFE.rb.HOT"]                               # 单品种聚焦
