"""S1+S2 双系统并行测试

覆盖: 子系统装配/units 均分、净仓批量执行、独立出场(部分平仓)、
过滤链对双系统生效、共享风控预算、快照v3恢复、日线驱动两段入场。
"""
import json

import numpy as np

from conftest import *  # noqa: F401,F403
from StraTurtle import StraTurtle
from schemas import InstrumentConfig, TurtleBaseConfig
from helpers.TurtleEngine import DEC_ENTER, DEC_EXIT


class MiniCtx:
    def __init__(self, pos=0):
        self.pos = pos
        self.avgpx = 0.0
        self.orders = []
        self.logs = []

    def stra_get_position(self, code, *a, **k):
        return self.pos

    def stra_set_position(self, code, target, tag=""):
        self.orders.append((code, target, tag))
        self.pos = target
        if target == 0:
            self.avgpx = 0.0
        elif abs(target) > abs(self.avgpx * 0 or 0) and self.avgpx == 0:
            self.avgpx = 3500.0

    def stra_get_position_avgpx(self, code=""):
        return self.avgpx

    def stra_log_text(self, text, level=1):
        self.logs.append((level, text))

    def add_chart_mark(self, price, icon, tag):
        pass


def _mk_dual(units=2):
    inst = InstrumentConfig(code="SHFE.rb.HOT", period="d1",
                            system="S1+S2", units=units)
    stra = StraTurtle("td", inst, TurtleBaseConfig())
    stra._volscale = 10.0     # on_init 才从框架取, 直测路径手工注入
    stra._pricetick = 1.0
    return stra


# ---------- 装配 ----------

def test_config_accepts_dual():
    import os
    import tempfile
    from helpers.ConfigLoader import ConfigLoader
    yml = """
instruments:
  - code: SHFE.rb.HOT
    system: S1+S2
    units: 3
"""
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "c.yaml")
        with open(p, "w", encoding="utf-8") as f:
            f.write(yml)
        cfg = ConfigLoader.load(p)
    assert cfg.instruments[0].system == "S1+S2"


def test_subsystems_split_units():
    stra = _mk_dual(units=2)
    assert stra._is_dual
    assert [s["name"] for s in stra._subs] == ["S1", "S2"]
    assert [s["units"] for s in stra._subs] == [1, 1]   # ceil/floor 均分
    assert stra._subs[0]["engine"].system == "S1"
    assert stra._subs[1]["engine"].system == "S2"
    assert stra._engine is stra._subs[0]["engine"]      # 兼容别名=S1

    stra3 = _mk_dual(units=3)
    assert [s["units"] for s in stra3._subs] == [2, 1]


# ---------- 净仓批量执行 ----------

def test_dual_entries_combine_to_net_order():
    stra = _mk_dual(units=2)
    ctx = MiniCtx()
    stra._execute_dual(ctx, {
        "S1": [{"decision": DEC_ENTER, "direction": 1, "price": 3500.0,
                "n": 30.0, "failsafe": False}],
        "S2": [{"decision": DEC_ENTER, "direction": 1, "price": 3500.0,
                "n": 30.0, "failsafe": False}]}, n=30.0)
    # 单次下单: 合并目标 = S1手数 + S2手数 (各半量)
    lots1 = stra._sub_lots(stra._subs[0])
    lots2 = stra._sub_lots(stra._subs[1])
    assert lots1 > 0 and lots2 > 0
    assert ctx.orders == [("SHFE.rb.HOT", lots1 + lots2, "Dual")]
    # 风控账本按市场合并记账
    assert stra._risk.has_position("SHFE.rb.HOT")
    assert stra._risk._book["SHFE.rb.HOT"]["lots"] == lots1 + lots2


def test_dual_partial_exit_keeps_other_sub():
    """S1 止损离场而 S2 持仓 → 部分平仓而非清零"""
    stra = _mk_dual(units=2)
    e1, e2 = stra._subs[0]["engine"], stra._subs[1]["engine"]
    e1.confirm_enter(1, 100, 3500.0)
    e2.confirm_enter(1, 80, 3500.0)
    ctx = MiniCtx(pos=180)
    stra._execute_dual(ctx, {
        "S1": [{"decision": DEC_EXIT, "reason": "stop", "price": 3440.0}],
        "S2": []}, n=30.0)
    assert e1.direction == 0 and e2.direction == 1
    assert ctx.orders == [("SHFE.rb.HOT", 80, "Dual")]   # 净仓 80
    assert stra._risk._book["SHFE.rb.HOT"]["lots"] == 80


def test_dual_filter_chain_blocks_both_subs():
    stra = _mk_dual()
    from filters.base import SignalFilterBase, FilterContext

    class No(SignalFilterBase):
        name = "No"

        def _setup(self, p):
            pass

        def allow_entry(self, ctx):
            return False, "always"
    stra._chain.append(No())
    ctx = MiniCtx()
    stra._execute_dual(ctx, {
        "S1": [{"decision": DEC_ENTER, "direction": 1, "price": 3500.0,
                "n": 30.0}],
        "S2": [{"decision": DEC_ENTER, "direction": 1, "price": 3500.0,
                "n": 30.0}]}, n=30.0)
    assert ctx.orders == []
    assert all(s["engine"].direction == 0 for s in stra._subs)


