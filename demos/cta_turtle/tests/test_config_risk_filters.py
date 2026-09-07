"""ConfigLoader 新配置段测试: filters / risk / group / 引擎开关"""
import os
import tempfile

import pytest

from conftest import *  # noqa: F401,F403
from helpers.ConfigLoader import ConfigLoader
from filters.builtins import AdxTrendFilter, VolatilityBandFilter


BASE = """
base:
  unit_var: 200000
  bar_cnt: 120
instruments:
  - code: SHFE.rb.HOT
    direction: long
    group: black
    filters:
      - type: AdxTrend
        params: {period: 14, threshold: 25}
      - type: VolatilityBand
        params: {window: 250, min_pct: 0.1, max_pct: 0.9}
"""


def _load(tmp, text):
    p = os.path.join(tmp, "cfg.yaml")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return ConfigLoader.load(p)


def test_filters_and_group_parsed():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _load(tmp, BASE)
    inst = cfg.instruments[0]
    assert inst.group == "black"
    assert [f.type for f in inst.filters] == \
        ["AdxTrend", "VolatilityBand"]
    assert inst.filters[0].params == {"period": 14, "threshold": 25}
    # 注册类型校验通过后可实例化
    f = AdxTrendFilter(dict(inst.filters[0].params))
    assert f.threshold == 25


def test_unknown_filter_type_rejected():
    bad = BASE.replace("AdxTrend", "MagicIndicator")
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ValueError, match="未知过滤器类型"):
            _load(tmp, bad)


def test_filters_must_be_list_of_typed_maps():
    bad = BASE.replace(
        "      - type: AdxTrend\n        params: {period: 14, threshold: 25}\n",
        "      - params: {a: 1}\n")
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ValueError, match="缺 type"):
            _load(tmp, bad)


def test_risk_section_parsed_and_validated():
    text = BASE + """
risk:
  max_risk_per_mkt: 0.03
  max_units_total: 5
"""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _load(tmp, text)
    assert cfg.risk.max_risk_per_mkt == 0.03
    assert cfg.risk.max_units_total == 5


def test_risk_unknown_field_rejected():
    bad = BASE + "risk:\n  typo: 1\n"
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ValueError, match="未知字段"):
            _load(tmp, bad)


def test_risk_out_of_range_rejected():
    bad = BASE + "risk:\n  max_margin_usage: 1.7\n"
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ValueError, match="max_margin_usage"):
            _load(tmp, bad)


def test_engine_switches_propagate():
    from StraTurtle import StraTurtle
    text = BASE + """
base_extra_never: remove_me
"""
    text = BASE.replace("bar_cnt: 120",
                        "bar_cnt: 120\n  enable_s1_filter: false\n"
                        "  recon_patience: 5")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _load(tmp, text)
    assert cfg.base.enable_s1_filter is False
    assert cfg.base.recon_patience == 5
    stra = StraTurtle("t", cfg.instruments[0], cfg.base,
                      risk_cfg=cfg.risk)
    assert stra._engine.enable_s1_filter is False
    assert stra._base.recon_patience == 5
    # 过滤链已装配: 隐式方向闸 + 2 个显式过滤器
    from filters.builtins import DirectionGateFilter
    assert [type(f) for f in stra._chain] == \
        [DirectionGateFilter, AdxTrendFilter, VolatilityBandFilter]
