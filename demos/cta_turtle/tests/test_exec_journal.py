"""执行层订单状态机/归因纯逻辑测试 (2026-10-21 实验)"""
import pytest
from conftest import *  # noqa: F401,F403
from core.ExecJournal import OrderTracker, ExecAttributor


def _ord(localid, ts, total=10, left=10, traded=0, canceled=False,
         state="已报", code="SHFE.rb", islong=True, isopen=True):
    return {"trader": "simnow", "time": ts, "localid": localid,
            "code": code, "islong": islong, "isopen": isopen,
            "istoday": False, "canceled": canceled, "total": total,
            "left": left, "traded": traded, "price": 3500.0,
            "state": state}


def _trd(localid, ts, vol=4, price=3501.0, code="SHFE.rb"):
    return {"trader": "simnow", "time": ts, "localid": localid,
            "code": code, "islong": True, "isopen": True, "istoday": False,
            "volume": vol, "price": price}


class TestOrderTracker:
    def test_lifecycle_transitions(self):
        t = OrderTracker()
        r1 = t.on_order(_ord(1, 1000))
        assert r1["to"] == "alive"
        r2 = t.on_order(_ord(1, 1001, left=6, traded=4, state="部成"))
        assert r2["to"] == "partial"
        r3 = t.on_order(_ord(1, 1002, left=0, traded=10, state="全部成交"))
        assert r3["to"] == "filled"
        r4 = t.on_order(_ord(2, 1003, canceled=True, state="已撤"))
        assert r4["to"] == "canceled"
        st = t.stats()
        assert st["orders"] == 2
        assert st["by_state"]["filled"] == 1
        assert st["by_state"]["canceled"] == 1
        # 单2 已撤无成交 → (1.0+0.0)/2
        assert st["avg_fill_ratio"] == 0.5

    def test_duplicate_events_deduped(self):
        """总线 at-least-once: 完全相同的连续事件只计一次"""
        t = OrderTracker()
        assert t.on_order(_ord(1, 1000)) is not None
        assert t.on_order(_ord(1, 1000)) is None      # 重发
        assert t.on_trade(_trd(1, 1001, vol=4)) is not None
        assert t.on_trade(_trd(1, 1001, vol=4)) is None
        o = t.orders[("simnow", 1)]
        assert len(o["trades"]) == 1

    def test_trade_before_order_stubbed(self):
        """订单事件缺失 (侧车启动前已挂的单): stub 兜底不丢成交"""
        t = OrderTracker()
        t.on_trade(_trd(9, 500, vol=3, price=3499.0))
        snap = t.snapshot()
        row = [r for r in snap if r["localid"] == 9][0]
        assert row["unknown"] is True
        assert row["trade_cnt"] == 1

    def test_partial_fill_ratio(self):
        t = OrderTracker()
        t.on_order(_ord(1, 1000, total=10, left=4, traded=6, state="部成"))
        t.on_order(_ord(1, 1001, canceled=True, state="已撤", left=4,
                        traded=6))
        assert t.stats()["by_state"]["canceled"] == 1
        assert t.stats()["avg_fill_ratio"] == 0.6


class TestExecAttributor:
    def test_match_and_slippage_buy(self):
        t = OrderTracker()
        t.on_order(_ord(1, 2000, total=10, left=0, traded=10,
                        state="全部成交"))
        t.on_trade(_trd(1, 2001, vol=6, price=3502.0))
        t.on_trade(_trd(1, 2002, vol=4, price=3504.0))   # vwap=3502.8
        a = ExecAttributor(window_secs=600)
        a.record_decision("enter_rb", "SHFE.rb", True, True, 3500.0, 1900)
        rows = a.attribute(t)
        assert len(rows) == 1
        r = rows[0]
        assert r["decision_tag"] == "enter_rb"
        assert r["vwap"] == pytest.approx(3502.8)
        assert r["slippage"] == pytest.approx(2.8)     # 买入: vwap-dec
        assert r["fill_ratio"] == 1.0
        assert r["elapsed"] == 100

    def test_sell_side_slippage_sign(self):
        """平多=卖出: 滑点 = dec - vwap, 卖低为不利"""
        t = OrderTracker()
        t.on_order(_ord(1, 2000, total=4, left=0, traded=4,
                        state="全部成交", isopen=False))
        t.on_trade(_trd(1, 2001, vol=4, price=3496.0))
        a = ExecAttributor()
        a.record_decision("exit_rb", "SHFE.rb", True, False, 3500.0, 1900)
        r = a.attribute(t)[0]
        assert r["slippage"] == pytest.approx(4.0)     # 3500-3496

    def test_window_and_direction_mismatch(self):
        t = OrderTracker()
        t.on_order(_ord(1, 9000, total=2, left=0, traded=2,
                        state="全部成交"))
        t.on_trade(_trd(1, 9001, vol=2))
        a = ExecAttributor(window_secs=100)
        # 决策在窗口外
        a.record_decision("old", "SHFE.rb", True, True, 3500.0, 1000)
        # 方向不匹配 (空头开仓)
        a.record_decision("short", "SHFE.rb", False, True, 3500.0, 8900)
        rows = a.attribute(t)
        assert rows and "decision_tag" not in rows[0]
        # 窗口内且匹配 → 归因成功
        a2 = ExecAttributor(window_secs=100)
        a2.record_decision("hit", "SHFE.rb", True, True, 3500.0, 8950)
        rows = a2.attribute(t)
        assert rows[0]["decision_tag"] == "hit"
