"""TurtleEngine 信号状态机测试

覆盖: S1/S2 入场出场(边沿触发), 2N 止损, 加仓层数/步长,
S1 盈利跳过过滤, fail-safe, sim 单推进, 持久化, 换月 re-anchor。
"""
from conftest import *  # noqa: F401,F403
from helpers.TurtleEngine import TurtleEngine


def _ch(entry=100.0, exit_=90.0, fs=110.0, prev_entry=None,
        prev_exit=None, prev_fs=None):
    """构造通道 dict (多头方向数值; 空头用镜像)"""
    return {
        'entry_upper': entry, 'entry_lower': -entry,
        'exit_upper': -exit_, 'exit_lower': exit_,
        'fs_upper': fs, 'fs_lower': -fs,
        'prev_entry_upper': prev_entry if prev_entry is not None else entry,
        'prev_entry_lower': -(prev_entry if prev_entry is not None else entry),
        'prev_fs_upper': prev_fs if prev_fs is not None else fs,
        'prev_fs_lower': -(prev_fs if prev_fs is not None else fs),
    }


def _mk_engine(system="S1", layers=4, step=0.5, stop=2.0):
    return TurtleEngine(system=system, stop_atr_mult=stop,
                        pyramid_layers=layers, pyramid_step_atr=step)


# ---------- S1 入场/出场 ----------

def test_s1_entry_on_fresh_breakout():
    e = _mk_engine("S1")
    ch = _ch(entry=100.0)
    dec = e.on_bar(high=101, low=99, close=101, prev_close=99, n=5.0,
                   channels=ch)
    assert len(dec) == 1 and dec[0]["decision"] == "enter"
    assert dec[0]["direction"] == 1


def test_s1_no_entry_without_edge():
    """收盘持续站上通道但无边沿 → 不重复触发"""
    e = _mk_engine("S1")
    ch = _ch(entry=100.0, prev_entry=99.0)   # prev_close=101 > prev_upper=99 → 无边沿
    dec = e.on_bar(high=102, low=100, close=101, prev_close=101, n=5.0,
                   channels=ch)
    assert dec == []


def test_s1_short_entry():
    e = _mk_engine("S1")
    ch = _ch(entry=100.0)
    dec = e.on_bar(high=-99, low=-101, close=-101, prev_close=-99, n=5.0,
                   channels=ch)
    assert dec[0]["decision"] == "enter" and dec[0]["direction"] == -1


def test_s1_exit_on_channel_break():
    e = _mk_engine("S1")
    e.confirm_enter(1, 10, 101.0)
    ch = _ch(exit_=90.0)
    # stop = 101-2*5 = 91, low=92 未触碰; close=89 < exit_lower=90 → 通道出场
    dec = e.on_bar(high=95, low=92, close=89, prev_close=95, n=5.0,
                   channels=ch)
    assert dec[0]["decision"] == "exit" and dec[0]["reason"] == "channel"
    assert dec[0]["price"] == 89


def test_s1_stop_loss_2n():
    """止损 = 最后加仓价 - 2N, bar low 触碰即触发, 按止损价离场"""
    e = _mk_engine("S1", stop=2.0)
    e.confirm_enter(1, 10, 100.0)
    ch = _ch(exit_=0.0)     # 通道出场远离
    # stop = 100 - 2*5 = 90; low=89 触碰
    dec = e.on_bar(high=95, low=89, close=93, prev_close=95, n=5.0,
                   channels=ch)
    assert dec[0]["decision"] == "exit" and dec[0]["reason"] == "stop"
    assert dec[0]["price"] == 90.0


def test_s1_same_bar_reversal():
    """平仓与新入场同 bar: 破 20-low 蕴含破 10-low → exit + enter"""
    e = _mk_engine("S1")
    e.confirm_enter(1, 10, 100.0)
    ch = _ch(entry=100.0, exit_=90.0)
    # close=-101: 破 exit_lower(90) 且破 entry_lower(-100) 且带边沿
    dec = e.on_bar(high=90, low=-102, close=-101, prev_close=0, n=5.0,
                   channels=ch)
    kinds = [d["decision"] for d in dec]
    assert kinds == ["exit", "enter"]
    assert dec[1]["direction"] == -1


# ---------- 加仓 ----------

