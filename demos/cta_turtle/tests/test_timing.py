# -*- coding: utf-8 -*-
"""事件时序：当根持仓管理不能使用收盘后才确定的参数/估值。"""
from core.TurtleEngine import TurtleEngine


def _channels():
    keys = ("entry_upper", "entry_lower", "exit_upper", "exit_lower",
            "fs_upper", "fs_lower", "prev_entry_upper", "prev_entry_lower",
            "prev_exit_upper", "prev_exit_lower", "prev_fs_upper",
            "prev_fs_lower")
    return {k: 100.0 for k in keys}


def test_close_ratchet_stop_uses_previous_bar_n():
    e = TurtleEngine(filter=False, entry_mode="close", stop_n_mode="ratchet")
    e.confirm_enter(1, 1, 100.0, n=10.0)       # 根前 stop=80/add=105
    ch = _channels()

    # 当根 N 缩到 1，若回溯使用新 N 会把 stop 提到 98 并误触 low=97；
    # 正确行为是本根仍用旧 N=10，不当根止损。
    dec1 = e.on_bar(104.0, 97.0, 104.0, 100.0, 1.0, ch, open=103.0)
    assert dec1 == []

    # 下一根根前 N 已是 1，stop=98 才允许生效。
    dec2 = e.on_bar(104.0, 97.0, 104.0, 104.0, 1.0, ch, open=103.0)
    assert dec2[0]["decision"] == "exit"
    assert dec2[0]["reason"] == "stop"
    assert dec2[0]["price"] == 98.0


def test_close_previous_n_survives_snapshot_restore():
    e = TurtleEngine(filter=False, entry_mode="close", stop_n_mode="ratchet")
    e.confirm_enter(1, 1, 100.0, n=10.0)
    ch = _channels()
    e.on_bar(104.0, 97.0, 104.0, 100.0, 1.0, ch, open=103.0)

    restored = TurtleEngine(filter=False, entry_mode="close",
                            stop_n_mode="ratchet")
    restored.restore(e.snapshot())
    dec = restored.on_bar(104.0, 97.0, 104.0, 104.0, 1.0, ch, open=103.0)
    assert dec[0]["decision"] == "exit"
    assert dec[0]["price"] == 98.0
