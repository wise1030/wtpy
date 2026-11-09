"""hotsutil 纯函数回归 (Phase 1, 2026-11-10)。

口径要点: CZCE 3 位码 (年尾数+月, 跨十年消歧靠日期窗); 换月检测的
gap/OI 双信号与去重窗; parse_contracts 与 update_hot_daily 单源迁移。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.hotsutil import (detect_rolls, load_hots, parse_contracts,  # noqa: E402
                           sina_symbol, split_code, wt_contract,
                           yymm_range)


def test_split_code():
    assert split_code("SHFE.rb.HOT") == ("SHFE", "rb")
    assert split_code("CZCE.TA.1701") == ("CZCE", "TA")
    assert split_code("SHFE.rb.2701") == ("SHFE", "rb")
    assert split_code("bad") == (None, None)


def test_parse_contracts_last_rule_and_empty():
    hots = {"SHFE": {"rb": [{"date": 20260901, "to": "rb2610"},
                            {"date": 20261020, "to": "rb2701"}]},
            "DCE": {"m": []}, "INE": {}}
    out = parse_contracts(hots, ["SHFE.rb.HOT", "DCE.m.HOT", "INE.sc.HOT",
                                 "CFFEX.IF.HOT"])
    assert out["SHFE.rb.HOT"] == "rb2701"     # 取最后一条
    assert out["DCE.m.HOT"] is None           # 空规则
    assert out["INE.sc.HOT"] is None          # 缺键
    assert out["CFFEX.IF.HOT"] is None


def test_load_hots_missing(tmp_path):
    assert load_hots(str(tmp_path / "nope.json")) == {}


def test_contract_code_formats():
    assert wt_contract("SHFE", "rb", "2701") == "SHFE.rb.2701"
    assert wt_contract("CZCE", "TA", "701") == "CZCE.TA.701"
    assert sina_symbol("SHFE", "rb", "2701") == "RB2701"
    assert sina_symbol("DCE", "m", "2701") == "M2701"
    assert sina_symbol("CZCE", "TA", "2701") == "TA701"
    assert sina_symbol("CZCE", "TA", "701") == "TA701"


def test_yymm_range_cycle_and_monthly():
    rb = yymm_range("SHFE", "rb", 2026, 2027)
    assert "2601" in rb and "2605" in rb and "2610" in rb
    assert "2602" not in rb and len(rb) == 6
    cu = yymm_range("SHFE", "cu", 2026, 2026)
    assert len(cu) == 12 and "2606" in cu
    ta = yymm_range("CZCE", "TA", 2026, 2026)
    assert set(ta) == {"601", "605", "609"}


def _series(dates, opens, pcs, ois=None):
    return detect_rolls(dates, opens, pcs, ois)


def test_detect_rolls_gap_signal():
    dates = [20260101 + i for i in range(40)]
    pcs = [100.0] * 40
    opens = [100.0] * 40
    opens[20] = 112.0          # 12% 跳空 (换月)
    rolls = _series(dates, opens, pcs)
    assert len(rolls) == 1
    assert rolls[0]["date"] == dates[20]
    assert rolls[0]["reason"] == "gap"
    assert abs(rolls[0]["delta"] - 12.0) < 1e-9


def test_detect_rolls_oi_signal_small_gap():
    dates = [20260101 + i for i in range(40)]
    pcs = [3000.0] * 40
    opens = [3000.0] * 40
    ois = [100000.0] * 40
    ois[20] = 40000.0          # OI 骤降 60% (主力切换), 跳空微小
    opens[20] = 3012.0
    rolls = _series(dates, opens, pcs, ois)
    assert len(rolls) == 1 and rolls[0]["reason"] == "oi"


def test_detect_rolls_dedup_and_quiet():
    dates = [20260101 + i for i in range(40)]
    pcs = [100.0] * 40
    opens = [100.0] * 40
    opens[10] = 112.0
    opens[11] = 124.0          # 3 窗内第二个触发 → 去重
    rolls = _series(dates, opens, pcs)
    assert len(rolls) == 1 and rolls[0]["date"] == dates[10]
    opens2 = [100.0] * 40
    assert _series(dates, opens2, pcs) == []     # 平静序列无触发
