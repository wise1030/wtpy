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
