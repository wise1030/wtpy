"""方向闸 + 执行层集成测试 (StraTurtle._execute 路径)

验证:
- 方向闸否决入场且不触碰引擎/S1过滤器状态
- 手数不足否决
- 否决日志去重
- implied_fill 从 avgpx 反推
- HOT 换月 re-anchor
"""
from unittest.mock import MagicMock

from conftest import *  # noqa: F401,F403
from schemas import InstrumentConfig, TurtleBaseConfig
from helpers.TurtleEngine import DEC_ENTER, DEC_EXIT


class FakeCtx:
    def __init__(self, pos=0, avgpx=0.0):
        self.pos = pos
        self.avgpx = avgpx
        self.orders = []
        self.logs = []
        self.kv = {}
        self.marks = []

    def stra_get_position(self, code, *a, **k):
        return self.pos

    def stra_set_position(self, code, target, tag=""):
        self.orders.append((code, target, tag))
        # 模拟按 3500 成交
        old_pos, old_avg = self.pos, self.avgpx
        self.pos = target
        if target == 0:
            self.avgpx = 0.0
        elif old_pos == 0:
            self.avgpx = 3500.0
        else:
            added = abs(target) - abs(old_pos)
            if added > 0:
                self.avgpx = (old_avg * abs(old_pos) + 3500.0 * added) \
                    / abs(target)

    def stra_get_position_avgpx(self, code=""):
        return self.avgpx

    def stra_log_text(self, text, level=1):
        self.logs.append((level, text))

    def user_save_data(self, key, val):
        self.kv[key] = val

    def add_chart_mark(self, price, icon, tag):
        self.marks.append((price, icon))


def _make_stra(direction="both", units=2):
    from StraTurtle import StraTurtle
    inst = InstrumentConfig(code="SHFE.rb.HOT", period="d1", system="S1",
                            units=units, direction=direction)
    base = TurtleBaseConfig()
    stra = StraTurtle.__new__(StraTurtle)
    from wtpy import BaseCtaStrategy
    BaseCtaStrategy.__init__(stra, "test_turtle")
    stra._inst = inst
    stra._base = base
    from helpers.TurtleEngine import TurtleEngine
    from helpers.PositionSizer import PositionSizer
    stra._engine = TurtleEngine(system="S1", stop_atr_mult=2.0,
                                pyramid_layers=4, pyramid_step_atr=0.5)
    stra._sizer = PositionSizer(base.unit_var)
    stra._volscale = 10.0
    stra._pricetick = 1.0
    stra._last_veto_key = None
    return stra


def test_direction_gate_blocks_short():
    """long_only 配置下空头信号被否决, 引擎状态不变(不过滤)"""
    stra = _make_stra(direction="long")
    ctx = FakeCtx()
    dec = {"decision": DEC_ENTER, "direction": -1, "price": 3400.0,
           "n": 30.0, "failsafe": False}
    stra._execute(ctx, dec, n=30.0)
    assert ctx.orders == []                      # 未下单
    assert stra._engine.direction == 0           # 引擎未提交
    assert stra._engine.s1_last is None          # 过滤器未受影响
    assert any("veto" in t for _, t in ctx.logs)


def test_direction_gate_allows_long():
    stra = _make_stra(direction="long")
    ctx = FakeCtx()
    dec = {"decision": DEC_ENTER, "direction": 1, "price": 3500.0,
           "n": 30.0, "failsafe": False}
    stra._execute(ctx, dec, n=30.0)
    assert ctx.orders == [("SHFE.rb.HOT", 1333, "Enter")]  # 400000/300=1333
    assert stra._engine.direction == 1
    assert stra._engine.layers == [[1333, 3500.0]]


def test_insufficient_lots_veto():
    """N 过大预算不足 1 手 → 否决 (units=2, 预算40万; N=5万×10=50万 > 40万)"""
    stra = _make_stra(direction="both")
    ctx = FakeCtx()
    dec = {"decision": DEC_ENTER, "direction": 1, "price": 3500.0,
           "n": 50000.0, "failsafe": False}
    stra._execute(ctx, dec, n=50000.0)
    assert ctx.orders == []
    assert stra._engine.direction == 0


