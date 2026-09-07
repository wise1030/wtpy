"""trigger 模式 (盘中触发, 方案C) 引擎级测试

口径锚定:
- on_bar 只武装价位 (DEC_ARM), 产出含当根的包络通道
- on_price(tick)/check_bar(bar仿真) 穿越武装位触发 DEC_ENTER/ADD/EXIT
- 触发后 pending 静默, confirm_* 提交并重武装; reject_pending 解除当日武装
- S1 skip 在触发时判定: 记 sim(触发价) + 武装 fail-safe; sim 由价格推进
- check_bar: 跳空按开盘价, 触碰按武装位, 止损优先, 单次调用至多一决策
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "helpers"))

from helpers.TurtleEngine import (TurtleEngine, DEC_ENTER, DEC_ADD,
                                  DEC_EXIT, DEC_SKIP, DEC_ARM)


def _ch(eu=110.0, el=90.0, xu=105.0, xl=95.0, fu=120.0, fl=80.0):
    return {"entry_upper": eu, "entry_lower": el,
            "exit_upper": xu, "exit_lower": xl,
            "fs_upper": fu, "fs_lower": fl,
            "prev_entry_upper": eu, "prev_entry_lower": el,
            "prev_fs_upper": fu, "prev_fs_lower": fl}


def _mk(system="S2", mode="trigger", **kw):
    return TurtleEngine(system=system, entry_mode=mode, **kw)


def _arm(e, h=100.0, l=98.0, c=99.0, pc=99.0, n=2.0, ch=None):
    return e.on_bar(h, l, c, pc, n, ch or _ch())


# ------------------------------------------------------------------
# 武装
# ------------------------------------------------------------------
def test_close_mode_unchanged():
    """close 模式: on_bar 行为不变, 不产出 DEC_ARM, on_price 静默"""
    e = _mk(mode="close")
    decs = _arm(e, c=111.0, pc=100.0)          # 收盘突破 (close 口径)
    kinds = [d["decision"] for d in decs]
    assert DEC_ENTER in kinds and DEC_ARM not in kinds
    assert e.on_price(130.0) == []             # close 模式不响应 tick
    assert e.check_bar(130.0, 131.0, 129.0, 130.0) == []


def test_arm_envelope_includes_today():
    """武装位 = 通道(不含当根) 与当根 high/low 的包络"""
    e = _mk()
    decs = _arm(e, h=112.0, l=97.0)            # 当根创出新高 112
    assert decs[0]["decision"] == DEC_ARM
    a = e.armed
    assert a["long"] == 112.0                  # max(110, 112)
    assert a["short"] == 90.0                  # min(90, 97)
    assert a["stop"] is None and a["add"] is None and a["exit"] is None


def test_arm_requires_valid_n_and_channels():
    e = _mk()
    assert e.on_bar(100, 98, 99, 99, float("nan"), _ch()) == []
    assert e.armed["long"] is None


# ------------------------------------------------------------------
# tick 触发路径
# ------------------------------------------------------------------
def test_tick_trigger_enter_and_rearm():
    """tick 穿越入场位 → DEC_ENTER(触发价); confirm 后武装止损/加仓/出场"""
    e = _mk()
    _arm(e)                                    # long=110 short=90
    assert e.on_price(109.9) == []             # 未触及
    decs = e.on_price(110.0)
    assert decs[0]["decision"] == DEC_ENTER
    assert decs[0]["direction"] == 1
    assert decs[0]["price"] == 110.0           # 按武装位报价
    assert e.on_price(111.0) == []             # pending 静默
    e.confirm_enter(1, 2, 110.0)
    a = e.armed
    assert a["stop"] == 110.0 - 2 * 2.0        # 2N 止损 = 106
    assert a["add"] == 110.0 + 0.5 * 2.0       # 0.5N 加仓 = 111
    assert a["exit"] == 95.0                   # 多头出场通道 = exit_lower
    assert a["long"] is None and a["short"] is None


def test_tick_stop_and_add_and_channel_exit():
    e = _mk()
    _arm(e)
    e.on_price(110.0)
    e.confirm_enter(1, 2, 110.0)
    # 加仓触发
    decs = e.on_price(111.0)
    assert decs[0]["decision"] == DEC_ADD and decs[0]["price"] == 111.0
    e.confirm_add(2, 111.0)
    assert e.armed["stop"] == 111.0 - 2 * 2.0  # 止损随最后加仓价上移
    # 通道出场 (touch 口径): 出场位须高于止损位才可先触及 (止损优先)
    e2 = _mk()
    _arm(e2, h=100.0, l=108.0, ch=_ch(xl=107.5))   # exit=min(107.5,108)=107.5 > stop 106
    e2.on_price(110.0)
    e2.confirm_enter(1, 2, 110.0)
    decs = e2.on_price(107.5)                  # 触及 exit_lower (未破止损)
    assert decs[0]["decision"] == DEC_EXIT
    assert decs[0]["reason"] == "channel" and decs[0]["price"] == 107.5
    e2.confirm_exit(95.0)
    assert e2.armed["long"] == 110.0           # 空仓后重新武装入场位
    # 止损触发
    e3 = _mk()
    _arm(e3)
    e3.on_price(110.0)
    e3.confirm_enter(1, 2, 110.0)
    decs = e3.on_price(106.0)
    assert decs[0]["decision"] == DEC_EXIT
    assert decs[0]["reason"] == "stop" and decs[0]["price"] == 106.0


def test_short_side_symmetric():
    e = _mk()
    _arm(e)
    decs = e.on_price(90.0)
    assert decs[0]["decision"] == DEC_ENTER and decs[0]["direction"] == -1
    e.confirm_enter(-1, 2, 90.0)
    a = e.armed
    assert a["stop"] == 90.0 + 2 * 2.0
    assert a["add"] == 90.0 - 0.5 * 2.0
    assert a["exit"] == 105.0                  # 空头出场 = exit_upper


# ------------------------------------------------------------------
# S1 过滤与 fail-safe (触发时判定)
# ------------------------------------------------------------------
def test_s1_skip_at_trigger_then_failsafe():
    e = _mk(system="S1")
    _arm(e)
    # 第一次突破: 无历史 → 直接入场
    e.on_price(110.0)
    e.confirm_enter(1, 2, 110.0)
    # 盈利出场 (110 → 105 仍是输; 拉到 120 赢)
    e.on_price(111.0)                          # 无 add? add=111 → 触发
    e.confirm_add(2, 111.0)
    e.on_price(130.0)                          # 无出场 (stop=107)
    e2 = e                                     # 重新武装由日评估完成
    _arm(e2)                                   # 新日评估
    # 制造盈利历史: 直接止损上方的 channel exit? 简化: 手动盈利出场
    e2.confirm_exit(130.0)                     # win → s1_last=win
    _arm(e2)                                   # 重新武装
    decs = e2.on_price(112.0)                  # 再次触及入场位
    assert decs[0]["decision"] == DEC_SKIP     # 上次盈利 → 跳过
    assert e2.s1_last["taken"] is False        # sim 单记录于触发价
    assert e2.s1_last["entry_price"] == 112.0
    assert e2._fs_armed == 1
    assert e2.armed["long"] is None            # 入场位当日撤除
    assert e2.armed["fs_long"] == 120.0        # fail-safe 武装
    decs = e2.on_price(120.0)                  # 触及 55 fail-safe
    assert decs[0]["decision"] == DEC_ENTER
    assert decs[0]["failsafe"] is True


def test_s1_sim_closed_by_price_releases_failsafe():
    e = _mk(system="S1")
    _arm(e)
    e.on_price(110.0)
    e.confirm_enter(1, 2, 110.0)
    e.confirm_exit(130.0)                      # win
    _arm(e)
    e.on_price(112.0)                          # skip → sim@112, n=2
    assert e._fs_armed == 1
    # sim 止损: 112 - 2*2 = 108
    e.on_price(108.0)
    assert e.s1_last["status"] == "closed"
    assert e.s1_last["result"] == "loss"
    assert e._fs_armed == 0                    # fail-safe 解除
    assert e.armed["fs_long"] is None


def test_reject_pending_disarms_direction():
    e = _mk()
    _arm(e)
    e.on_price(110.0)                          # DEC_ENTER pending
    e.reject_pending()                         # 策略层否决 (过滤/风控)
    assert e.armed["long"] is None
    assert e.on_price(115.0) == []             # 不再触发多头
    assert e.on_price(90.0)[0]["decision"] == DEC_ENTER  # 空头仍武装


# ------------------------------------------------------------------
# check_bar 回测仿真
# ------------------------------------------------------------------
def test_check_bar_gap_and_touch():
    e = _mk()
    _arm(e)
    # 跳空高开过位 → 开盘价成交
    decs = e.check_bar(111.0, 112.0, 110.5, 111.0)
    assert decs[0]["decision"] == DEC_ENTER and decs[0]["price"] == 111.0
    e.confirm_enter(1, 2, 111.0)
    # 次日: 止损被跳空击穿 → 开盘价成交
    decs = e.check_bar(100.0, 101.0, 99.0, 100.0)
    assert decs[0]["decision"] == DEC_EXIT
    assert decs[0]["reason"] == "stop" and decs[0]["price"] == 100.0


def test_check_bar_one_decision_per_call():
    """单 bar 同时满足入场+止损路径时只产一决策 (防路径不明的乐观偏差)"""
    e = _mk()
    _arm(e)
    decs = e.check_bar(100.0, 115.0, 95.0, 100.0)  # 触及多头入场
    assert len(decs) == 1 and decs[0]["decision"] == DEC_ENTER
    e.confirm_enter(1, 2, 110.0)
    # 入场后 bar 内止损不再本 bar 评估 (pending已清, 但下一根才检查)
    decs = e.check_bar(100.0, 101.0, 99.0, 100.0)
    assert decs[0]["decision"] == DEC_EXIT and decs[0]["price"] == 100.0


def test_check_bar_entry_after_exit_next_bar():
    e = _mk()
    _arm(e)
    e.check_bar(110.0, 110.0, 109.0, 110.0)
    e.confirm_enter(1, 2, 110.0)
    e.check_bar(105.0, 106.0, 104.0, 105.0)    # 止损
    e.confirm_exit(105.0)
    assert e.direction == 0
    assert e.armed["long"] == 110.0            # 空仓重新武装


# ------------------------------------------------------------------
# 快照 / 换月
# ------------------------------------------------------------------
def test_snapshot_v4_roundtrip_and_rearm():
    e = _mk(system="S1")
    _arm(e)
    e.on_price(110.0)
    e.confirm_enter(1, 2, 110.0)
    snap = e.snapshot()
    assert snap["armed"]["stop"] == 106.0
    assert snap["n"] == 2.0
    e2 = _mk(system="S1")
    e2.restore(snap)
    assert e2.direction == 1 and e2.armed["stop"] == 106.0
    decs = e2.on_price(106.0)                  # 恢复后立即可触发
    assert decs[0]["decision"] == DEC_EXIT


def test_restore_v3_compatible():
    """旧版快照 (无 armed/n/ch) 恢复后: 触发能力待下次日评估重建"""
    e = _mk()
    e.restore({"direction": 1, "layers": [[2, 110.0]],
               "s1_last": None, "fs_armed": 0})
    assert e.armed["stop"] is None
    assert e.on_price(100.0) == []             # _n 缺失 → 静默
    _arm(e)                                    # 日评估后重建
    assert e.armed["stop"] == 110.0 - 4.0


def test_shift_prices_shifts_armed_and_channels():
    e = _mk()
    _arm(e)
    e.on_price(110.0)
    e.confirm_enter(1, 2, 110.0)
    e.shift_prices(50.0)                       # 换月价差 +50
    assert e.layers[0][1] == 160.0
    assert e.armed["stop"] == 156.0
    assert e.armed["add"] == 161.0
    assert e._entry_ch == (160.0, 140.0)
