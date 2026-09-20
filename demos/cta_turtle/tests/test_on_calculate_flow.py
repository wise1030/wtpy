"""on_calculate 全链路集成测试

用合成 K 线驱动完整主流程: 指标计算 → 引擎决策 → 执行 → 图表 → 持久化,
以及 on_init 的孤儿持仓重建。
"""
import json

import numpy as np

from conftest import *  # noqa: F401,F403
from schemas import InstrumentConfig, TurtleBaseConfig


class FakeBars:
    def __init__(self, n=120, start=3400.0, drift=0.0, seed=7):
        rng = np.random.RandomState(seed)
        steps = rng.randn(n) * 8 + drift
        self.closes = start + np.cumsum(steps)
        self.highs = self.closes + np.abs(rng.randn(n)) * 4
        self.lows = self.closes - np.abs(rng.randn(n)) * 4

    def __len__(self):
        return len(self.closes)


class FullCtx:
    """覆盖 StraTurtle 全部框架调用面"""
    def __init__(self, bars):
        self.bars = bars
        self.pos = 0
        self.avgpx = 0.0
        self.orders = []
        self.prewarmed = []
        self.logs = []
        self.kv = {}
        self.indices = {}
        self.marks = []

    # 数据
    def stra_prepare_bars(self, code, period, cnt, isMain=False):
        self.prepared = (code, period, cnt, isMain)

    def stra_get_bars(self, code, period, cnt, isMain=False):
        return self.bars

    def stra_get_comminfo(self, code):
        m = type("Comm", (), {})()
        m.volscale = 10.0
        m.pricetick = 1.0
        return m

    # 持仓
    def stra_get_position(self, code, *a, **k):
        return self.pos

    def stra_set_position(self, code, target, tag=""):
        if tag == "PreWarm":
            self.prewarmed.append((code, target))
            return   # 执行单元预热 (F1): 非策略信号, 不计入订单
        self.orders.append((code, target, tag))
        old = self.pos
        self.pos = target
        if target == 0:
            self.avgpx = 0.0
        elif old == 0:
            self.avgpx = float(self.bars.closes[-1])
        else:
            added = abs(target) - abs(old)
            if added > 0:
                self.avgpx = (self.avgpx * abs(old)
                              + float(self.bars.closes[-1]) * added) / abs(target)

    def stra_get_position_avgpx(self, code=""):
        return self.avgpx

    # 图表
    def set_chart_kline(self, code, period):
        self.chart = (code, period)

    def register_index(self, name, idxType=1):
        pass

    def register_index_line(self, idx, line, lineType=0):
        pass

    def add_index_baseline(self, idx, line, val):
        pass

    def set_index_value(self, idx, line, val):
        self.indices[(idx, line)] = val

    def add_chart_mark(self, price, icon, tag):
        self.marks.append((price, icon))

    # 日志/持久化
    def stra_log_text(self, text, level=1):
        self.logs.append((level, text))

    def user_save_data(self, key, val):
        self.kv[key] = val

    def user_load_data(self, key, default=None, vtype=str):
        return self.kv.get(key, default)


def _make_stra(direction="both", units=2):
    from StraTurtle import StraTurtle
    inst = InstrumentConfig(code="SHFE.rb.HOT", period="d1", system="S1",
                            units=units, direction=direction)
    return StraTurtle("test_turtle", inst, TurtleBaseConfig())


def test_on_init_registers_and_prepares():
    stra = _make_stra()
    ctx = FullCtx(FakeBars())
    stra.on_init(ctx)
    assert ctx.prepared == ("SHFE.rb.HOT", "d1", 120, True)
    assert stra._volscale == 10.0
    assert any("inited" in t for _, t in ctx.logs)


def test_full_calculate_uptrend_enters():
    """持续上涨: 突破 20 通道触发入场, 状态持久化"""
    stra = _make_stra()
    bars = FakeBars(n=120, drift=3.0)   # 单边上行
    ctx = FullCtx(bars)
    stra.on_init(ctx)
    stra.on_calculate(ctx)
    # 应有多头入场 (方向闸 both, N 正常)
    assert ctx.pos > 0
    assert stra._engine.direction == 1
    assert any(k[1] == "position" for k in ctx.indices)   # 图表值已写
    assert ("test_turtle_engine" in ctx.kv)
    snap = json.loads(ctx.kv["test_turtle_engine"])
    if "v" in snap and snap["v"] in (3, 4):  # v3/v4: 子系统+去重键包装
        assert set(snap["subs"].keys()) == {"S1"}
        snap = snap["subs"]["S1"]
    elif "v" in snap and snap["v"] == 2:
        snap = snap["engine"]
    assert snap["direction"] == 1


def test_full_calculate_direction_gate_blocks():
    """short_only + 上涨突破 → 否决, 不下单"""
    stra = _make_stra(direction="short")
    bars = FakeBars(n=120, drift=3.0)
    ctx = FullCtx(bars)
    stra.on_init(ctx)
    stra.on_calculate(ctx)
    assert ctx.pos == 0
    assert stra._engine.direction == 0
    assert any("veto" in t for _, t in ctx.logs)


def test_on_init_adopts_orphan_position():
    """框架有持仓但引擎空仓 → 单层重建"""
    stra = _make_stra()
    ctx = FullCtx(FakeBars())
    ctx.pos = 50
    ctx.avgpx = 3450.0
    stra.on_init(ctx)
    assert stra._engine.direction == 1
    assert stra._engine.layers == [[50, 3450.0]]
    assert any("orphan" in t for _, t in ctx.logs)


def test_state_restore_from_kv():
    """持久化快照恢复"""
    stra = _make_stra()
    ctx = FullCtx(FakeBars())
    ctx.kv["test_turtle_engine"] = json.dumps({
        "system": "S1", "direction": 1, "layers": [[100, 3400.0]],
        "s1_last": None, "fs_armed": 0})
    stra.on_init(ctx)
    assert stra._engine.direction == 1
    assert stra._engine.layers == [[100, 3400.0]]
    assert any("restored" in t for _, t in ctx.logs)
