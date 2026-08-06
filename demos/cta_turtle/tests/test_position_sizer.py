"""PositionSizer VaR 反算测试"""
from conftest import *  # noqa: F401,F403
from helpers.PositionSizer import PositionSizer


def test_layer_lots_basic():
    """lots = floor(units × unit_var / (N × volscale))"""
    s = PositionSizer(unit_var=200000)
    # N=30, volscale=10 → 1手1N风险=300; 2单位=40万 → floor(400000/300)=1333
    assert s.layer_lots(n=30.0, volscale=10.0, units=2) == 1333


def test_layer_lots_single_unit():
    s = PositionSizer(unit_var=200000)
    assert s.layer_lots(n=50.0, volscale=10.0, units=1) == 400


def test_layer_lots_floor():
    s = PositionSizer(unit_var=200000)
    # 200000/(33*10) = 606.06 → 606
    assert s.layer_lots(n=33.0, volscale=10.0, units=1) == 606


def test_layer_lots_zero_when_n_invalid():
    s = PositionSizer(unit_var=200000)
    assert s.layer_lots(n=0.0, volscale=10.0, units=1) == 0
    assert s.layer_lots(n=-5.0, volscale=10.0, units=1) == 0
    assert s.layer_lots(n=float('nan'), volscale=10.0, units=1) == 0
    assert s.layer_lots(n=None, volscale=10.0, units=1) == 0


def test_layer_lots_zero_when_budget_insufficient():
    """预算不足 1 手 → 0 (调用方放弃入场)"""
    s = PositionSizer(unit_var=200000)
    # N×volscale = 250000 > 200000 → 0 手
    assert s.layer_lots(n=25000.0, volscale=10.0, units=1) == 0


def test_layer_lots_zero_units():
    s = PositionSizer(unit_var=200000)
    assert s.layer_lots(n=30.0, volscale=10.0, units=0) == 0
