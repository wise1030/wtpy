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
    with pytest.raises(ValueError, match="account_gate_mode"):
        RiskManager(RiskConfig(account_gate_mode="strict"))
    with pytest.raises(ValueError, match="account_reserve_frac"):
        RiskManager(RiskConfig(account_reserve_frac=1.0))


# ---------- 账户可用资金闸门 (P1, 共享账户) ----------

def _cap(rm, lots=10, avail=None):
    return rm.cap_entry("SHFE.rb.HOT", lots=lots, n=50.0, volscale=10.0,
                        price=3500.0, stop_mult=2.0, equity=5_000_000.0,
                        group="black", avail=avail)


def test_account_gate_warn_only_notes_reason():
    """warn (默认): 记录原因但不改手数 — 先观察再切 veto"""
    rm = RiskManager(RiskConfig(account_gate_mode="warn",
                                account_reserve_frac=0.2))
    lots, reasons = _cap(rm, avail=1000.0)      # 可用资金远不够
    assert lots == 10                            # 手数不变
    assert any(r.startswith("account_margin(") for r in reasons)
    assert "affordable=0" in [r for r in reasons
                              if r.startswith("account_margin(")][0]


def test_account_gate_veto_scales_then_blocks():
    """veto: 缩量优先, 归零才否决 (reserve 安全垫之内不放行)"""
    rm = RiskManager(RiskConfig(account_gate_mode="veto",
                                account_reserve_frac=0.2))
    per_lot = 3500.0 * 10.0 * 0.12               # 单手保证金 = 4200
    avail = per_lot * 5 / 0.8                    # 扣 20% 安全垫后够 5 手
    lots, reasons = _cap(rm, avail=avail)
    assert lots == 5
    assert any(r.startswith("account_margin(") for r in reasons)
    lots0, reasons0 = _cap(rm, avail=per_lot * 0.5)   # 连安全垫都不够
    assert lots0 == 0
    assert any(r.startswith("account_margin(") for r in reasons0)


def test_account_gate_off_and_absent():
    """off 或未提供 avail (回测/真值缺失): 行为与升级前完全一致"""
    rm_off = RiskManager(RiskConfig(account_gate_mode="off"))
    lots, reasons = _cap(rm_off, avail=1.0)
    assert lots == 10 and reasons == []
    rm_warn = RiskManager(RiskConfig(account_gate_mode="warn"))
    lots2, reasons2 = _cap(rm_warn, avail=None)
    assert lots2 == 10 and reasons2 == []


def test_account_gate_ample_funds_no_reason():
    rm = RiskManager(RiskConfig(account_gate_mode="veto"))
    lots, reasons = _cap(rm, avail=50_000_000.0)
    assert lots == 10 and reasons == []


# ---------- 权益来源 ----------

def test_equity_dynamic_and_fallback():
    # stra_get_fund_data(0) = 净盈亏 (不含本金), equity = 基准 + 净盈亏
    rm = RiskManager()
    assert rm.equity(CtxWithFund(-800000)) == 14200000.0
    assert rm.last_equity_source == "dynamic"
    assert rm.equity(CtxNoFund()) == 15000000.0
    assert rm.last_equity_source == "fallback"
    assert rm.equity(CtxWithFund(-5)) == 15000000.0 - 5  # 负盈亏有效不回退
    assert rm.equity(CtxWithFund(float("nan"))) == 15000000.0  # NaN 回退


def test_equity_exception_falls_back():
    class Boom:
        def stra_get_fund_data(self, flag=0):
            raise RuntimeError("x")
    rm = RiskManager()
    assert rm.equity(Boom()) == 15000000.0


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


def test_margin_scoped_per_strategy_planB():
    """方案B (2026-10-08): 保证金扣减按 sid 收窄 — 他策略重仓不再
    占满本策略保证金预算 (旧全局口径实盘事故: CF 159 手满仓后全体
    12 策略 margin cap 恒 ≈0-2, al/MA/TA 入场被误杀、rb 661->2)。"""
    cfg = RiskConfig(max_margin_usage=0.10, max_units_total=0,
                     max_units_per_group=0)
    rm = RiskManager(cfg)
    # 策略 A (CF): 159 手 @16047.45 volscale=5 → 预估保证金 ≈153.1 万
    # > 15M×10%=150 万 → A 自身再加仓 margin cap=0 (自身额度用尽是正确行为)
    rm.on_rebuilt("CZCE.CF.HOT", -1, 159, 16047.45, 100.0, 5.0, 2.0,
                  sid="strat_A")
    assert rm._book["CZCE.CF.HOT"]["sid"] == "strat_A"
    lots, reasons = rm.cap_entry("CZCE.CF.HOT", lots=10, n=100, volscale=5,
                                 price=16000, stop_mult=2.0, equity=15e6,
                                 sid="strat_A")
    assert lots == 0 and any(r.startswith("margin(cap=0)") for r in reasons)
    # 策略 B (rb, 空仓): 单手保证金 3071*10*0.12=3685.2 → margin cap=407;
    # per_market 15M*2%/604.8=496。旧全局口径下 A 的 153 万会把 B 压到 0。
    lots, reasons = rm.cap_entry("SHFE.rb.HOT", lots=661, n=30.24,
                                 volscale=10, price=3071, stop_mult=2.0,
                                 equity=15e6, sid="strat_B")
    assert lots == 407
    assert any(r.startswith("margin(cap=407)") for r in reasons)
    # 组合总敞口仍是全局口径: A 的 risk 记账 (159*100*5*2=159000)
    # 计入 B 的 total_risk 扣减 (预算 900000, 剩余 741000 非约束)
    assert not any(r.startswith("total_risk") for r in reasons)
    # sync_position 的 sid 透传
    rm.sync_position("SHFE.rb.HOT", -1, 2, 3072.0, 30.24, 10.0, 2.0,
                     risk=1209.6, sid="strat_B")
    assert rm._book["SHFE.rb.HOT"]["sid"] == "strat_B"


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
