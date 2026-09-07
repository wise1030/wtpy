"""RiskManager 组合风控测试

覆盖: 配置校验、权益回退、单品种/总敞口/保证金缩量、
市场单位限制(组内/全组合)、记账生命周期、对账重建。
"""
import pytest

from conftest import *  # noqa: F401,F403
from schemas import RiskConfig
from helpers.RiskManager import RiskManager


class CtxWithFund:
    def __init__(self, equity):
        self._eq = equity

    def stra_get_fund_data(self, flag=0):
        return self._eq


class CtxNoFund:
    pass


# ---------- 配置校验 ----------

def test_invalid_risk_config_rejected():
    with pytest.raises(ValueError, match="max_risk_per_mkt"):
        RiskManager(RiskConfig(max_risk_per_mkt=1.5))
    with pytest.raises(ValueError, match="margin_rate"):
        RiskManager(RiskConfig(margin_rate=0))
    with pytest.raises(ValueError, match="fallback_capital"):
        RiskManager(RiskConfig(fallback_capital=-1))


# ---------- 权益来源 ----------

def test_equity_dynamic_and_fallback():
    # stra_get_fund_data(0) = 净盈亏 (不含本金), equity = 基准 + 净盈亏
    rm = RiskManager()
    assert rm.equity(CtxWithFund(-800000)) == 4200000.0
    assert rm.last_equity_source == "dynamic"
    assert rm.equity(CtxNoFund()) == 5000000.0
    assert rm.last_equity_source == "fallback"
    assert rm.equity(CtxWithFund(-5)) == 5000000.0 - 5  # 负盈亏有效不回退
    assert rm.equity(CtxWithFund(float("nan"))) == 5000000.0  # NaN 回退


def test_equity_exception_falls_back():
    class Boom:
        def stra_get_fund_data(self, flag=0):
            raise RuntimeError("x")
    rm = RiskManager()
    assert rm.equity(Boom()) == 5000000.0


# ---------- 缩量/否决 ----------

def test_per_market_risk_cap():
    rm = RiskManager()
    # rpl = 84*10*2 = 1680; cap = floor(5e6*0.02/1680) = 59
    lots, reasons = rm.cap_entry("SHFE.rb.HOT", lots=1000, n=84,
                                 volscale=10, price=3900, stop_mult=2.0,
                                 equity=5_000_000)
    assert lots == 59 and any("per_market_risk" in r for r in reasons)


def test_total_risk_cap_after_existing_book():
    cfg = RiskConfig(max_units_total=0, max_units_per_group=0)
    rm = RiskManager(cfg)
    rm.on_filled("A", 1, 100, 100.0, n=50, volscale=10, stop_mult=2)
    # A 已占风险 100*50*10*2=100_000; 总预算 5e6*0.06=300_000 → 余 200_000
    lots, reasons = rm.cap_entry("B", lots=9999, n=40, volscale=8,
                                 price=200, stop_mult=2.0,
                                 equity=5_000_000)
    # rpl = 40*8*2=640; 单品种预算 floor(5e6*0.02/640)=156 (更紧),
    # 总敞口余量 cap=floor(200_000/640)=312 → 取 min=156
    assert lots == 156
    assert any("per_market_risk" in r for r in reasons)
    assert any("total_risk" in r for r in reasons)


def test_margin_cap_triggers():
    cfg = RiskConfig(max_margin_usage=0.10, max_units_total=0,
                     max_units_per_group=0)
    rm = RiskManager(cfg)
    # 单手保证金 = 4000*10*0.12 = 4800; 预算 5e6*0.10=500_000 → 104 手
    lots, reasons = rm.cap_entry("X", lots=500, n=20, volscale=10,
                                 price=4000, stop_mult=2.0,
                                 equity=5_000_000)
    assert lots == 104 and any("margin" in r for r in reasons)


def test_no_veto_when_within_budget():
    rm = RiskManager(RiskConfig(max_units_total=0, max_units_per_group=0))
    lots, reasons = rm.cap_entry("X", lots=10, n=10, volscale=10,
                                 price=3000, stop_mult=2.0,
                                 equity=5_000_000)
    assert lots == 10 and reasons == []


