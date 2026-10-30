# -*- coding: utf-8 -*-
"""实盘出场未知态：真实确认前保留管理状态，确认后才允许重入。"""
from core.TurtleEngine import TurtleEngine


def _channels():
    return {
        "entry_upper": 200.0, "entry_lower": 109.0,
        "exit_upper": 300.0, "exit_lower": 108.0,
        "fs_upper": 400.0, "fs_lower": 80.0,
        "prev_entry_upper": 199.0, "prev_entry_lower": 110.0,
        "prev_exit_upper": 300.0, "prev_exit_lower": 108.0,
        "prev_fs_upper": 399.0, "prev_fs_lower": 81.0,
    }


def _exit_then_skip(engine):
    return engine.on_bar(120.0, 106.0, 107.0, 120.0, 10.0,
                         _channels(), open=120.0)


def test_trigger_exit_pending_blocks_retrigger_and_survives_restore():
    e = TurtleEngine(system="S1", entry_mode="trigger")
    e.on_bar(100, 98, 99, 99, 2.0, _channels())
    e.on_price(200.0)
    e.confirm_enter(1, 3, 200.0, 2.0)
    stop = e.armed["stop"]

    dec = e.on_price(stop)[0]
    assert dec["decision"] == "exit"
    assert e.begin_exit(dec["reason"], dec["price"])
    assert e.direction == 1 and e.layers == [[3, 200.0]]
    assert e.on_price(stop - 10) == []       # 不重复出场、不提前反手
    assert e.check_bar(stop - 10, stop + 1, stop - 11, stop - 10) == []

    restored = TurtleEngine(system="S1", entry_mode="trigger")
    restored.restore(e.snapshot())
    assert restored.direction == 1
    assert restored._exit_pending["lots"] == 3
    assert restored.on_price(stop - 10) == []

    restored.confirm_exit(stop - 2.0)        # 真实证据到达后才清场
    assert restored.direction == 0 and restored.layers == []
    assert restored._exit_pending is None
    assert restored.armed["long"] is not None


def test_close_async_exit_restores_s1_until_real_fill():
    e = TurtleEngine(system="S1", entry_mode="close")
    e.confirm_enter(1, 1, 100.0, n=10.0)
    decs = _exit_then_skip(e)
    assert [d["decision"] for d in decs] == ["exit", "skip"]
    assert e.s1_last["taken"] is False       # 决策阶段已建反向 sim

    dec = decs[0]
    assert e.begin_exit(dec["reason"], dec["price"])
    assert e.direction == 1 and e.layers == [[1, 100.0]]
    assert e.s1_last["taken"] is True        # 未成交前仍管理原实单
    assert e.s1_last["status"] == "open"
    assert e._fs_armed == 0                  # 不保留未执行的反向 sim
    assert e.on_bar(120, 106, 107, 120, 10, _channels()) == []

    e.confirm_exit(105.0)                    # 真实成交价结清旧实单
    assert e.direction == 0
    assert e.s1_last["result"] == "win"
    assert e.s1_last["taken"] is True


def test_close_immediate_exit_skip_preserves_new_sim_failsafe():
    """回测/即时确认路径也修复旧仓 confirm 清掉新 sim 的边界。"""
    e = TurtleEngine(system="S1", entry_mode="close")
    e.confirm_enter(1, 1, 100.0, n=10.0)
    decs = _exit_then_skip(e)
    e.confirm_exit(decs[0]["price"])
    assert e.direction == 0
    assert e.s1_last["taken"] is False
    assert e.s1_last["direction"] == -1
    assert e._fs_armed == -1