def test_veto_log_dedup():
    """同类否决连续触发只记一次"""
    stra = _make_stra(direction="long")
    ctx = FakeCtx()
    dec = {"decision": DEC_ENTER, "direction": -1, "price": 3400.0,
           "n": 30.0, "failsafe": False}
    stra._execute(ctx, dec, n=30.0)
    stra._execute(ctx, dec, n=30.0)
    veto_logs = [t for _, t in ctx.logs if "veto" in t]
    assert len(veto_logs) == 1


def test_implied_fill_on_add():
    """加仓成交价 = avgpx 加权反推"""
    stra = _make_stra()
    ctx = FakeCtx(pos=100, avgpx=3400.0)
    stra._engine.confirm_enter(1, 100, 3400.0)
    # 直接测 _implied_fill: 加仓 100 手 @3500 → new_avg=3450, fill=3500
    ctx.stra_set_position("SHFE.rb.HOT", 200, "Add")
    fill = stra._implied_fill(ctx, prev_pos=100, prev_avg=3400.0,
                              fallback=9999.0)
    assert abs(fill - 3500.0) < 1e-9


def test_implied_fill_short_signed_avgpx():
    """框架 avgpx 空头为负(符号约定): 反推成交价仍为正价格

    复核发现: WT C++ 层 avgpx 带持仓方向符号 (avgpx×pos=成本>0),
    空头 avgpx=-3773, 若不做符号处理 re-anchor 会误判 -2×price 漂移。
    """
    stra = _make_stra()
    ctx = FakeCtx(pos=-552, avgpx=-3773.0)   # 空头: avgpx 负值
    fill = stra._implied_fill(ctx, prev_pos=0, prev_avg=0.0,
                              fallback=9999.0)
    assert abs(fill - 3773.0) < 1e-9


def test_reanchor_short_signed_avgpx_no_false_roll():
    """空头持仓: avgpx 符号不应触发假换月"""
    stra = _make_stra()
    stra._engine.confirm_enter(-1, 100, 3773.0)
    ctx = FakeCtx(pos=-100, avgpx=-3773.0)   # 空头负 avgpx, 无真实漂移
    stra._reanchor_on_roll(ctx)
    assert stra._engine.layers[0][1] == 3773.0   # 未被错误平移
    assert not any("roll" in t for _, t in ctx.logs)


def test_reanchor_on_hot_roll():
    """持仓不变但 avgpx 漂移(换月) → 层价格平移"""
    stra = _make_stra()
    stra._engine.confirm_enter(1, 100, 3400.0)
    stra._engine.confirm_add(100, 3500.0)
    # 框架侧换月: 持仓量不变, 均价升 50 (新合约升水)
    ctx = FakeCtx(pos=200, avgpx=3500.0)
    stra._reanchor_on_roll(ctx)
    assert stra._engine.layers[0][1] == 3450.0
    assert stra._engine.layers[1][1] == 3550.0
    assert any("roll" in t for _, t in ctx.logs)


def test_no_reanchor_when_position_mismatch():
    """持仓量与层数不符(部分成交) → 不 re-anchor"""
    stra = _make_stra()
    stra._engine.confirm_enter(1, 100, 3400.0)
    ctx = FakeCtx(pos=50, avgpx=3600.0)   # 量不符
    stra._reanchor_on_roll(ctx)
    assert stra._engine.layers[0][1] == 3400.0


def test_exit_executes_and_records_result():
    stra = _make_stra()
    ctx = FakeCtx(pos=100, avgpx=3400.0)
    stra._engine.confirm_enter(1, 100, 3400.0)
    dec = {"decision": DEC_EXIT, "reason": "stop", "price": 3340.0}
    stra._execute(ctx, dec, n=30.0)
    assert ctx.orders == [("SHFE.rb.HOT", 0, "Exit")]
    assert stra._engine.direction == 0
    assert stra._engine.s1_last["result"] == "loss"
