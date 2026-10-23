"""共享账本组合回测纯函数测试 (2026-10-20)"""
import os
from pathlib import Path

import numpy as np
import pytest

from conftest import *  # noqa: F401,F403
from run_portfolio_bt import (fee_cost, load_bars, need_bars, PortfolioSim,
                              channel_at, build_channels)
from schemas import TurtleBaseConfig


def test_fee_cost_by_volume():
    fee = {"open": 2.5, "close": 2.5, "byvolume": True}
    assert fee_cost(fee, "open", 3400, 10, 10) == 25.0
    assert fee_cost(fee, "close", 3400, 3, 10) == 7.5


def test_fee_cost_by_turnover_rate():
    fee = {"open": 2e-05, "close": 2e-05, "byvolume": False}
    # 3400 * 10 * 10 = 340000; * 2e-05 = 6.8
    assert fee_cost(fee, "open", 3400, 10, 10) == pytest.approx(6.8)
    assert fee_cost({}, "open", 3400, 10, 10) == 0.0


def test_fee_cost_closetoday():
    # P2-2: DCE/CZCE 平今费率与隔夜平仓不同 (如 DCE.m close=1.5/closetoday=0)
    fee = {"open": 1.5, "close": 1.5, "closetoday": 0.0, "byvolume": True}
    # 隔日平仓 → close 费率
    assert fee_cost(fee, "close", 3000, 10, 10) == 15.0
    # 当仓平今 → closetoday 费率 (0 元)
    assert fee_cost(fee, "close", 3000, 10, 10, today=True) == 0.0
    # 无 closetoday 键 → 回退 close 费率
    fee2 = {"open": 1.5, "close": 1.5, "byvolume": True}
    assert fee_cost(fee2, "close", 3000, 10, 10, today=True) == 15.0
    # byvolume=False 口径: INE.SC open/close 20 元, 平今免
    fee3 = {"open": 20.0, "close": 20.0, "closetoday": 0.0,
            "byvolume": False}
    assert fee_cost(fee3, "close", 500, 10, 1000, today=True) == 0.0


