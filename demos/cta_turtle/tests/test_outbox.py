# -*- coding: utf-8 -*-
"""enter/add write-ahead TargetIntent/outbox 回归。

目标：set_position 之前意图已持久化；恢复时只允许绝对目标重放或真实
成交证据确认，禁止把框架信号账误当真实成交。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
sys.path.insert(0, os.path.dirname(__file__))

from test_stra_trigger import TrigCtx, _make_trig  # noqa: E402


class _OpenJournal:
    """逐笔成交证据：本层真实成交均价可用。"""

    def __init__(self, price=None):
        self.price = price

    def cursor(self):
        return 0

    def cursor_key(self):
        return "fake:empty:0"

    def layer_price(self, *args, **kwargs):
        return self.price

    def fill_stats(self, *args, **kwargs):
        return 0.0, None


def _mk_live():
    stra = _make_trig()
    stra._volscale = 10.0
    stra._pricetick = 1.0
    stra._last_n = 30.0
    return stra


def _enter_dec(lots_dir=1):
    return {"decision": "enter", "direction": lots_dir,
            "price": 3400.0, "n": 30.0, "failsafe": False}


def test_begin_position_intent_persists_before_target_submit():
    stra = _mk_live()
    ctx = TrigCtx()
    sub = stra._subs[0]
    iid = stra._begin_position_intent(ctx, "enter", sub, _enter_dec(), 7)
    assert iid and list(stra._open_intents) == [iid]
    snap = json.loads(ctx.kv["tb_engine"])
    assert snap["outbox_seq"] == 1
    assert snap["intents"][iid]["intent"] == {
        "code": "SHFE.rb.HOT", "before": 0, "after": 7}
    assert snap["intents"][iid]["state"] == "new"
    # 意图已先落盘，但目标与引擎均未提交/确认。
    assert ctx.orders == []
    assert stra._engine.direction == 0
    stra._complete_position_intent(iid)
    assert stra._open_intents == {}


def test_open_intent_resubmits_absolute_target_without_retriggering():
    """W1：崩溃在 set_position 前/提交丢失 → 下一事件按绝对目标重放，
    不重新走触发链，也不用新 tick 价替代原决策价。"""
    stra = _mk_live()
    ctx = TrigCtx()
    ctx.px = 3400.0
    ctx.stra_get_fund_data = lambda flag=0: 0.0
    sub = stra._subs[0]
    dec = _enter_dec()
    stra._begin_position_intent(ctx, "enter", sub, dec, 7)
    stra._process_open_intents(ctx)
    assert ctx.orders == [("SHFE.rb.HOT", 7, "Enter")]
    assert stra._engine.direction == 0              # 仍未假确认
    rec = next(iter(stra._open_intents.values()))
    assert rec["state"] == "submitted"

    # 再次处理不得重复下单。
    stra._process_open_intents(ctx)
    assert ctx.orders == [("SHFE.rb.HOT", 7, "Enter")]


def test_open_intent_confirms_only_with_fill_evidence():
    """W2/W3：目标账到位 + 逐笔成交证据 → 确认引擎、重建 fill_pending。"""
    stra = _mk_live()
    ctx = TrigCtx()
    ctx.stra_get_fund_data = lambda flag=0: 0.0
    sub = stra._subs[0]
    stra._trade_journal = _OpenJournal(price=None)   # 登记时已有业务游标
    stra._begin_position_intent(ctx, "enter", sub, _enter_dec(), 7)
    # 模拟 set_position 已把框架信号账推进到目标；此时仍不能确认。
    ctx.pos = 7
    stra._process_open_intents(ctx)
    assert stra._engine.direction == 0
    assert len(stra._open_intents) == 1

    # 真实逐笔成交到达后才确认；成交价之后由 re-anchor 流程修正。
    stra._trade_journal = _OpenJournal(price=3405.0)
    stra._process_open_intents(ctx)
    assert stra._engine.direction == 1
    assert stra._engine.layers == [[7, 3400.0]]
    assert stra._risk.entry("SHFE.rb.HOT", "S1")["lots"] == 7
    assert "S1" in stra._fill_pending
    assert stra._open_intents == {}


def test_open_intent_survives_restart_and_does_not_adopt_orphan():
    """快照恢复后 intent 仍在；持仓收养被阻断，等待证据或绝对目标重放。"""
    stra = _mk_live()
    ctx = TrigCtx()
    ctx.stra_get_fund_data = lambda flag=0: 0.0
    sub = stra._subs[0]
    stra._begin_position_intent(ctx, "enter", sub, _enter_dec(), 7)
    saved = ctx.kv["tb_engine"]

    restored = _make_trig()
    restored._volscale = 10.0
    restored._pricetick = 1.0
    restored._last_n = 30.0
    restored.on_init(ctx)
    ctx.px = 3400.0
    assert ctx.kv["tb_engine"] == saved
    assert len(restored._open_intents) == 1
    assert restored._engine.direction == 0
    assert not restored._risk.has_position("SHFE.rb.HOT")

    restored._process_open_intents(ctx)
    assert ctx.orders == [("SHFE.rb.HOT", 7, "Enter")]
    assert restored._engine.direction == 0


def test_backtest_never_creates_outbox_intent():
    stra = _mk_live()
    stra._is_backtest = True
    ctx = TrigCtx()
    iid = stra._begin_position_intent(
        ctx, "enter", stra._subs[0], _enter_dec(), 7)
    assert iid is None
    assert stra._open_intents == {}
