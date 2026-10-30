# -*- coding: utf-8 -*-
"""订单级归因链 (core/OrderAttributor) 回归 + 故障注入。

覆盖 docs/ORDER_ATTRIBUTION_NO_CPP.md §3.3 算法与 §4 残余风险:
  - 签名预测: 开/平多、开/平空、跨零 mixed (平多+开空)、反向 (平多+平空)
  - 场景 A: 同向双意图 + 外部订单穿插 → 按提交序绑定, 不错配
  - 场景 C: 跨零意图双订单逐一认领 → done + 精确 vwap
  - 分批成交进度; 撤单重挂 (R1); 无订单事件的成交 stub 认领 (R3)
  - 守恒破坏: 无法匹配的成交进 unmatched, 意图保持未完成 (UNKNOWN)
  - OrderFeed: JSONL+CSV 双源去重 / 半行不消费 / 按日轮转增量
  - 进程内集成: outbox 意图经归因证据以 orders vwap 确认 (enter/exit)
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
sys.path.insert(0, os.path.dirname(__file__))

from core.OrderAttributor import (OrderAttributor, OrderFeed, ExecAttribution,
                                  _signatures)  # noqa: E402

CODE = "SHFE.rb.HOT"


# ---------- 签名预测 (簿记镜像) ----------

def test_signatures_open_close_and_cross_zero():
    assert _signatures(0, 0, 10) == [(True, True, 10)]      # 开多
    assert _signatures(10, 0, 0) == [(True, False, 10)]     # 平多
    assert _signatures(0, 0, -4) == [(False, True, 4)]      # 开空
    assert _signatures(0, 4, 0) == [(False, False, 4)]      # 平空
    # 跨零 mixed: +6 → -10 = 平多 10 + 开空 16... 簿记口径: 目标 -10 →
    # 多簿 0/空簿 10: 平多 10 + 开空 (10-4)=6
    assert _signatures(10, 4, -10) == [(True, False, 10),
                                       (False, True, 6)]
    # 反向并存同批全平: +6 (L10/S4) → 0 = 平多 10 + 平空 4
    assert _signatures(10, 4, 0) == [(True, False, 10),
                                     (False, False, 4)]


# ---------- 场景 A: 同向双意图 + 外部订单穿插 ----------

def test_fifo_binding_with_foreign_order_interleaved():
    attr = OrderAttributor()
    attr.init_book(CODE, long=0, short=0)
    attr.on_intent("i1", CODE, before=0, after=10, ts=100.0)
    attr.on_intent("i2", CODE, before=10, after=14, ts=101.0)
    # 外部订单 (其他策略/手工, 方向签名与 i1 相同但时序在两者之间)
    attr.on_order({"code": CODE, "localid": 99, "islong": True,
                   "isopen": True, "total": 5, "ts": 100.5})
    # i1 的订单: 开多 10
    attr.on_order({"code": CODE, "localid": 1, "islong": True,
                   "isopen": True, "total": 10, "ts": 100.1})
    # i2 的订单: 开多 4
    attr.on_order({"code": CODE, "localid": 2, "islong": True,
                   "isopen": True, "total": 4, "ts": 101.1})
    attr.on_trade({"code": CODE, "localid": 1, "volume": 10,
                   "price": 3400.0, "ts": 100.2})
    attr.on_trade({"code": CODE, "localid": 2, "volume": 4,
                   "price": 3410.0, "ts": 101.2})
    p1, p2 = attr.progress("i1"), attr.progress("i2")
    assert p1["done"] and p1["vwap"] == pytest.approx(3400.0)
    assert p2["done"] and p2["vwap"] == pytest.approx(3410.0)
    # 外部订单无成交, 不产生 unmatched
    assert attr.unmatched == []


# ---------- 场景 C: 跨零 mixed 双订单逐一认领 ----------

def test_cross_zero_mixed_resolves_by_signature_sequence():
    attr = OrderAttributor()
    attr.init_book(CODE, long=10, short=4)       # S1 多10 + S2 空4
    attr.on_intent("dual", CODE, before=6, after=-10, ts=200.0)
    # 执行单元拆单: 先平多 10, 后开空 16-4... 实际两簿: 目标 -10 → 空簿 10
    attr.on_order({"code": CODE, "localid": 7, "islong": True,
                   "isopen": False, "total": 10, "ts": 200.1})
    attr.on_order({"code": CODE, "localid": 8, "islong": False,
                   "isopen": True, "total": 6, "ts": 200.2})
    # 平多先全成 → 未完成 (还有开空签名); 开空部成 → 仍未完成
    attr.on_trade({"code": CODE, "localid": 7, "volume": 10,
                   "price": 3350.0, "ts": 200.3})
    p = attr.progress("dual")
    assert not p["done"] and p["filled"] == 10
    # 订单到达乱序成交: 开空成交先回 (真实柜台可交错)
    attr.on_trade({"code": CODE, "localid": 8, "volume": 6,
                   "price": 3340.0, "ts": 200.15})
    p = attr.progress("dual")
    assert p["done"] and p["vwap"] == pytest.approx(
        (10 * 3350.0 + 6 * 3340.0) / 16.0)


# ---------- R1: 撤单重挂 (新 localid 重新认领同一签名) ----------

def test_cancel_and_replace_rebinds_signature():
    attr = OrderAttributor()
    attr.init_book(CODE, long=0, short=0)
    attr.on_intent("i1", CODE, before=0, after=10, ts=300.0)
    attr.on_order({"code": CODE, "localid": 11, "islong": True,
                   "isopen": True, "total": 10, "ts": 300.1})
    attr.on_order({"code": CODE, "localid": 11, "canceled": True,
                   "ts": 300.2})
    attr.on_order({"code": CODE, "localid": 12, "islong": True,
                   "isopen": True, "total": 10, "ts": 300.3})
    attr.on_trade({"code": CODE, "localid": 12, "volume": 10,
                   "price": 3405.0, "ts": 300.4})
    p = attr.progress("i1")
    assert p["done"] and p["vwap"] == pytest.approx(3405.0)
    # 旧 localid 的迟到成交不再挂到意图上 (撤单后解绑)
    attr.on_trade({"code": CODE, "localid": 11, "volume": 10,
                   "price": 3400.0, "ts": 300.5})
    assert attr.progress("i1")["vwap"] == pytest.approx(3405.0)


# ---------- R3: 无订单事件的成交 stub 认领 (trades.csv 兜底) ----------

def test_fill_without_order_event_stub_binds_by_signature():
    attr = OrderAttributor()
    attr.init_book(CODE, long=10, short=0)
    attr.on_intent("x", CODE, before=10, after=0, ts=400.0)
    # 总线丢包: 只有成交行 (trades.csv 口径, localid 未被订单事件绑定)
    attr.on_trade({"code": CODE, "localid": 55, "volume": 6,
                   "price": 3360.0, "islong": True, "isopen": False,
                   "ts": 400.1})
    p = attr.progress("x")
    assert not p["done"] and p["filled"] == 6
    attr.on_trade({"code": CODE, "localid": 55, "volume": 4,
                   "price": 3362.0, "islong": True, "isopen": False,
                   "ts": 400.2})
    p = attr.progress("x")
    assert p["done"] and p["vwap"] == pytest.approx(
        (6 * 3360.0 + 4 * 3362.0) / 10.0)


# ---------- 守恒破坏: 无法匹配的成交 → UNKNOWN ----------

def test_unmatched_fill_keeps_intent_open_and_records():
    attr = OrderAttributor()
    attr.init_book(CODE, long=0, short=0)
    attr.on_intent("i1", CODE, before=0, after=10, ts=500.0)
    # 方向签名不匹配任何待签名 (平空成交 vs 开多意图)
    attr.on_trade({"code": CODE, "localid": 66, "volume": 4,
                   "price": 3400.0, "islong": False, "isopen": False,
                   "ts": 500.1})
    assert not attr.progress("i1")["done"]
    assert len(attr.unmatched) == 1
    # 正确订单到达后仍可正常完成 (未匹配成交不毒化队列)
    attr.on_order({"code": CODE, "localid": 67, "islong": True,
                   "isopen": True, "total": 10, "ts": 500.2})
    attr.on_trade({"code": CODE, "localid": 67, "volume": 10,
                   "price": 3401.0, "ts": 500.3})
    assert attr.progress("i1")["done"]


# ---------- 簿镜像漂移校准 ----------

def test_intent_before_recalibrates_drifted_book():
    attr = OrderAttributor()
    attr.init_book(CODE, long=0, short=0)      # 镜像落后 (真实已持多 10)
    sigs = attr.on_intent("i1", CODE, before=10, after=0, ts=600.0)
    assert sigs == [(True, False, 10)]         # 校准后预测正确, 不平成 0 手


# ---------- complete 不回滚簿 / discard 回滚 ----------

def test_complete_keeps_book_discard_rolls_back():
    attr = OrderAttributor()
    attr.init_book(CODE, long=0, short=0)
    attr.on_intent("i1", CODE, before=0, after=10, ts=700.0)
    attr.complete("i1")
    assert attr._books[CODE]["long"] == 10.0    # 镜像保留
    attr.on_intent("i2", CODE, before=10, after=0, ts=701.0)
    attr.discard("i2")
    assert attr._books[CODE]["long"] == 10.0    # 回滚到 before


# ---------- OrderFeed: 双源去重 / 半行 / 轮转 ----------

def _feed_dirs(tmp_path, lines, csv_lines=None):
    jdir = tmp_path / "execj"
    jdir.mkdir(exist_ok=True)
    f = jdir / "exec_20261029.jsonl"
    f.write_text("".join(lines), encoding="utf-8")
    csv = ""
    if csv_lines is not None:
        csv_f = tmp_path / "trades.csv"
        csv_f.write_text("".join(csv_lines), encoding="utf-8")
        csv = str(csv_f)
    return OrderFeed(str(jdir), csv)


def test_order_feed_dedup_across_jsonl_and_csv(tmp_path):
    jdir = tmp_path / "execj"
    jdir.mkdir(exist_ok=True)
    csv_f = tmp_path / "trades.csv"
    feed = OrderFeed(str(jdir), str(csv_f))      # 先建 feed (空目录)
    (jdir / "exec_20261029.jsonl").write_text(
        json.dumps({"type": "trade", "code": CODE, "localid": 5,
                    "volume": 2, "price": 3400.0, "ts": 1.0}) + "\n",
        encoding="utf-8")
    csv_f.write_text(
        "5,20261029,93000,SHFE.rb.2610,OL,2,3400.0,t9,5\n"   # 跨源重复
        "6,20261029,93001,SHFE.rb.2610,OL,3,3401.0,t10,6\n",  # 补缺
        encoding="utf-8")
    evs = feed.poll()
    trades = [e for e in evs if e["kind"] == "trade"]
    assert len(trades) == 2                      # 跨源去重生效
    assert sorted(e["localid"] for e in trades) == ["5", "6"]
    # 再次 poll 不重复产出
    assert feed.poll() == []


def test_order_feed_half_line_not_consumed(tmp_path):
    f = tmp_path / "execj" / "exec_20261029.jsonl"
    f.parent.mkdir(exist_ok=True)
    f.write_text(json.dumps({"type": "order", "code": CODE, "localid": 1,
                             "islong": True, "isopen": True, "total": 1,
                             "ts": 1.0}), encoding="utf-8")  # 无换行半行
    feed = OrderFeed(str(tmp_path / "execj"), "")
    assert feed.poll() == []
    with open(f, "a", encoding="utf-8") as fh:
        fh.write("\n")
    evs = feed.poll()
    assert len(evs) == 1 and evs[0]["kind"] == "order"


def test_order_feed_primes_backlog_without_events(tmp_path):
    """存量积压 (重启前已有的 journal/成交) 不回放: 无待归因意图时历史
    不得成批 unmatched (2026-10-30 生产部署实测回归)。"""
    f = tmp_path / "execj" / "exec_20261030.jsonl"
    f.parent.mkdir(exist_ok=True)
    f.write_text(
        json.dumps({"type": "trade", "code": CODE, "localid": 9,
                    "volume": 2, "price": 3400.0, "ts": 1.0}) + "\n",
        encoding="utf-8")
    csv_f = tmp_path / "trades.csv"
    csv_f.write_text(
        "9,20261030,100000,SHFE.rb.2610,OL,2,3400.0,t1,9\n",
        encoding="utf-8")
    feed = OrderFeed(str(tmp_path / "execj"), str(csv_f))
    assert feed.poll() == []                     # 积压不产生事件
    # 新事件正常流动
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "trade", "code": CODE, "localid": 10,
                             "volume": 1, "price": 3401.0, "ts": 2.0})
                 + "\n")
    evs = feed.poll()
    assert len(evs) == 1 and evs[0]["localid"] == "10"


def test_order_feed_daily_rotation_resumes(tmp_path):
    jdir = tmp_path / "execj"
    jdir.mkdir(exist_ok=True)
    feed = OrderFeed(str(jdir), "")              # 先建 feed (空目录)
    (jdir / "exec_20261029.jsonl").write_text(
        json.dumps({"type": "order", "code": CODE, "localid": 1,
                    "islong": True, "isopen": True, "total": 1,
                    "ts": 1.0}) + "\n", encoding="utf-8")
    assert len(feed.poll()) == 1
    # 新一天的文件出现: 增量继续, 旧文件不重读
    (tmp_path / "execj" / "exec_20261030.jsonl").write_text(
        json.dumps({"type": "order", "code": CODE, "localid": 2,
                    "islong": True, "isopen": True, "total": 1,
                    "ts": 2.0}) + "\n", encoding="utf-8")
    evs = feed.poll()
    assert len(evs) == 1 and evs[0]["localid"] == "2"


# ---------- ExecAttribution: 簿初值 ----------

def test_exec_attr_init_book_from_broker_positions():
    ea = ExecAttribution(CODE)
    ea.init_book([
        {"code": "SHFE.rb.2610",
         "long": {"newvol": 10, "prevol": 0, "newavail": 10,
                  "preavail": 0},
         "short": {"newvol": 4, "prevol": 0, "newavail": 4,
                   "preavail": 0}},
        {"code": "CZCE.MA.609",
         "long": {"newvol": 9, "prevol": 0}},
    ], fallback_net=0.0)
    book = ea.attr._books[CODE]
    assert book["long"] == 10.0 and book["short"] == 4.0


def test_exec_attr_fallback_net_when_no_broker_positions():
    ea = ExecAttribution(CODE)
    ea.init_book(None, fallback_net=6.0)
    book = ea.attr._books[CODE]
    assert book["long"] == 6.0 and book["short"] == 0.0


# ---------- 进程内集成: outbox 意图经 orders 证据确认 ----------

class _Ctx:
    """最小实盘桩: 目标账可推进, kv 可持久化。"""

    def __init__(self, pos=0.0):
        self.pos = pos
        self.kv = {}
        self.logs = []
        self.px = 3400.0

    def stra_get_position(self, code):
        return self.pos

    def stra_set_position(self, code, target, tag=""):
        self.pos = float(target)

    def stra_get_fund_data(self, flag=0):
        return 0.0

    def stra_get_price(self, code):
        return self.px

    def stra_get_date(self):
        return 20261029

    def user_save_data(self, key, val):
        self.kv[key] = val

    def stra_log_text(self, msg, level=1):
        self.logs.append((level, msg))


def _mk_live(tmp_path, monkeypatch):
    import time as _t
    from test_stra_trigger import _make_trig
    import execution.paths as paths
    monkeypatch.setattr(paths, "GROUP_DIR", str(tmp_path))
    stra = _make_trig()
    stra._volscale = 10.0
    stra._pricetick = 1.0
    stra._last_n = 30.0
    stra._t0 = _t.time()
    return stra


def _write_journal(tmp_path, events, t0):
    jdir = tmp_path / "generated" / "exec_journal"
    jdir.mkdir(parents=True, exist_ok=True)
    with open(jdir / "exec_20261029.jsonl", "a", encoding="utf-8") as f:
        for i, ev in enumerate(events):
            ev.setdefault("ts", t0 + 0.1 + 0.05 * i)
            f.write(json.dumps(ev) + "\n")


def test_open_intent_confirmed_by_orders_evidence(tmp_path, monkeypatch):
    """W2/W3 恢复: 目标账到位 + localid 归因链全成 → orders vwap 确认。"""
    stra = _mk_live(tmp_path, monkeypatch)
    ctx = _Ctx()
    sub = stra._subs[0]
    stra._begin_position_intent(ctx, "enter", sub, {
        "decision": "enter", "direction": 1, "price": 3400.0,
        "n": 30.0}, 7)
    ctx.pos = 7.0                                # set_position 已落地
    _write_journal(tmp_path, [
        {"type": "order", "code": CODE, "localid": 21, "islong": True,
         "isopen": True, "total": 7},
        {"type": "trade", "code": CODE, "localid": 21, "volume": 3,
         "price": 3400.0},
        {"type": "trade", "code": CODE, "localid": 21, "volume": 4,
         "price": 3406.0},
    ], stra._t0)
    stra._process_open_intents(ctx)
    assert stra._engine.direction == 1
    vwap = (3 * 3400.0 + 4 * 3406.0) / 7.0
    assert stra._engine.layers[-1][0] == 7
    assert abs(stra._engine.layers[-1][1] - vwap) < 1e-9
    assert stra._open_intents == {}
    assert any("evidence=orders" in m for _l, m in ctx.logs)


def test_exit_confirmed_by_orders_evidence(tmp_path, monkeypatch):
    stra = _mk_live(tmp_path, monkeypatch)
    ctx = _Ctx()
    stra._engine.confirm_enter(1, 10, 3400.0, n=30.0)
    stra._sync_risk_book(30.0)
    ctx.pos = 10.0
    stra._begin_exit_order(ctx, "S1", stra._engine, {
        "reason": "stop", "price": 3340.0})
    ctx.pos = 0.0
    _write_journal(tmp_path, [
        {"type": "order", "code": CODE, "localid": 31, "islong": True,
         "isopen": False, "total": 10},
        {"type": "trade", "code": CODE, "localid": 31, "volume": 10,
         "price": 3341.5},
    ], stra._t0)
    stra._process_exit_orders(ctx)
    assert stra._engine.direction == 0
    assert stra._exit_orders == {}
    assert any("evidence=orders" in m for _l, m in ctx.logs)


def test_dual_mixed_exit_resolved_by_orders_evidence(tmp_path, monkeypatch):
    """跨零 mixed 批次: 归因链全成 → 精确确认, 不再 UNKNOWN 封锁。

    镜像从净仓初值 (L6/S0) 起步, 真实簿 L10/S4 —— 首单平多 10 手
    触发簿反推, 签名重算为 [平多10, 开空6], 与执行单元实际拆单同构。"""
    import time as _t
    from test_dual_system import _mk_dual
    from execution.batch import TargetIntent
    import execution.paths as paths
    monkeypatch.setattr(paths, "GROUP_DIR", str(tmp_path))
    stra = _mk_dual(units=2)
    stra._volscale = 10.0
    e1 = stra._subs[0]["engine"]
    e2 = stra._subs[1]["engine"]
    e1.confirm_enter(1, 10, 3400.0)              # S1 多 10
    e2.confirm_enter(-1, 4, 3390.0)              # S2 空 4 → 净 +6
    stra._sync_risk_book(30.0)
    e1.begin_exit("stop", 3345.0)
    e2.begin_exit("stop", 3335.0)
    ctx = _Ctx(pos=6.0)
    t0 = _t.time()
    stra._begin_dual_exit(ctx, TargetIntent(CODE, 6, -10), [e1, e2])
    ctx.pos = -10.0
    _write_journal(tmp_path, [
        {"type": "order", "code": CODE, "localid": 41, "islong": True,
         "isopen": False, "total": 10},
        {"type": "order", "code": CODE, "localid": 42, "islong": False,
         "isopen": True, "total": 6},
        {"type": "trade", "code": CODE, "localid": 41, "volume": 10,
         "price": 3345.0},
        {"type": "trade", "code": CODE, "localid": 42, "volume": 6,
         "price": 3335.0},
    ], t0)
    stra._process_exit_orders(ctx)
    assert stra._exit_orders == {}
    assert e1.direction == 0 and e2.direction == 0
    assert any("evidence=orders" in m for _l, m in ctx.logs)
    assert not any("dual_exit_mixed" in m for _l, m in ctx.logs)
