# -*- coding: utf-8 -*-
"""run_speed_study 的日 PnL 估值口径回归。"""
import numpy as np
import pytest

from research.run_speed_study import sim_speed, UNIT_VAR


def test_gap_stop_pnl_stops_at_open_not_close():
    """旧持仓在开盘止损后，不得再计入 open→close 的无持仓时段。

    前 5 根横盘，第 6 根突破入场 @105，N=10 → 止损 85；下一根
    open=80 跳空越过止损，应按 80 计退出。旧实现按 close=78 计，
    会多计 (80-78) 的无持仓价格波动。
    """
    dates = np.arange(20200101, 20200108)
    opens = np.array([100, 100, 100, 100, 100, 100, 80], dtype=float)
    highs = np.array([101, 101, 101, 101, 101, 110, 82], dtype=float)
    lows = np.array([99, 99, 99, 99, 99, 100, 75], dtype=float)
    closes = np.array([100, 100, 100, 100, 100, 105, 78], dtype=float)
    atr = np.full(len(closes), 10.0)
    result = sim_speed(dates, opens, highs, lows, closes, atr,
                       entry=2, exit_=2, fs=3, system="S2")
    lots = UNIT_VAR / 10.0
    assert result["pnl"][5] == 0.0          # 突破收盘才入场，不计此前涨跌
    # 上一根收盘 105 → 实际开盘 80 出场；不能继续计到收盘 78
    assert result["pnl"][6] == pytest.approx(lots * (80.0 - 105.0))
    assert result["pnl"][6] != pytest.approx(lots * (78.0 - 105.0))
