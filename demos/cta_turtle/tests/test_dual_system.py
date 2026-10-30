"""S1+S2 双系统并行测试

覆盖: 子系统装配/units 均分、净仓批量执行、独立出场(部分平仓)、
过滤链对双系统生效、共享风控预算、快照v3恢复、日线驱动两段入场。
"""
import json

import numpy as np

from conftest import *  # noqa: F401,F403
from StraTurtle import StraTurtle
from schemas import InstrumentConfig, TurtleBaseConfig
from core.TurtleEngine import DEC_ENTER, DEC_EXIT


class MiniCtx:
    def __init__(self, pos=0):
        self.pos = pos
        self.avgpx = 0.0
        self.orders = []
        self.prewarmed = []
        self.logs = []

    def stra_get_position(self, code, *a, **k):
        return self.pos

    def stra_set_position(self, code, target, tag=""):
        if tag == "PreWarm":
            self.prewarmed.append((code, target))
            return   # 执行单元预热 (F1): 非策略信号, 不计入订单
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
    from core.ConfigLoader import ConfigLoader
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
    assert stra._risk.entry("SHFE.rb.HOT", "S1")["lots"] == lots1
    assert stra._risk.entry("SHFE.rb.HOT", "S2")["lots"] == lots2


def test_dual_partial_exit_keeps_other_sub():
    """S1 止损离场而 S2 持仓 → 部分平仓而非清零"""
    stra = _mk_dual(units=2)
    stra._is_backtest = True
    e1, e2 = stra._subs[0]["engine"], stra._subs[1]["engine"]
    e1.confirm_enter(1, 100, 3500.0)
    e2.confirm_enter(1, 80, 3500.0)
    ctx = MiniCtx(pos=180)
    stra._execute_dual(ctx, {
        "S1": [{"decision": DEC_EXIT, "reason": "stop", "price": 3440.0}],
        "S2": []}, n=30.0)
    assert e1.direction == 0 and e2.direction == 1
    assert ctx.orders == [("SHFE.rb.HOT", 80, "Dual")]   # 净仓 80
    assert stra._risk.entry("SHFE.rb.HOT", "S2")["lots"] == 80
    assert stra._risk.entry("SHFE.rb.HOT", "S1") is None


class _BatchJournal:
    def __init__(self, volume=0.0, vwap=None):
        self.volume = volume
        self.vwap = vwap

    def cursor(self):
        return 0

    def fill_stats(self, code, cursor, side=None, offset=None):
        return self.volume, self.vwap

    def layer_price(self, *args, **kwargs):
        return self.vwap


class _BatchCtx(MiniCtx):
    def __init__(self, pos=0):
        super().__init__(pos)
        self.kv = {}
        self.px = 0.0

    def stra_get_price(self, code):
        return self.px

    def stra_get_tdate(self):
        return None

    def stra_get_fund_data(self, flag=0):
        return 0.0

    def user_save_data(self, key, value):
        self.kv[key] = value

    def user_load_data(self, key, default=None, value_type=str):
        return self.kv.get(key, "")


def test_dual_live_exit_batch_waits_for_net_fill():
    stra = _mk_dual(units=2)
    e1, e2 = stra._subs[0]["engine"], stra._subs[1]["engine"]
    e1.confirm_enter(1, 100, 3500.0)
    e2.confirm_enter(1, 80, 3500.0)
    stra._last_n = 30.0
    journal = _BatchJournal()
    stra._trade_journal = journal
    ctx = _BatchCtx(pos=180)
    stra._execute_dual(ctx, {
        "S1": [{"decision": DEC_EXIT, "reason": "stop", "price": 3440.0}],
        "S2": []}, n=30.0)
    assert ctx.orders == [("SHFE.rb.HOT", 80, "DualExit")]
    assert e1.direction == 1 and e2.direction == 1   # 未确认前都保留
    assert stra._risk.entry("SHFE.rb.HOT", "S1")["lots"] == 100
    assert stra._risk.entry("SHFE.rb.HOT", "S2")["lots"] == 80
    intent = stra._exit_orders["__dual__"]["intent"]
    assert intent.before == 180 and intent.after == 80

    journal.volume, journal.vwap = 100.0, 3435.0
    stra._process_exit_orders(ctx)
    assert e1.direction == 0 and e2.direction == 1
    assert stra._risk.entry("SHFE.rb.HOT", "S2")["lots"] == 80
    assert stra._risk.entry("SHFE.rb.HOT", "S1") is None
    assert stra._exit_orders == {}


