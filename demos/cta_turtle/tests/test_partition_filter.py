"""his/rt 交易日分区过滤 + WTSBarStruct 字段名回归测试 (P1)

覆盖:
- CsvDataLoader: stop_tdate 滤掉 >= 当日行; m5 time 列解析; vol/money/hold
  正确写入结构体 (字段名误用曾被 ctypes 静默吞掉 → C++ 侧恒 0)
- convert_csv_to_dsb: --stop-tdate 分区截断; time 列解析 (纯函数路径)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "helpers"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from helpers.CsvDataLoader import (  # noqa: E402
    CsvDataLoader, _parse_hhmm, parse_csv_rows)
import convert_csv_to_dsb as conv  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture()
def csv_dir(tmp_path):
    d = tmp_path / "csv"
    d.mkdir()
    (d / "SHFE.rb.HOT_d.csv").write_text(
        "date,open,high,low,close,volume,turnover,open_interest\n"
        "2026/08/27,3400,3420,3390,3410,1000,3.4e6,5000\n"
        "2026/08/28,3410,3430,3400,3425,1100,3.7e6,5100\n"
        "2026/08/31,3425,3440,3420,3435,900,3.1e6,5200\n",
        encoding="utf-8")
    (d / "SHFE.rb.HOT_m5.csv").write_text(
        "date,time,open,high,low,close,volume,turnover,open_interest\n"
        "2026-08-28,09:05:00,3410,3415,3408,3412,100,3.4e5,5000\n"
        "2026-08-28,09:10:00,3412,3418,3410,3416,120,4.0e5,5010\n"
        "2026-08-31,09:05:00,3425,3428,3422,3426,90,3.1e5,5020\n",
        encoding="utf-8")
    return str(d)


# ------------------------------------------------------------------
# 纯函数: _parse_hhmm
# ------------------------------------------------------------------
def test_parse_hhmm_formats():
    assert _parse_hhmm("09:05:00") == 905
    assert _parse_hhmm("90500") == 905
    assert _parse_hhmm("1500") == 1500
    assert _parse_hhmm("21:30") == 2130
    assert _parse_hhmm("bad") == -1
    assert _parse_hhmm("25:00:00") == -1
    assert _parse_hhmm("09:60:00") == -1


# ------------------------------------------------------------------
# CsvDataLoader 分区过滤
# ------------------------------------------------------------------
def test_loader_stop_tdate_filters_today(csv_dir):
    rows = parse_csv_rows(os.path.join(csv_dir, "SHFE.rb.HOT_d.csv"),
                          "d", stop_tdate=20260831)
    assert [r[0] for r in rows] == [20260827, 20260828]   # 当日被滤
    rows_all = parse_csv_rows(os.path.join(csv_dir, "SHFE.rb.HOT_d.csv"),
                              "d")
    assert len(rows_all) == 3                              # 默认不过滤


def test_loader_m5_time_column_parsed(csv_dir):
    rows = parse_csv_rows(os.path.join(csv_dir, "SHFE.rb.HOT_m5.csv"),
                          "m5", stop_tdate=20260831)
    assert len(rows) == 2
    # time_int = (date-19900000)*10000 + HHMM
    assert rows[0][1] == (20260828 - 19900000) * 10000 + 905
    assert rows[1][1] == (20260828 - 19900000) * 10000 + 910


def test_loader_feeder_struct_fields(csv_dir):
    """字段名回归: vol/money/hold 必须进结构体内存 (非 python 属性)"""
    loader = CsvDataLoader(csv_dir, stop_tdate=20260831)
    captured = {}

    def feeder(ptr, count):
        captured["bars"] = [ptr[i] for i in range(count)]

    assert loader.load_final_his_bars("SHFE.rb.HOT", "d1", feeder)
    bars = captured["bars"]
    assert len(bars) == 2                                  # 分区过滤生效
    b = bars[0]
    assert b.date == 20260827
    assert b.time == (20260827 - 19900000) * 10000 + 1500  # 日线收盘时间
    assert b.open == 3400.0 and b.close == 3410.0
    assert b.vol == 1000.0                                 # 非0 → 字段名正确
    assert b.money == pytest.approx(3.4e6)
    assert b.hold == 5000.0


def test_loader_unknown_period_returns_false(csv_dir):
    loader = CsvDataLoader(csv_dir)
    assert loader.load_final_his_bars("SHFE.rb.HOT", "w1",
                                      lambda p, n: None) is False
    assert loader.load_final_his_bars("XX.none.HOT", "d1",
                                      lambda p, n: None) is False


# ------------------------------------------------------------------
# convert_csv_to_dsb 纯函数路径
# ------------------------------------------------------------------
def test_convert_iter_rows_partition(csv_dir):
    rows = conv.iter_csv_rows(os.path.join(csv_dir, "SHFE.rb.HOT_d.csv"),
                              stop_tdate=20260831)
    assert [r[0] for r in rows] == [20260827, 20260828]
    rows_all = conv.iter_csv_rows(
        os.path.join(csv_dir, "SHFE.rb.HOT_d.csv"))
    assert len(rows_all) == 3


def test_convert_iter_rows_m5_time(csv_dir):
    rows = conv.iter_csv_rows(os.path.join(csv_dir, "SHFE.rb.HOT_m5.csv"),
                              stop_tdate=20260831, is_min=True)
    assert len(rows) == 2
    assert rows[0][1] == (20260828 - 19900000) * 10000 + 905


def test_convert_parse_hhmm():
    assert conv.parse_hhmm("14:55:00") == 1455
    assert conv.parse_hhmm("") == -1
