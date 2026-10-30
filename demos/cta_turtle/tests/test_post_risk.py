# -*- coding: utf-8 -*-
"""加仓共同止损重锚后的 post-risk 预算测试。"""
import pytest

from core.RiskManager import RiskManager
from schemas import RiskConfig, InstrumentConfig, TurtleBaseConfig
from StraTurtle import StraTurtle


def _rm(total=0.25):
    return RiskManager(RiskConfig(
        fallback_capital=100.0,
        max_risk_per_mkt=1.0, max_risk_per_market=1.0,
        max_total_risk_frac=total, max_margin_usage=1.0,
        max_units_total=0, max_units_per_group=0))


def test_post_risk_rejects_add_that_would_breach_after_reanchor():
    rm = _rm()
    rm.on_rebuilt("X", 1, 1, 100.0, n=1.0, volscale=1.0,
                  stop_mult=2.0, risk=2.0)
    # 旧口径 2+20<=25 会放行；重锚后 15+20=35>25 必须否决。
    old, _ = rm.cap_entry("X", lots=1, n=10.0, volscale=1.0,
                          price=100.5, stop_mult=2.0, equity=100.0)
    assert old == 1
    new, reasons = rm.cap_entry(
        "X", lots=1, n=10.0, volscale=1.0, price=100.5,
        stop_mult=2.0, equity=100.0,
        post_risk_base=15.0, post_risk_per_lot=20.0)
    assert new == 0 and any("total_risk" in r for r in reasons)


def test_post_risk_scales_to_exact_remaining_capacity():
    rm = _rm(total=0.40)
    rm.on_rebuilt("X", 1, 1, 100.0, n=1.0, volscale=1.0,
                  stop_mult=2.0, risk=2.0)
    lots, _ = rm.cap_entry(
        "X", lots=2, n=10.0, volscale=1.0, price=100.5,
        stop_mult=2.0, equity=100.0,
        post_risk_base=15.0, post_risk_per_lot=20.0)
    assert lots == 1                         # 40-15=25，仅够一层 20


def test_router_post_risk_matches_engine_after_confirm_add():
    stra = StraTurtle("post", InstrumentConfig(code="X", units=1),
                      TurtleBaseConfig(stop_n_mode="ratchet"),
                      risk_mgr=_rm())
    stra._volscale = 1.0
    e = stra._engine
    e.confirm_enter(1, 1, 100.0, n=10.0)
    e.update_n(1.0)
    assert e.stop_price() == pytest.approx(98.0)  # 棘轮已收紧
    e.update_n(10.0)                              # 加仓位仍不放宽

    base = stra._router.post_add_risk_base(stra._subs[0], 100.5, 10.0)
    assert base == pytest.approx(19.5)            # 新 stop=80.5
    e.confirm_add(1, 100.5, n=10.0)
    actual = e.booked_risk(1.0, stop=e.stop_anchor())
    assert actual == pytest.approx(39.5)          # base19.5 + 新层20
    assert actual - 20.0 == pytest.approx(base)
