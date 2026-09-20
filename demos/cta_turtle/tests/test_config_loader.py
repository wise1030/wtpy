"""ConfigLoader 测试"""
import os
import tempfile

import pytest

from conftest import *  # noqa: F401,F403
from helpers.ConfigLoader import ConfigLoader

VALID_YAML = """
base:
  unit_var: 200000
  atr_period: 20
  pyramid_layers: 4
  bar_cnt: 120
instruments:
  - code: SHFE.rb.HOT
    period: d1
    system: S1
    units: 2
    direction: both
  - code: DCE.i.HOT
    period: h1
    system: S2
    units: 1
    direction: long
"""


def _write(tmp, text):
    p = os.path.join(tmp, "cfg.yaml")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


def test_load_valid():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = ConfigLoader.load(_write(tmp, VALID_YAML))
    assert cfg.base.unit_var == 200000
    assert len(cfg.instruments) == 2
    rb, i = cfg.instruments
    assert rb.code == "SHFE.rb.HOT" and rb.units == 2
    assert i.period == "h1" and i.system == "S2" and i.direction == "long"


def test_defaults_applied():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = ConfigLoader.load(_write(tmp, "instruments:\n  - code: SHFE.rb.HOT\n"))
    inst = cfg.instruments[0]
    assert inst.period == "d1" and inst.system == "S1"
    assert inst.units == 1 and inst.direction == "both"
    assert cfg.base.bar_cnt == 120


def test_invalid_direction_rejected():
    bad = VALID_YAML.replace("direction: long", "direction: sideways")
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ValueError, match="direction"):
            ConfigLoader.load(_write(tmp, bad))


def test_invalid_period_rejected():
    bad = VALID_YAML.replace("period: h1", "period: m5")
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ValueError, match="period"):
            ConfigLoader.load(_write(tmp, bad))


def test_invalid_system_rejected():
    bad = VALID_YAML.replace("system: S2", "system: S3")
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ValueError, match="system"):
            ConfigLoader.load(_write(tmp, bad))


def test_duplicate_code_rejected():
    bad = VALID_YAML + "  - code: SHFE.rb.HOT\n"
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ValueError, match="重复"):
            ConfigLoader.load(_write(tmp, bad))


def test_bar_cnt_too_small_rejected():
    bad = VALID_YAML.replace("bar_cnt: 120", "bar_cnt: 50")
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ValueError, match="bar_cnt"):
            ConfigLoader.load(_write(tmp, bad))


def test_unknown_base_field_rejected():
    bad = VALID_YAML.replace("unit_var: 200000",
                             "unit_var: 200000\n  typo_field: 1")
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ValueError, match="未知字段"):
            ConfigLoader.load(_write(tmp, bad))


def test_empty_instruments_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ValueError, match="instruments"):
            ConfigLoader.load(_write(tmp, "base: {}\n"))


def test_allows_direction():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = ConfigLoader.load(_write(tmp, VALID_YAML))
    rb, i = cfg.instruments
    assert rb.allows_direction(1) and rb.allows_direction(-1)
    assert i.allows_direction(1) and not i.allows_direction(-1)


# === P1-6 加固: 顶层/instruments 未知键拒绝 + units 整型校验 ===

def test_unknown_top_section_rejected():
    bad = VALID_YAML + "risks:\n  max_risk_per_mkt: 0.03\n"
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ValueError, match="未知顶层段"):
            ConfigLoader.load(_write(tmp, bad))


def test_unknown_instrument_field_rejected():
    """高频手改区 typo (untis/drection) 不再静默回默认 (P1-6)"""
    bad = VALID_YAML.replace("units: 2", "untis: 2")
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ValueError, match="未知字段"):
            ConfigLoader.load(_write(tmp, bad))


def test_fractional_units_rejected():
    """units=1.5 原被 int() 静默截断为 1 — 现拒绝"""
    bad = VALID_YAML.replace("units: 2", "units: 1.5")
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ValueError, match="units"):
            ConfigLoader.load(_write(tmp, bad))