def test_dual_exit_batch_snapshot_restores_intent():
    stra = _mk_dual(units=2)
    e1, e2 = stra._subs[0]["engine"], stra._subs[1]["engine"]
    e1.confirm_enter(1, 100, 3500.0)
    e2.confirm_enter(1, 80, 3500.0)
    stra._last_n = 30.0
    stra._trade_journal = _BatchJournal()
    ctx = _BatchCtx(pos=180)
    stra._execute_dual(ctx, {
        "S1": [{"decision": DEC_EXIT, "reason": "stop", "price": 3440.0}],
        "S2": []}, n=30.0)
    saved = stra._exit_order_snapshot()
    json.dumps(saved)                              # 必须可持久化

    restored = _mk_dual(units=2)
    restored._sub("S1")["engine"].restore(e1.snapshot())
    restored._sub("S2")["engine"].restore(e2.snapshot())
    restored._restore_exit_orders(saved)
    fp = restored._exit_orders["__dual__"]
    assert fp["intent"].after == 80
    assert [e.system for e in fp["engines"]] == ["S1"]
    assert fp["engines"][0].direction == 1
    # 批次已覆盖 S1；兼容补录不得再生成独立的 S1 出场记录。
    # 否则批次成交后该记录会把仍持仓的 S2 风险账一并误清。
    assert list(restored._exit_orders) == ["__dual__"]


def test_restored_dual_exit_fill_keeps_remaining_sub_risk():
    stra = _mk_dual(units=2)
    e1, e2 = stra._subs[0]["engine"], stra._subs[1]["engine"]
    e1.confirm_enter(1, 100, 3500.0)
    e2.confirm_enter(1, 80, 3500.0)
    stra._last_n = 30.0
    stra._trade_journal = _BatchJournal()
    stra._sync_risk_book(30.0)
    ctx = _BatchCtx(pos=180)
    stra._execute_dual(ctx, {
        "S1": [{"decision": DEC_EXIT, "reason": "stop", "price": 3440.0}],
        "S2": []}, n=30.0)
    saved = stra._exit_order_snapshot()

    restored = _mk_dual(units=2)
    restored._sub("S1")["engine"].restore(e1.snapshot())
    restored._sub("S2")["engine"].restore(e2.snapshot())
    restored._last_n = 30.0
    restored._sync_risk_book(30.0)
    restored._restore_exit_orders(saved)
    journal = _BatchJournal(volume=100.0, vwap=3435.0)
    restored._trade_journal = journal
    restored._process_exit_orders(ctx)
    assert [s["engine"].direction for s in restored._subs] == [0, 1]
    book = restored._risk.entry("SHFE.rb.HOT", "S2")
    assert book["lots"] == 80
    assert restored._risk.entry("SHFE.rb.HOT", "S1") is None
    assert restored._exit_orders == {}