def test_cancel_exit_keeps_original_position_and_rearms_management():
    e = TurtleEngine(system="S2", entry_mode="trigger")
    e.on_bar(100, 98, 99, 99, 2.0, _channels())
    e.on_price(200.0)
    e.confirm_enter(1, 2, 200.0, 2.0)
    stop = e.armed["stop"]
    dec = e.on_price(stop)[0]
    e.begin_exit(dec["reason"], dec["price"])

    e.cancel_exit()                          # 仅目标提交失败，不是已成交
    assert e.direction == 1 and e.layers == [[2, 200.0]]
    assert e._exit_pending is None
    assert e.on_price(stop)[0]["decision"] == "exit"


class _ExitJournal:
    def __init__(self):
        self.volume = 0.0
        self.vwap = None
        self.calls = []

    def cursor(self):
        return 0

    def layer_price(self, *args, **kwargs):
        return None                 # 入场均价回写不在本测试范围内

    def fill_stats(self, code, cursor, side=None, offset=None):
        self.calls.append((code, cursor, side, offset))
        return self.volume, self.vwap


def _live_position():
    from test_stra_trigger import _setup
    stra, ctx, _d1 = _setup()
    journal = _ExitJournal()
    stra._trade_journal = journal
    stra.on_calculate(ctx)
    ctx.px = stra._engine.armed["long"]
    stra.on_tick(ctx, "SHFE.rb.HOT", None)
    assert stra._engine.direction == 1
    return stra, ctx, journal


def test_live_exit_waits_for_close_fill_and_blocks_reentry():
    stra, ctx, journal = _live_position()
    engine = stra._engine
    lots = engine.layers[0][0]
    stop = engine.armed["stop"]
    ctx.px = stop
    stra.on_tick(ctx, "SHFE.rb.HOT", None)

    assert ctx.orders[-1][1] == 0            # 目标已提交
    assert engine.direction == 1              # 真实确认前仍持仓
    assert stra._risk.has_position("SHFE.rb.HOT")
    assert stra._exit_orders["S1"]["lots"] == lots
    order_count = len(ctx.orders)
    stra.on_tick(ctx, "SHFE.rb.HOT", None)   # 不重复提交、不重入
    assert len(ctx.orders) == order_count
    assert journal.calls[-1][2:] == (-1, "close")

    stra._exit_orders["S1"]["ts"] -= 700     # 超时也只告警，不能假确认
    stra.on_tick(ctx, "SHFE.rb.HOT", None)
    assert engine.direction == 1 and stra._exit_orders
    assert any("exit unconfirmed" in t for _, t in ctx.logs)

    journal.volume = lots - 1                # 部分成交仍不释放风险
    if lots > 1:
        stra.on_tick(ctx, "SHFE.rb.HOT", None)
        assert engine.direction == 1
        assert stra._risk.has_position("SHFE.rb.HOT")

    journal.volume = lots
    journal.vwap = stop - 3.0
    stra.on_tick(ctx, "SHFE.rb.HOT", None)
    assert engine.direction == 0
    assert not stra._risk.has_position("SHFE.rb.HOT")
    assert stra._exit_orders == {}
    assert engine.s1_last["entry_price"] is not None


def test_live_exit_pending_snapshot_restore_keeps_block():
    stra, ctx, journal = _live_position()
    engine = stra._engine
    lots = engine.layers[0][0]
    ctx.px = engine.armed["stop"]
    stra.on_tick(ctx, "SHFE.rb.HOT", None)
    assert stra._exit_orders
    order_count = len(ctx.orders)

    from test_stra_trigger import _make_trig
    restored = _make_trig()
    restored.on_init(ctx)                    # 从同一 kv 恢复
    restored._trade_journal = journal
    assert restored._engine.direction == 1
    assert restored._exit_orders["S1"]["lots"] == lots
    restored.on_tick(ctx, "SHFE.rb.HOT", None)
    assert len(ctx.orders) == order_count

    journal.volume = lots
    journal.vwap = ctx.px
    restored.on_tick(ctx, "SHFE.rb.HOT", None)
    assert restored._engine.direction == 0
    assert restored._exit_orders == {}


