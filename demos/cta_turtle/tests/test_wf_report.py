"""wf_report.py 纯逻辑测试 (P2 walk-forward 分析面板)"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import wf_report as wr  # noqa: E402


def _sum_rows():
    return [
        {"window": "1", "params": '{"stop_atr_mult": 2.0}',
         "is_annual_return": "10.0", "oos_annual_return": "6.0",
         "is_max_falldown": "5.0", "oos_max_falldown": "8.0",
         "oos_win_rate": "30.0"},
        {"window": "2", "params": '{"stop_atr_mult": 2.0}',
         "is_annual_return": "12.0", "oos_annual_return": "9.0",
         "is_max_falldown": "4.0", "oos_max_falldown": "6.0",
         "oos_win_rate": "35.0"},
        {"window": "3", "params": '{"stop_atr_mult": 2.5}',
         "is_annual_return": "8.0", "oos_annual_return": "2.0",
         "is_max_falldown": "6.0", "oos_max_falldown": "12.0",
         "oos_win_rate": "25.0"},
    ]


def _grid_rows():
    rows = []
    for w in (1, 2):
        for mult in (1.5, 2.0, 2.5):
            for layers in (3, 4):
                rows.append({
                    "window": str(w),
                    "params": json.dumps({"stop_atr_mult": mult,
                                          "pyramid_layers": layers}),
                    "is_score": str(mult * layers + w)})
    return rows


def test_summary_stats():
    st = wr.summary_stats(_sum_rows())
    assert st["windows"] == 3
    assert abs(st["oos_ann_mean"] - (6 + 9 + 2) / 3) < 1e-9
    assert st["oos_dd_worst"] == 12.0
    assert abs(st["oos_win_mean"] - 30.0) < 1e-9
    # 衰减比: 0.6 / 0.75 / 0.25 → 中位 0.6
    assert abs(st["deg_median"] - 0.6) < 1e-9


def test_summary_stats_empty_and_guards():
    st = wr.summary_stats([])
    assert st["windows"] == 0 and st["oos_ann_mean"] is None
    # IS 年化为 0/负 的窗不参与衰减比
    rows = [{"is_annual_return": "0", "oos_annual_return": "5"}]
    assert wr.summary_stats(rows)["deg_median"] is None


def test_param_frequency():
    freq = wr.param_frequency(_sum_rows())
    assert freq[0] == ('{"stop_atr_mult": 2.0}', 2)
    assert freq[1] == ('{"stop_atr_mult": 2.5}', 1)


def test_heatmap_matrix_two_dims():
    hm = wr.heatmap_matrix(_grid_rows())
    assert hm["px"] == "stop_atr_mult"      # 不同取值最多(3)的维度优先
    assert hm["py"] == "pyramid_layers"
    assert hm["xs"] == [1.5, 2.0, 2.5]
    assert hm["ys"] == [3, 4]
    # (2.0, 3): 窗1=7.0, 窗2=8.0 → 7.5
    assert abs(hm["cells"][(2.0, 3)] - 7.5) < 1e-9
    assert hm["counts"][(2.0, 3)] == 2


def test_heatmap_matrix_single_param_degrades():
    rows = [{"params": json.dumps({"a": 1}), "is_score": "1.0"},
            {"params": json.dumps({"a": 2}), "is_score": "3.0"}]
    hm = wr.heatmap_matrix(rows)
    assert hm["px"] == "a" and hm["py"] is None
    assert hm["ys"] == ["-"]
    assert hm["cells"][(1, "-")] == 1.0


def test_heatmap_matrix_empty():
    hm = wr.heatmap_matrix([])
    assert hm["cells"] == {} and hm["xs"] == []


def test_cell_color_scale():
    mid = wr.cell_color(5, 0, 10)
    lo = wr.cell_color(0, 0, 10)
    hi = wr.cell_color(10, 0, 10)
    assert lo == "#d84a4a" and hi == "#3fae5a" and mid == "#555555"
    assert wr.cell_color(1, 1, 1) == "#555555"      # 等值 → 中性灰
    assert wr.cell_color(99, 0, 10) == hi           # 越界钳位


def test_render_html_smoke():
    doc = wr.render_html(_sum_rows(), _grid_rows(), title="T")
    assert "<html" in doc and "参数稳定性" in doc
    assert "参数热力图" in doc and "逐窗明细" in doc
    assert "IS→OOS 衰减比" in doc
    assert "stop_atr_mult" in doc                  # 热力图轴标签
    assert "#" in doc                              # 色阶着色存在


def test_render_html_no_grid():
    doc = wr.render_html(_sum_rows(), [])
    assert "无全网格数据" in doc


def test_load_rows_missing(tmp_path):
    assert wr.load_rows(str(tmp_path / "none.csv")) == []
