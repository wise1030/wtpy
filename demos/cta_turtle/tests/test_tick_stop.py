"""close 模式 tick 止损扫描回归 (2026-11-14, CZCE d1 副K 坑的配套)。

CZCE 三品种 intrabar 模式的 d1 副K在实盘 reader 读取失败 → 回退
daily 模式, 盘中 2N 保护由 _tick_stop_scan (tick 触碰) 承载。
本组测试钉死: 触碰挂批次 / 防重复 / 空持仓与 NaN 守卫 / 价格
正常不触发。
"""
import os
import sys
import types

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from StraTurtle import StraTurtle                          # noqa: E402
from schemas import InstrumentConfig, TurtleBaseConfig     # noqa: E402
from core.RiskManager import RiskConfig                    # noqa: E402


class _Ctx:
    """最小 on_tick 上下文: 出场批次提交 + 日志。"""

    is_backtest = False

    def __init__(self):
        self.pos = -100
        self.orders = []
        self.logs = []
        self.kv = {}

    def stra_get_position(self, code):
        return self.pos

    def stra_set_position(self, code, target, tag=""):
        self.orders.append((code, target, tag))

    def stra_log_text(self, txt, level=1):
        self.logs.append((level, txt))

    def stra_save_user_data(self, key, data, flag):
        self.kv[key] = data

    def user_save_data(self, key, data, flag=""):
        self.kv[key] = data


def _stra_close():
    base = TurtleBaseConfig(entry_mode="close")
    inst = InstrumentConfig(code="CZCE.TA.HOT", units=1)
    s = StraTurtle("t", inst, base, RiskConfig(), notify=None)
    s._truth = types.SimpleNamespace(is_stale=lambda: True,
                                     net_position=lambda c: None,
                                     age=lambda: None, path="",
                                     ok=False,
                                     positions=lambda: None)
    return s


def _arm_short(s, layers=1):
    e = s._engine
    # 正规入口: 建层同时落 layer_ns/止损棘轮位 (直注 layers 会绕过)
    got = e.adopt_position(-1, 100, 6000.0, n=80.0)
    assert got == 100
    s._last_n = 80.0
    return e


def test_tick_stop_hit_short():
    s = _stra_close()
    e = _arm_short(s)
    stop = e.stop_price()
    assert stop > 6000            # 空头止损在上方 (6000+2N)
    ctx = _Ctx()
    tick = types.SimpleNamespace(price=stop + 5)
    s.on_tick(ctx, "CZCE.TA.HOT", tick)
    assert s._exit_orders, "触碰后应挂出场批次"
    assert any(t[1] == 0 for t in ctx.orders)   # 目标归零提交
    assert any("tick stop hit" in t for _, t in ctx.logs)
    assert any("stop_tick" in t for _, t in ctx.logs)


def test_tick_stop_no_duplicate():
    s = _stra_close()
    _arm_short(s)
    ctx = _Ctx()
    tick = types.SimpleNamespace(price=6200.0)
    s.on_tick(ctx, "CZCE.TA.HOT", tick)
    n_orders = len(ctx.orders)
    s.on_tick(ctx, "CZCE.TA.HOT", tick)     # 批次未确认, 不得重复
    assert len(ctx.orders) == n_orders


def test_tick_stop_not_hit_and_guards():
    s = _stra_close()
    _arm_short(s)
    ctx = _Ctx()
    # 价格在止损下方 (空头安全区)
    s.on_tick(ctx, "CZCE.TA.HOT", types.SimpleNamespace(price=5900.0))
    assert not s._exit_orders
    # 空持仓
    e2 = s._engine
    e2.direction = 0
    e2.layers = []
    ctx2 = _Ctx()
    s.on_tick(ctx2, "CZCE.TA.HOT", types.SimpleNamespace(price=99999.0))
    assert not s._exit_orders
    # 价格非法 (0)
    _arm_short(s)
    ctx3 = _Ctx()
    s.on_tick(ctx3, "CZCE.TA.HOT", types.SimpleNamespace(price=0.0))
    assert not s._exit_orders
    # NaN 止损 (脏状态: 棘轮位缺失 + 有效 N 不可用) → 跳过不误触发
    s2 = _stra_close()
    e4 = _arm_short(s2)
    e4._stop_level = None
    e4._n = None
    ctx4 = _Ctx()
    s2.on_tick(ctx4, "CZCE.TA.HOT", types.SimpleNamespace(price=99999.0))
    assert not s2._exit_orders
