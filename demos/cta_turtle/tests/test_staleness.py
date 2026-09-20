"""数据时效断言测试 (P1-4)

2026-09-01→16 HOT 日线冻结事故: check_config 的存在性/深度/倒置
检查全部无感, 16 天陈旧数据直接进评估窗口。本检查按日历推导
"预期最近交易日" (跳过周末+holidays.json), 滞后即 warn。
"""
import os

from helpers.DataChecker import (check_staleness, expected_trade_date,
                                 load_holidays)
from schemas import TurtleConfig, InstrumentConfig


def _cfg(code="SHFE.rb.HOT"):
    cfg = TurtleConfig()
    cfg.instruments = [InstrumentConfig(code=code)]
    return cfg


def _mk(tmp_path, code, rows, holidays=None):
    """构造 storage/csv/<code>_d.csv + common/holidays.json"""
    common = tmp_path / "common"
    common.mkdir()
    hol = {"CHINA": holidays or []}
    (common / "holidays.json").write_text(__import__("json").dumps(hol))
    csvdir = tmp_path / "storage" / "csv"
    csvdir.mkdir(parents=True)
    f = csvdir / f"{code}_d.csv"
    f.write_text("date,open,high,low,close\n" + rows)
    return str(common), str(tmp_path / "storage")


class TestExpectedTradeDate:
    def test_friday_baseline_is_thursday(self):
        """口径: 基准=严格早于今天的最近交易日 (周五当天 bar 收盘前
        不存在, 不应作为启动期断言基准)"""
        assert expected_trade_date(20260918, set()) == 20260917

    def test_monday_rolls_to_friday(self):
        assert expected_trade_date(20260921, set()) == 20260918

    def test_holiday_rolls_back(self):
        # 20261001 国庆节 (在 holidays 列表中) → 0930
        assert expected_trade_date(20261001, {"20261001"}) == 20260930

    def test_long_holiday_skips_weekend(self):
        # 20261005 周一, 国庆连休 (1001~1008 均在列表) → 0930
        hol = {"20261001", "20261002", "20261005", "20261006",
               "20261007", "20261008"}
        assert expected_trade_date(20261005, hol) == 20260930


class TestCheckStaleness:
    def test_fresh_data_ok(self, tmp_path):
        common, storage = _mk(tmp_path, "SHFE.rb.HOT",
                              "20260916,1,2,1,2\n20260917,1,2,1,2\n"
                              "20260918,1,2,1,2\n")
        issues = check_staleness(_cfg(), common, storage, today=20260918)
        assert any(i.level == "info" and "时效 OK" in i.msg for i in issues)
        assert not any(i.level == "warn" for i in issues)

    def test_stale_data_warns_with_lag(self, tmp_path):
        common, storage = _mk(tmp_path, "SHFE.rb.HOT",
                              "20260901,1,2,1,2\n20260902,1,2,1,2\n")
        issues = check_staleness(_cfg(), common, storage, today=20260918)
        warns = [i for i in issues if i.level == "warn"]
        assert len(warns) == 1
        assert "滞后" in warns[0].msg and "hotdaily" in warns[0].msg

    def test_weekend_not_stale(self, tmp_path):
        """周五数据在周日启动不算滞后"""
        common, storage = _mk(tmp_path, "SHFE.rb.HOT",
                              "20260918,1,2,1,2\n")
        issues = check_staleness(_cfg(), common, storage, today=20260920)
        assert not any(i.level == "warn" for i in issues)

    def test_missing_file_skips(self, tmp_path):
        common, storage = _mk(tmp_path, "SHFE.rb.HOT", "")
        os.remove(os.path.join(storage, "csv", "SHFE.rb.HOT_d.csv"))
        issues = check_staleness(_cfg(), common, storage, today=20260918)
        assert issues == []

    def test_load_holidays_missing_file(self, tmp_path):
        assert load_holidays(str(tmp_path)) == set()