def test_pyramid_add_every_half_n():
    e = _mk_engine("S1", layers=4, step=0.5)
    e.confirm_enter(1, 10, 100.0)
    ch = _ch(exit_=0.0)
    # 100 + 0.5*5 = 102.5; close=103 ≥ 102.5 → add
    dec = e.on_bar(high=104, low=101, close=103, prev_close=101, n=5.0,
                   channels=ch)
    assert dec[0]["decision"] == "add"
    e.confirm_add(10, 103.0)
    # 下一层锚 = 103 + 2.5 = 105.5; close=105 不到 → 不加
    dec = e.on_bar(high=106, low=104, close=105, prev_close=103, n=5.0,
                   channels=ch)
    assert dec == []
    # close=106 ≥ 105.5 → add
    dec = e.on_bar(high=107, low=105, close=106, prev_close=105, n=5.0,
                   channels=ch)
    assert dec[0]["decision"] == "add"


def test_pyramid_max_layers():
    e = _mk_engine("S1", layers=2, step=0.5)
    e.confirm_enter(1, 10, 100.0)
    e.confirm_add(10, 102.5)
    ch = _ch(exit_=0.0)
    dec = e.on_bar(high=110, low=104, close=110, prev_close=104, n=5.0,
                   channels=ch)
    assert dec == []   # 已达 2 层上限


def test_stop_uses_last_layer_price():
    """加仓后止损锚 = 最后加仓价 - 2N (整体上移)"""
    e = _mk_engine("S1", stop=2.0)
    e.confirm_enter(1, 10, 100.0)
    e.confirm_add(10, 102.5)
    ch = _ch(exit_=0.0)
    # stop = 102.5 - 2*5 = 92.5 (非首仓的 90)
    dec = e.on_bar(high=100, low=92.0, close=95, prev_close=100, n=5.0,
                   channels=ch)
    assert dec[0]["reason"] == "stop" and dec[0]["price"] == 92.5


# ---------- S2 ----------

def test_s2_entry_no_filter():
    """S2 无盈利跳过: 上次赢也照样入场"""
    e = _mk_engine("S2")
    e.s1_last = {"direction": 1, "entry_price": 90, "n": 5.0,
                 "taken": True, "status": "closed", "result": "win"}
    ch = _ch(entry=100.0)
    dec = e.on_bar(high=101, low=99, close=101, prev_close=99, n=5.0,
                   channels=ch)
    assert dec[0]["decision"] == "enter"


# ---------- S1 过滤器 ----------

def test_s1_skip_after_winner():
    """上次实单盈利 → 本次信号跳过, 记 sim 单 + 武装 fail-safe"""
    e = _mk_engine("S1")
    e.s1_last = {"direction": 1, "entry_price": 90, "n": None,
                 "taken": True, "status": "closed", "result": "win"}
    ch = _ch(entry=100.0, fs=110.0)   # fs=110, close=101 未破
    dec = e.on_bar(high=101, low=99, close=101, prev_close=99, n=5.0,
                   channels=ch)
    assert dec[0]["decision"] == "skip"
    assert e.direction == 0
    assert e.s1_last["taken"] is False and e.s1_last["status"] == "open"
    assert e._fs_armed == 1


def test_s1_no_skip_after_loser():
    e = _mk_engine("S1")
    e.s1_last = {"direction": 1, "entry_price": 90, "n": None,
                 "taken": True, "status": "closed", "result": "loss"}
    ch = _ch(entry=100.0)
    dec = e.on_bar(high=101, low=99, close=101, prev_close=99, n=5.0,
                   channels=ch)
    assert dec[0]["decision"] == "enter"


def test_s1_failsafe_entry_on_55_break():
    """被跳过后价格继续破 55 → 强制入场 (fail-safe)

    场景: bar1 破 20 被跳过(上次赢) → bar2 无边沿但破 55 → 入场
    """
    e = _mk_engine("S1")
    e.s1_last = {"direction": 1, "entry_price": 90, "n": None,
                 "taken": True, "status": "closed", "result": "win"}
    # bar1: 破 20(entry=100) 被跳过, 未破 55(fs=110)
    ch1 = _ch(entry=100.0, fs=110.0)
    dec = e.on_bar(high=101, low=99, close=101, prev_close=99, n=5.0,
                   channels=ch1)
    assert dec[0]["decision"] == "skip"
    # bar2: 无 20 边沿(prev_close=101 > prev_upper=100), 但破 55 边沿
    ch2 = _ch(entry=100.0, fs=110.0, prev_fs=111.0)
    dec = e.on_bar(high=113, low=108, close=112, prev_close=101, n=5.0,
                   channels=ch2)
    assert dec[0]["decision"] == "enter" and dec[0]["failsafe"] is True