def test_dual_mixed_enter_is_deferred_and_replayed_after_exit_fill():
    stra = _mk_dual(units=2)
    e1, e2 = stra._subs[0]["engine"], stra._subs[1]["engine"]
    e1.confirm_enter(1, 100, 3500.0)
    stra._last_n = 30.0
    journal = _BatchJournal()
    stra._trade_journal = journal
    ctx = _BatchCtx(pos=100)
    stra._execute_dual(ctx, {
        "S1": [{"decision": DEC_EXIT, "reason": "stop", "price": 3440.0}],
        "S2": [{"decision": DEC_ENTER, "direction": -1,
                "price": 3400.0, "n": 30.0, "failsafe": False}]},
        n=30.0)
    # 混合净目标不得立即执行 enter；批次只提交净出场。
    assert ctx.orders == [("SHFE.rb.HOT", 0, "DualExit")]
    assert [d["decision"] for d in stra._deferred_decisions["S2"]] == ["enter"]
    assert e2.direction == 0

    # 快照恢复后仍是"延后"而不是消失。
    saved = json.loads(json.dumps({
        "exit_orders": stra._exit_order_snapshot(),
        "deferred": stra._deferred_decisions}))
    restored = _mk_dual(units=2)
    restored._sub("S1")["engine"].restore(e1.snapshot())
    restored._sub("S2")["engine"].restore(e2.snapshot())
    restored._restore_exit_orders(saved["exit_orders"])
    restored._deferred_decisions = saved["deferred"]
    restored._last_n = 30.0
    journal.volume, journal.vwap = 100.0, 3435.0
    restored._trade_journal = journal
    restored._process_exit_orders(ctx)
    r1, r2 = restored._subs[0]["engine"], restored._subs[1]["engine"]
    assert r1.direction == 0
    assert r2.direction == -1
    assert restored._deferred_decisions == {}
    assert ctx.orders[-1] == ("SHFE.rb.HOT", -r2.layers[0][0], "Dual")
    assert restored._risk.has_position("SHFE.rb.HOT")


def test_dual_trigger_same_tick_is_one_net_order():
    inst = InstrumentConfig(code="SHFE.rb.HOT", period="d1",
                            system="S1+S2", units=2)
    stra = StraTurtle("td_trig", inst, TurtleBaseConfig(entry_mode="trigger"))
    stra._volscale = 10.0
    stra._pricetick = 1.0
    stra._last_n = 30.0
    for sub in stra._subs:
        e = sub["engine"]
        e._n = 30.0
        e._entry_ch = (100.0, 90.0)
        e._rearm()
    ctx = _BatchCtx()
    ctx.px = 100.0
    stra.on_tick(ctx, "SHFE.rb.HOT", None)
    assert len(ctx.orders) == 1
    assert ctx.orders[0][2] == "Dual"
    assert ctx.orders[0][1] == stra._combined_target()
    assert all(s["engine"].direction == 1 for s in stra._subs)


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