def test_daily_flatten_live_uses_same_exit_pending_path():
    stra, ctx, _journal = _live_position()
    stra._flatten_all(ctx, "daily_loss -6%")
    assert ctx.orders[-1] == ("SHFE.rb.HOT", 0, "DailyStop")
    assert stra._engine.direction == 1
    assert stra._risk.has_position("SHFE.rb.HOT")
    assert stra._exit_orders["S1"]["reason"] == "daily_loss -6%"


def test_dual_daily_flatten_waits_for_net_fill():
    from test_dual_system import _BatchCtx, _BatchJournal, _mk_dual
    stra = _mk_dual(units=2)
    e1, e2 = stra._subs[0]["engine"], stra._subs[1]["engine"]
    e1.confirm_enter(1, 100, 3500.0)
    e2.confirm_enter(1, 80, 3500.0)
    stra._last_n = 30.0
    stra._trade_journal = _BatchJournal()
    stra._sync_risk_book(30.0)
    stra._alert = lambda *args, **kwargs: None
    ctx = _BatchCtx(pos=180)
    stra._flatten_all(ctx, "daily_loss -6%")
    # 多 profile 日损同样不得目标 0 即确认；成交证据前保留全部管理状态。
    assert e1.direction == 1 and e2.direction == 1
    assert stra._risk.entry("SHFE.rb.HOT", "S1")["lots"] == 100
    assert stra._risk.entry("SHFE.rb.HOT", "S2")["lots"] == 80
    assert stra._exit_orders["__dual__"]["intent"].after == 0

    stra._trade_journal.volume, stra._trade_journal.vwap = 180.0, 3400.0
    stra._process_exit_orders(ctx)
    assert e1.direction == 0 and e2.direction == 0
    assert not stra._risk.has_position("SHFE.rb.HOT")
    assert stra._exit_orders == {}


def test_dual_intrabar_stop_uses_async_exit_batch():
    """盘中止损路径与常规信号出场同一语义：多 profile 不得 confirm 后
    再祈祷成交，必须等真实平仓证据。"""
    from test_dual_system import _BatchJournal, _mk_dual
    from test_intrabar import _fill_daily
    from test_stra_trigger import TrigCtx
    stra = _mk_dual(units=2)
    stra._inst = stra._inst.__class__(
        code="SHFE.rb.HOT", period="d1", system="S1+S2",
        units=2, intrabar_stop=True)
    stra._mode = stra._mode.__class__(
        is_backtest=False, period="d1", entry_mode="close",
        intrabar_stop=True, bt_no_simtick=False)
    ctx = TrigCtx()
    d1 = ctx.add_store("d1")
    _fill_daily(d1)
    ctx.avgpx = 3500.0
    stra.on_init(ctx)
    e1, e2 = stra._subs[0]["engine"], stra._subs[1]["engine"]
    e1.confirm_enter(1, 100, 3500.0)
    e2.confirm_enter(1, 80, 3500.0)
    stra._last_n = 30.0
    stra._set_n(30.0)
    stra._sync_risk_book(30.0)
    stra._trade_journal = _BatchJournal()
    ctx.pos = 180
    m5 = ctx.add_store("m5")
    m5.append(20230089, 900, 3450, 3452, 3448, 3450)   # 首根只建扫描基线
    stra.on_calculate(ctx)
    m5.append(20230089, 905, 3450, 3452, 3438, 3445)   # 触发共同止损 3440
    stra.on_calculate(ctx)
    assert e1.direction == 1 and e2.direction == 1
    assert stra._exit_orders["__dual__"]["intent"].after == 0
    assert stra._risk.entry("SHFE.rb.HOT", "S1")["lots"] == 100
    assert stra._risk.entry("SHFE.rb.HOT", "S2")["lots"] == 80

    stra._trade_journal.volume, stra._trade_journal.vwap = 180.0, 3438.0
    stra._process_exit_orders(ctx)
    assert e1.direction == 0 and e2.direction == 0
    assert not stra._risk.has_position("SHFE.rb.HOT")