def test_new_base_fields_have_defaults():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = ConfigLoader.load(_write(tmp, VALID_YAML))
    assert cfg.base.ghost_auto_trim is True
    assert cfg.base.limit_lock_secs == 120


def test_config_version_accepted_and_future_warns():
    ver = "config_version: 1\n" + VALID_YAML
    with tempfile.TemporaryDirectory() as tmp:
        cfg = ConfigLoader.load(_write(tmp, ver))
    assert cfg.base.entry_mode == "close"
    badver = "config_version: 99\n" + VALID_YAML
    with tempfile.TemporaryDirectory() as tmp:
        # 未来版本只告警不拒绝 (向后兼容读取)
        cfg = ConfigLoader.load(_write(tmp, badver))
    assert cfg is not None


# === 校验分支补齐 (覆盖率 P2-12): 每条约款都有正反两面测试 ===

def test_base_enum_and_range_validations():
    cases = [
        ("recon_patience: 0", "recon_patience"),
        ("intrabar_period: h4", "intrabar_period"),
        ("intrabar_cnt: 30", "intrabar_cnt"),
        ("entry_mode: weird", "entry_mode"),
        ("stop_n_mode: weird", "stop_n_mode"),
        ("truth_json: 123", "truth_json"),
        ("truth_max_age: -1", "truth_max_age"),
        ("truth_rebuild_mode: weird", "truth_rebuild_mode"),
        ("ghost_auto_trim: 1", "ghost_auto_trim"),
        ("limit_lock_secs: -5", "limit_lock_secs"),
    ]
    for frag, match in cases:
        bad = VALID_YAML.replace(
            "base:\n  unit_var: 200000",
            f"base:\n  unit_var: 200000\n  {frag}")
        with tempfile.TemporaryDirectory() as tmp:
            with pytest.raises(ValueError, match=match):
                ConfigLoader.load(_write(tmp, bad))


def test_risk_validation_ranges():
    bad = VALID_YAML + "risk:\n  max_risk_per_mkt: 1.5\n"
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ValueError, match="max_risk_per_mkt"):
            ConfigLoader.load(_write(tmp, bad))


def test_notify_type_and_unknown_rejected():
    bad = VALID_YAML + "notify:\n  type: carrier_pigeon\n"
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ValueError, match="notify"):
            ConfigLoader.load(_write(tmp, bad))
    bad2 = VALID_YAML + "notify:\n  smoke_signal: true\n"
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ValueError, match="未知字段"):
            ConfigLoader.load(_write(tmp, bad2))


_RB_FILTERS = """    units: 2
    direction: both
    filters:
      - type: DirectionGate
        params:
          direction: long
      - type: AdxTrend
        params:
          period: 14
          threshold: 25
"""


def test_filters_and_speeds_parsing():
    yaml_txt = VALID_YAML.replace("    units: 2\n    direction: both\n",
                                  _RB_FILTERS)
    with tempfile.TemporaryDirectory() as tmp:
        cfg = ConfigLoader.load(_write(tmp, yaml_txt))
    rb = cfg.instruments[0]
    assert len(rb.filters) == 2
    assert rb.filters[0].type == "DirectionGate"
    assert rb.filters[1].params.get("period") == 14


def test_speeds_share_sum_must_be_one():
    speeds = """    units: 2
    direction: both
    speeds:
      - name: V0
        entry: 20
        exit: 10
        share: 0.6
      - name: V1
        entry: 55
        exit: 20
        share: 0.3
"""
    yaml_txt = VALID_YAML.replace("    units: 2\n    direction: both\n",
                                  speeds)
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ValueError, match="share"):
            ConfigLoader.load(_write(tmp, yaml_txt))


def test_unknown_filter_type_rejected():
    bad_filter = """    units: 2
    direction: both
    filters:
      - type: no_such_filter
"""
    yaml_txt = VALID_YAML.replace("    units: 2\n    direction: both\n",
                                  bad_filter)
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ValueError, match="no_such_filter"):
            ConfigLoader.load(_write(tmp, yaml_txt))


def test_non_mapping_top_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ValueError, match="映射"):
            ConfigLoader.load(_write(tmp, "- just\n- a\n- list\n"))
