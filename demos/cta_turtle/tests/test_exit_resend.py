"""出场批次重发回归 (2026-11-16 p 裸奔 2 天事故)。

事故链: 周五 23:00 收盘根止损单被柜台拒 → 批次挂起跨重启无人重发
→ 周一全天零订单空头裸奔。修复: _process_exit_orders 在交易时段内
节流重发 target=0 (信号仓未归零才发, 幂等)。
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from StraTurtle import StraTurtle                          # noqa: E402
from schemas import InstrumentConfig, TurtleBaseConfig     # noqa: E402
from core.RiskManager import RiskConfig                    # noqa: E402


class _Ctx:
    is_backtest = False

    def __init__(self, pos=-116):
        self.pos = pos
        self.orders = []
        self.logs = []
        self.kv = {}

    def stra_get_position(self, code):
        return self.pos

    def stra_set_position(self, code, target, tag=""):
        self.orders.append((code, target, tag))
        self.pos = target

    def stra_log_text(self, txt, level=1):
        self.logs.append((level, txt))

    def user_save_data(self, key, data, flag=""):
        self.kv[key] = data


def _stra_with_exit_batch():
    base = TurtleBaseConfig(entry_mode="close")
    inst = InstrumentConfig(code="DCE.p.HOT", units=1)
    s = StraTurtle("t", inst, base, RiskConfig(), notify=None)
    s._truth = types.SimpleNamespace(is_stale=lambda: True,
                                     net_position=lambda c: None,
                                     age=lambda: None, path="",
                                     ok=False, positions=lambda: None)
    e = s._engine
    e.adopt_position(-1, 116, 9069.0, n=80.0)
    s._last_n = 80.0
    ctx = _Ctx()
    ok = s._begin_exit_order(ctx, "S1", e,
                             {"decision": "exit", "reason": "stop",
                              "price": 9069.0})
    assert ok and s._exit_orders
    ctx.orders.clear()               # 忽略挂批次时的初始下单
    ctx.pos = -116                   # 模拟目标未落地 (重启后信号复位)
    return s, ctx


def test_exit_batch_resent_in_session():
    s, ctx = _stra_with_exit_batch()
    s._fills._market_open_now = lambda code: True
    s._fills.process_exit_orders(ctx)
    assert any(t[1] == 0 for t in ctx.orders), "时段内应重发 target 0"
    assert any("resent" in t for _, t in ctx.logs)


def test_exit_batch_no_resend_out_of_session():
    s, ctx = _stra_with_exit_batch()
    s._fills._market_open_now = lambda code: False
    s._fills.process_exit_orders(ctx)
    assert not ctx.orders, "非交易时段重发只会被柜台拒 (11/13 p 实锤)"


def test_exit_batch_resend_throttled():
    s, ctx = _stra_with_exit_batch()
    s._fills._market_open_now = lambda code: True
    s._fills.process_exit_orders(ctx)
    n = len(ctx.orders)
    s._fills.process_exit_orders(ctx)      # 60s 节流内不得重复
    assert len(ctx.orders) == n