def test_dual_same_batch_reserves_total_risk():
    """同批第二个子系统必须看到第一个的新风险占用。

    旧实现全批结束后才 sync，两个入场可分别消费同一份 total_risk
    剩余额度；现在每个 confirm 后立即做批次内预算预约。
    """
    from schemas import RiskConfig
    inst = InstrumentConfig(code="X.Y.HOT", period="d1",
                            system="S1+S2", units=2)
    rcfg = RiskConfig(max_units_total=0, max_units_per_group=0,
                      max_total_risk_frac=0.01)
    stra = StraTurtle("tb_reserve", inst, TurtleBaseConfig(), risk_cfg=rcfg)
    stra._volscale = 10.0
    ctx = MiniCtx()
    dec = {"decision": DEC_ENTER, "direction": 1, "price": 3500.0,
           "n": 100.0}
    stra._execute_dual(ctx, {"S1": [dec], "S2": [dict(dec)]}, n=100.0)
    l1 = stra._sub_lots(stra._subs[0])
    l2 = stra._sub_lots(stra._subs[1])
    budget = stra._risk.cfg.fallback_capital * 0.01
    assert l1 == int(budget // 2000)  # budget / (100×10×2)
    assert l2 == 0             # 第一笔已占满总风险，第二笔不得重复消费
    assert stra._risk.open_risk() <= budget + 1e-6


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


# ---------- B5: 双系统换月/漂移 re-anchor ----------

def test_dual_reanchor_shifts_both_subsystems():
    """两子系统同时持仓时 avgpx 漂移 re-anchor 必须生效 (旧实现恒跳过)"""
    stra = _mk_dual()
    e1, e2 = stra._subs[0]["engine"], stra._subs[1]["engine"]
    e1.confirm_enter(1, 10, 3400.0, n=30.0)
    e2.confirm_enter(1, 10, 3420.0, n=30.0)
    ctx = MiniCtx(pos=20)
    ctx.avgpx = 3415.0            # 框架净均价: 我们的 3410 + 换月价差 5
    stra._reanchor_on_roll(ctx)
    assert e1.layers == [[10, 3405.0]]
    assert e2.layers == [[10, 3425.0]]
    assert any("re-anchor" in t and "2 sub" in t for _, t in ctx.logs)


def test_dual_reanchor_handles_opposite_subsystems():
    """反向并存 (S1 多 / S2 空): 按净成本口径同样平移"""
    stra = _mk_dual()
    e1, e2 = stra._subs[0]["engine"], stra._subs[1]["engine"]
    e1.confirm_enter(1, 10, 3400.0, n=30.0)
    e2.confirm_enter(-1, 4, 3500.0, n=30.0)
    net_cost = 10 * 3400.0 - 4 * 3500.0          # 20000
    our_avg = abs(net_cost) / 14.0               # 1428.57
    ctx = MiniCtx(pos=6)                         # 净仓 10-4
    ctx.avgpx = our_avg + 5.0
    stra._reanchor_on_roll(ctx)
    assert abs(e1.layers[0][1] - 3405.0) < 1e-9
    assert abs(e2.layers[0][1] - 3505.0) < 1e-9


def test_dual_reanchor_skips_on_position_mismatch():
    """框架持仓与净目标不符 (部分成交/手工干预) → 不平移"""
    stra = _mk_dual()
    e1, e2 = stra._subs[0]["engine"], stra._subs[1]["engine"]
    e1.confirm_enter(1, 10, 3400.0, n=30.0)
    e2.confirm_enter(1, 10, 3420.0, n=30.0)
    ctx = MiniCtx(pos=15)                        # 与净目标 20 不符
    ctx.avgpx = 3415.0
    stra._reanchor_on_roll(ctx)
    assert e1.layers[0][1] == 3400.0 and e2.layers[0][1] == 3420.0


# ---------- B6: 双系统风控账本按层冻结 N 记账 ----------

def test_dual_risk_book_uses_layer_n_not_current():
    """批量同步账本用各层冻结 N 汇总, 不用当根 N (旧实现每根 bar 覆盖)"""
    stra = _mk_dual()
    e1, e2 = stra._subs[0]["engine"], stra._subs[1]["engine"]
    e1.confirm_enter(1, 10, 3400.0, n=60.0)
    e2.confirm_enter(1, 5, 3420.0, n=80.0)
    stra._sync_risk_book(200.0)          # 当根 N 已漂到 200
    expect = 10 * 60.0 * 10.0 * 2.0 + 5 * 80.0 * 10.0 * 2.0
    assert abs(stra._risk.open_risk() - expect) < 1e-6
    book1 = stra._risk.entry("SHFE.rb.HOT", "S1")
    book2 = stra._risk.entry("SHFE.rb.HOT", "S2")
    assert book1["lots"] == 10 and book2["lots"] == 5
    assert abs(book1["risk"] + book2["risk"] - expect) < 1e-6


def test_engine_booked_risk_per_layer():
    """engine.booked_risk(): 逐层按各自冻结 N (缺 N 的层跳过)"""
    from core.TurtleEngine import TurtleEngine
    e = TurtleEngine("S1", stop_atr_mult=2.0, pyramid_layers=4)
    e.confirm_enter(1, 10, 3000.0, n=60.0)
    e.confirm_add(5, 3030.0, n=80.0)
    assert abs(e.booked_risk(10.0) - (10 * 60 + 5 * 80) * 10 * 2) < 1e-9
    assert abs(e.booked_risk(10.0, stop_mult=3.0)
               - (10 * 60 + 5 * 80) * 10 * 3) < 1e-9


def test_risk_manager_explicit_risk_override():
    """on_rebuilt/sync_position 支持显式风险值 (多子系统汇总口径)"""
    from core.RiskManager import RiskManager
    rm = RiskManager()
    rm.on_rebuilt("SHFE.rb.HOT", 1, 15, 3410.0, 200.0, 10.0, 2.0,
                  "black", risk=1234.0)
    assert abs(rm.open_risk() - 1234.0) < 1e-9
    rm.sync_position("SHFE.rb.HOT", 1, 15, 3410.0, 200.0, 10.0, 2.0,
                     "black", risk=999.0)
    assert abs(rm.open_risk() - 999.0) < 1e-9
    assert rm.open_units == 1