def test_group_unit_limit_vetoes_new_market_only():
    cfg = RiskConfig(max_units_per_group=1, max_units_total=4)
    rm = RiskManager(cfg)
    rm.on_filled("rb", 1, 10, 3000, n=10, volscale=10, stop_mult=2,
                 group="black")
    # 同组第二个新市场 → 整笔否决
    lots, reasons = rm.cap_entry("hc", lots=50, n=10, volscale=10,
                                 price=3000, stop_mult=2.0,
                                 equity=5_000_000, group="black")
    assert lots == 0 and any("group_units" in r for r in reasons)
    # 已有持仓加仓不受组限影响 (不改变单位数)
    lots, _ = rm.cap_entry("rb", lots=50, n=10, volscale=10,
                           price=3000, stop_mult=2.0,
                           equity=5_000_000, group="black")
    assert lots > 0


def test_total_unit_limit():
    cfg = RiskConfig(max_units_total=1, max_units_per_group=0)
    rm = RiskManager(cfg)
    rm.on_filled("A", 1, 10, 100, n=10, volscale=10, stop_mult=2)
    lots, reasons = rm.cap_entry("B", lots=10, n=10, volscale=10,
                                 price=100, stop_mult=2.0,
                                 equity=5_000_000)
    assert lots == 0 and any("total_units" in r for r in reasons)


def test_empty_group_skips_group_limit_but_respects_total():
    cfg = RiskConfig(max_units_per_group=1, max_units_total=2)
    rm = RiskManager(cfg)
    rm.on_filled("A", 1, 10, 100, n=10, volscale=10, stop_mult=2,
                 group="")
    # 空组不计组限, 但计入全组合单位数
    lots, reasons = rm.cap_entry("B", lots=10, n=10, volscale=10,
                                 price=100, stop_mult=2.0,
                                 equity=5_000_000, group="")
    assert lots == 10   # total=1 < 2 放行


# ---------- 记账生命周期 ----------

def test_on_filled_accumulates_and_closes():
    rm = RiskManager()
    rm.on_filled("X", 1, 100, 3400, n=30, volscale=10, stop_mult=2)
    e = rm._book["X"]
    assert e["lots"] == 100 and e["risk"] == 100 * 30 * 10 * 2
    rm.on_filled("X", 1, 60, 3500, n=32, volscale=10, stop_mult=2)
    e = rm._book["X"]
    assert e["lots"] == 160
    assert abs(e["price"] - (3400 * 100 + 3500 * 60) / 160) < 1e-9
    assert e["risk"] == 100 * 30 * 10 * 2 + 60 * 32 * 10 * 2
    rm.on_closed("X")
    assert not rm.has_position("X") and rm.open_risk() == 0


def test_on_filled_direction_flip_replaces_entry():
    rm = RiskManager()
    rm.on_filled("X", 1, 100, 3400, n=30, volscale=10, stop_mult=2)
    rm.on_filled("X", -1, 50, 3300, n=28, volscale=10, stop_mult=2)
    e = rm._book["X"]
    assert e["dir"] == -1 and e["lots"] == 50   # 反向视为新开替换


def test_on_rebuilt_overwrites_book():
    rm = RiskManager()
    rm.on_filled("X", 1, 500, 3400, n=30, volscale=10, stop_mult=2)
    rm.on_rebuilt("X", 1, 120, 3410, n=29, volscale=10, stop_mult=2)
    e = rm._book["X"]
    assert e["lots"] == 120 and e["risk"] == 120 * 29 * 10 * 2
    rm.on_rebuilt("X", 0, 0, 0, n=29, volscale=10, stop_mult=2)
    assert not rm.has_position("X")


def test_open_units_observers():
    rm = RiskManager()
    rm.on_filled("A", 1, 1, 1, n=1, volscale=1, stop_mult=1, group="g1")
    rm.on_filled("B", -1, 1, 1, n=1, volscale=1, stop_mult=1, group="g2")
    assert rm.open_units == 2
    assert rm.units_in_group("g1") == 1
    assert rm.units_in_group("") == 0