def test_dual_shares_risk_budget_sequentially():
    """第二子系统入场受第一子已占用预算约束 → 手数更小或否决"""
    from schemas import RiskConfig
    inst = InstrumentConfig(code="X.Y.HOT", period="d1", system="S1+S2",
                            units=10)
    rcfg = RiskConfig(max_units_total=0, max_units_per_group=0)
    stra = StraTurtle("tb", inst, TurtleBaseConfig(), risk_cfg=rcfg)
    stra._volscale = 10.0
    ctx = MiniCtx()
    dec = {"decision": DEC_ENTER, "direction": 1, "price": 3500.0,
           "n": 100.0}
    stra._execute_dual(ctx, {"S1": [dec], "S2": [dict(dec)]}, n=100.0)
    l1 = stra._sub_lots(stra._subs[0])
    l2 = stra._sub_lots(stra._subs[1])
    # rpl=2000; 总预算 300_000 → 全组合 ≤150 手; S1 先占其半量份额后
    # S2 只能拿余量 (严格小于等量申请)
    assert l1 >= 1 and l1 <= 75
    assert l2 <= 150 - l1
    assert stra._risk.open_risk() <= 5_000_000 * 0.06 + 1e-6


def test_dual_snapshot_v3_roundtrip():
    stra = _mk_dual()
    stra._subs[0]["engine"].confirm_enter(1, 50, 3500.0)
    stra._subs[1]["engine"].confirm_enter(-1, 30, 3490.0)
    snap = {"v": 3,
            "subs": {s["name"]: s["engine"].snapshot()
                     for s in stra._subs},
            "d1_date": 20230105, "m5_key": 202301052105.0}
    blob = json.dumps(snap)
    stra2 = _mk_dual()

    class KV:
        raw = blob

        def user_load_data(self, k, d=None, v=str):
            return self.raw
    # 复用 on_init 恢复逻辑 (跳过图表/行情准备)
    data = json.loads(blob)
    for nm, sn in data["subs"].items():
        stra2._sub(nm)["engine"].restore(sn)
    stra2._d1_date = data["d1_date"]
    stra2._m5_key = data["m5_key"]
    assert stra2._subs[0]["engine"].direction == 1
    assert stra2._subs[0]["engine"].layers == [[50, 3500.0]]
    assert stra2._subs[1]["engine"].direction == -1
    assert stra2._combined_target() == 20


# ---------- 日线驱动集成 ----------

from test_intrabar import FakePeriodBars, MultiCtx, _make_stra  # noqa: E402


def _ramp_ctx(dual=True):
    inst = InstrumentConfig(code="SHFE.rb.HOT", period="d1",
                            system="S1+S2" if dual else "S1", units=2)
    stra = StraTurtle("tx", inst, TurtleBaseConfig())
    ctx = MultiCtx()
    d1 = ctx.add_store("d1")
    m5 = ctx.add_store("m5")
    px = 3400.0
    # 平整基底; index45 放尖峰 3500 (day90/91 时在55窗内、20窗外)
    for i in range(90):
        if i == 45:
            o = c = 3400.0
            h, l = 3500.0, 3398.0
        else:
            c = 3400.0 + (0.6 if i % 2 else -0.6)
            o = c
            h, l = c + 1.5, c - 1.5
        d1.append(20230000 + i, 1500, o, h, l, c)
        px = c
    m5.append(20230089, 2110, px, px + 1, px - 1, px)
    return stra, ctx, d1, m5


def test_daily_two_stage_entries_s1_then_s2():
    stra, ctx, d1, m5 = _ramp_ctx()
    stra.on_init(ctx)
    stra.on_calculate(ctx)                       # 基线
    # Day90: 收盘 3450 破 20 通道 (~3403), 未破 55 通道 (含 3500 尖峰)
    d1.append(20230090, 1500, 3449, 3452, 3448, 3450)
    m5.append(20230090, 2110, 3450, 3451, 3449, 3450)
    stra.on_calculate(ctx)
    e1 = stra._subs[0]["engine"]
    e2 = stra._subs[1]["engine"]
    assert e1.direction == 1, "S1 应入场"
    assert e2.direction == 0, "S2 的55通道未破不应入场"
    lots1 = stra._sub_lots(stra._subs[0])
    assert ctx.orders[-1][1] == lots1
    # Day91: 收盘 3520 破 55 通道 → S2 同向加入, 净仓单一次合并
    d1.append(20230091, 1500, 3519, 3522, 3518, 3520)
    m5.append(20230091, 2110, 3520, 3521, 3519, 3520)
    stra.on_calculate(ctx)
    assert e2.direction == 1
    total = stra._sub_lots(stra._subs[0]) + stra._sub_lots(stra._subs[1])
    assert ctx.pos == total
    tags = [t for t in ctx.orders if t[2] == "Dual"]
    assert len(tags) == 2                        # 两日各一次合并净仓单
