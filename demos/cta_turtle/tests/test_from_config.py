"""StraTurtle.from_config 装配测试 (2026-10-20)

此前零测试; 同时锁定回测资金口径: capital 必须覆盖
risk.fallback_capital, 否则报告分母与风控基数不一致。
"""
import os
import tempfile

import pytest

from conftest import *  # noqa: F401,F403
from StraTurtle import StraTurtle


YML = """
base:
  unit_var: 200000
  bar_cnt: 120
risk:
  fallback_capital: 15000000
instruments:
  - code: SHFE.rb.HOT
    period: d1
    system: S1
    units: 1
"""


def _write(tmp, text):
    p = os.path.join(tmp, "c.yaml")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


def test_from_config_overrides_risk_capital(tmp_path):
    p = _write(str(tmp_path), YML)
    stra = StraTurtle.from_config("t", "SHFE.rb.HOT", p,
                                  capital=5_000_000)
    assert stra._risk.cfg.fallback_capital == 5_000_000
    assert stra._sizer._unit_var == 200000


def test_from_config_without_override_keeps_config(tmp_path):
    p = _write(str(tmp_path), YML)
    stra = StraTurtle.from_config("t", "SHFE.rb.HOT", p)
    assert stra._risk.cfg.fallback_capital == 15_000_000


def test_from_config_invalid_capital(tmp_path):
    p = _write(str(tmp_path), YML)
    with pytest.raises(ValueError, match="capital"):
        StraTurtle.from_config("t", "SHFE.rb.HOT", p, capital=0)