def test_s1_sim_stop_clears_filter():
    """被跳过的 sim 单 2N 止损 → 过滤器解除, 下个信号正常入场"""
    e = _mk_engine("S1")
    e.s1_last = {"direction": 1, "entry_price": 90, "n": None,
                 "taken": True, "status": "closed", "result": "win"}
    ch1 = _ch(entry=100.0, fs=110.0)
    e.on_bar(high=101, low=99, close=101, prev_close=99, n=5.0,
             channels=ch1)   # skip, sim@101 n=5
    # bar2: sim 止损 = 101-2*5=91, low=90 触碰
    e.on_bar(high=100, low=90, close=95, prev_close=101, n=5.0,
             channels=_ch(entry=100.0))
    assert e.s1_last["result"] == "loss" and e._fs_armed == 0
    # bar3: 价格重新站上 (先跌回通道内) 再新鲜突破 → 正常入场
    e.on_bar(high=99, low=94, close=98, prev_close=95, n=5.0,
             channels=_ch(entry=100.0))
    dec = e.on_bar(high=102, low=97, close=101, prev_close=98, n=5.0,
                   channels=_ch(entry=100.0, prev_entry=102.0))
    assert dec and dec[0]["decision"] == "enter"


def test_s1_sim_winner_via_channel_exit():
    """sim 单走 10 通道盈利出场 → closed/win"""
    e = _mk_engine("S1")
    e.s1_last = {"direction": 1, "entry_price": 90, "n": None,
                 "taken": True, "status": "closed", "result": "win"}
    ch1 = _ch(entry=100.0, fs=110.0, exit_=90.0)
    e.on_bar(high=101, low=99, close=101, prev_close=99, n=5.0,
             channels=ch1)   # skip → sim@101
    # sim 浮盈后退场: close=95 < exit_lower=90? 不破 → 仍 open; 再一根破
    e.on_bar(high=100, low=94, close=95, prev_close=101, n=5.0,
             channels=_ch(exit_=90.0))
    assert e.s1_last["status"] == "open"
    e.on_bar(high=94, low=88, close=89, prev_close=95, n=5.0,
             channels=_ch(exit_=90.0))
    assert e.s1_last["status"] == "closed" and e.s1_last["result"] == "loss"


# ---------- 实单结果回写过滤器 ----------

def test_real_trade_win_recorded():
    e = _mk_engine("S1")
    e.confirm_enter(1, 10, 100.0)
    e.confirm_exit(110.0)
    assert e.s1_last["result"] == "win" and e.s1_last["status"] == "closed"


def test_real_trade_loss_recorded():
    e = _mk_engine("S1")
    e.confirm_enter(1, 10, 100.0)
    e.confirm_exit(90.0)
    assert e.s1_last["result"] == "loss"


# ---------- 持久化 / 换月 ----------

def test_snapshot_restore_roundtrip():
    e = _mk_engine("S1")
    e.confirm_enter(1, 10, 100.0)
    e.confirm_add(10, 102.5)
    snap = e.snapshot()
    e2 = _mk_engine("S1")
    e2.restore(snap)
    assert e2.direction == 1
    assert e2.layers == [[10, 100.0], [10, 102.5]]


def test_shift_prices_reanchors_layers():
    """换月: 各层价格平移 shift, 止损锚随之移动"""
    e = _mk_engine("S1")
    e.confirm_enter(1, 10, 100.0)
    e.shift_prices(20.0)   # 新合约升水 20
    assert e.layers[0][1] == 120.0
    ch = _ch(exit_=0.0)
    # stop = 120 - 2*5 = 110
    dec = e.on_bar(high=118, low=109, close=115, prev_close=118, n=5.0,
                   channels=ch)
    assert dec[0]["reason"] == "stop" and dec[0]["price"] == 110.0


def test_nan_n_no_action():
    e = _mk_engine("S1")
    dec = e.on_bar(high=101, low=99, close=101, prev_close=99,
                   n=float('nan'), channels=_ch())
    assert dec == []