def test_load_bars_parses_date_formats(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text(
        "date,open,high,low,close,volume,turnover,open_interest\n"
        "2019/01/02,3400,3410,3390,3405,100,0,500\n"
        "20190103,3405,3420,3400,3415,120,0,600\n"
        "bad,1,2,3,4,5,6,7\n", encoding="utf-8")
    b = load_bars(str(p))
    assert list(b["dates"]) == [20190102, 20190103]
    assert b["index"][20190103] == 1
    assert b["closes"][1] == 3415
    assert b["volumes"][1] == 120


def test_load_bars_missing_file(tmp_path):
    assert load_bars(str(tmp_path / "none.csv")) == {}


def test_need_bars_covers_all_windows():
    base = TurtleBaseConfig(bar_cnt=120)
    assert need_bars(base) >= base.s2_entry + 2


def test_channels_use_past_only_and_match_length():
    base = TurtleBaseConfig()
    n = 90
    h = np.linspace(100, 140, n)
    l = h - 2
    c = h - 1
    bars = {"highs": h, "lows": l, "closes": c}
    atr, chan = build_channels(bars, base, "S1")
    assert len(atr) == n
    ch = channel_at(chan, 60)
    # 通道不含当根: upper[60] == max(highs[40:60])
    assert ch["entry_upper"] == pytest.approx(float(np.max(h[40:60])))
    assert np.isnan(atr[10])


# ----------------------------------------------------------------------
# trigger 模式 (2026-10-21): 日线武装 + m5 触碰仿真
# ----------------------------------------------------------------------
_D1_HEAD = "date,open,high,low,close,volume,turnover,open_interest"
_M5_HEAD = "date,time,open,high,low,close,volume,turnover,open_interest"


def _w(p, head, rows):
    with open(p, "w", encoding="utf-8") as f:
        f.write(head + "\n")
        for r in rows:
            f.write(",".join(str(x) for x in r) + "\n")


def _flat_d1(n=59, start=20200101):
    """59 根横盘 bar (o=100 h=101 l=99 c=100) + 1 根放量突破 bar
    (o=100 h=105 l=100 c=104), 日期连续。"""
    rows = [[start + i, 100, 101, 99, 100, 1000, 0, 500]
            for i in range(n)]
    rows.append([start + n, 100, 105, 100, 104, 2000, 0, 600])
    return rows


def _mk_trigger_sim(tmp_path, m5_rows=None, direction="both",
                    slippage=0, d1_rows=None):
    csv_dir = Path(tmp_path) / "csv"
    csv_dir.mkdir()
    _w(csv_dir / "SHFE.rb.HOT_d.csv", _D1_HEAD,
       d1_rows if d1_rows is not None else _flat_d1())
    if m5_rows is not None:
        _w(csv_dir / "SHFE.rb.HOT_m5.csv", _M5_HEAD, m5_rows)
    cfgp = Path(tmp_path) / "cfg.yaml"
    cfgp.write_text(
        "base:\n  unit_var: 200000\n"
        "instruments:\n"
        "  - code: SHFE.rb.HOT\n"
        f"    direction: {direction}\n    units: 1\n",
        encoding="utf-8")
    from core.ConfigLoader import ConfigLoader
    cfg = ConfigLoader.load(str(cfgp), warn=False)
    contracts = {("SHFE", "rb"): {"volscale": 10, "pricetick": 1}}
    fees = {"SHFE.rb": {"open": 1.5, "close": 1.5, "closetoday": 0.0,
                        "byvolume": True}}
    return PortfolioSim(cfg, contracts, fees, 10_000_000.0, 0.9,
                        slippage, entry_mode="trigger",
                        data_dir=str(csv_dir))


_BREAK_M5 = [
    # 20200160 日盘: 09:10 那根 high=101.2 触碰武装位 101
    [20200160, "09:05:00", 100.0, 100.8, 100.0, 100.5, 10, 0, 0],
    [20200160, "09:10:00", 100.5, 101.2, 100.4, 101.0, 10, 0, 0],
    [20200160, "09:15:00", 101.0, 101.5, 100.9, 101.2, 10, 0, 0],
]


def test_trigger_entry_fills_at_armed_level():
    """m5 触碰触发: 成交价=武装位 101 (非 d1 收盘 104), 加仓锚/止损立即可用"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        sim = _mk_trigger_sim(tmp, m5_rows=_BREAK_M5)
        res = sim.run(20200160, 20200160)
    e = sim.engines["SHFE.rb.HOT"]
    assert e.direction == 1
    assert e.layers[0][1] == pytest.approx(101.0)     # 价格=武装位
    assert e.layers[0][0] > 0                          # 手数被正确传入 (>0)
    assert res["trades"] == 1
    assert res["daily"][0]["positions"] == 1


def test_trigger_same_day_stop_uses_closetoday_fee():
    """当仓当日止损: 出场费按 closetoday (0 元), 不按 close (1.5元/手)"""
    import tempfile
    # 止损 bar 后不再给新 bar: 否则止损 97 同时触及空头武装位 99,
    # 引擎会按设计立即反手 (check_bar 每 bar 至多一个决策, 下根 bar 才触发)
    m5 = _BREAK_M5 + [
        [20200160, "09:30:00", 100.8, 100.9, 96.5, 97.0, 10, 0, 0],
    ]
    with tempfile.TemporaryDirectory() as tmp:
        sim = _mk_trigger_sim(tmp, m5_rows=m5)
        res = sim.run(20200160, 20200160)
    e = sim.engines["SHFE.rb.HOT"]
    assert e.direction == 0
    assert res["trades"] == 2
    # 手数 = 单笔风险闸 2%×10M / (2N×volscale) = 200000/40 = 5000
    # 平今口径: fee = 开仓 5000×1.5 = 7500 (平仓 0 元);
    # 若误按隔夜 close: fee = 10000 → 差值即平今费率回归的判据
    assert sim.fee_paid == pytest.approx(7500.0)
    assert sim.realized == pytest.approx(-200000.0)   # (97-101)×5000×10
    assert res["daily"][0]["positions"] == 0
    assert sim.open_dates == {}        # 出场后开仓日清理


def test_trigger_direction_gate_disarms_at_arm():
    """direction=short: 武装时方向闸否决多头 → 当日不再触发"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        sim = _mk_trigger_sim(tmp, m5_rows=_BREAK_M5, direction="short")
        res = sim.run(20200160, 20200160)
    assert sim.engines["SHFE.rb.HOT"].direction == 0
    assert res["trades"] == 0


def test_trigger_degrades_to_d1_bar_without_m5():
    """无 m5: 退化 d1 bar 触碰 (与实盘 bt_no_simtick 同口径) — 日 bar
    high=105 触碰武装位 → 触发 @101"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        sim = _mk_trigger_sim(tmp, m5_rows=None)
        res = sim.run(20200160, 20200160)
    e = sim.engines["SHFE.rb.HOT"]
    assert "SHFE.rb.HOT" not in sim.m5      # 确认走了退化路径
    assert e.direction == 1
    assert e.layers[0][1] == pytest.approx(101.0)
    assert res["trades"] == 1
