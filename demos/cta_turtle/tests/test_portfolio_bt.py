"""共享账本组合回测纯函数测试 (2026-10-20)"""
import os
from pathlib import Path

import numpy as np
import pytest

from conftest import *  # noqa: F401,F403
from run_portfolio_bt import (fee_cost, fee_cfg, load_bars, need_bars,
                              PortfolioSim, channel_at, build_channels,
                              _roll_adjusted_hlc)
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
                    slippage=0, d1_rows=None, system="S1", units=1):
    csv_dir = Path(tmp_path) / "csv"
    csv_dir.mkdir(exist_ok=True)
    _w(csv_dir / "SHFE.rb.HOT_d.csv", _D1_HEAD,
       d1_rows if d1_rows is not None else _flat_d1())
    if m5_rows is not None:
        _w(csv_dir / "SHFE.rb.HOT_m5.csv", _M5_HEAD, m5_rows)
    cfgp = Path(tmp_path) / "cfg.yaml"
    cfgp.write_text(
        "base:\n  unit_var: 200000\n"
        "instruments:\n"
        "  - code: SHFE.rb.HOT\n"
        f"    system: {system}\n"
        f"    direction: {direction}\n    units: {units}\n",
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


def test_multi_profile_same_direction_tracks_gross_and_net(tmp_path):
    """S1+S2 同向触发：引擎/风险账/费用按 profile gross，执行目标按 net。"""
    sim = _mk_trigger_sim(tmp_path, m5_rows=_BREAK_M5,
                          system="S1+S2", units=2)
    res = sim.run(20200160, 20200160)
    subs = sim.subs["SHFE.rb.HOT"]
    l1 = subs[0]["engine"].layers[0][0]
    l2 = subs[1]["engine"].layers[0][0]
    assert subs[0]["engine"].direction == 1
    assert subs[1]["engine"].direction == 1
    assert l1 > 0 and l2 > 0
    assert sim.risk.entry("SHFE.rb.HOT", "S1")["lots"] == l1
    assert sim.risk.entry("SHFE.rb.HOT", "S2")["lots"] == l2
    assert sim.broker_pos["SHFE.rb.HOT"] == l1 + l2      # net 执行目标
    assert res["orders"] == 1                            # 同批一次 net 变化
    assert sim.fee_paid == pytest.approx((l1 + l2) * 1.5)  # gross 费用
    assert sim._gross_margin() == pytest.approx(
        sim._profile_margin("SHFE.rb.HOT", "S1") +
        sim._profile_margin("SHFE.rb.HOT", "S2"))


def test_multi_profile_units_one_keeps_s1_result_and_s2_flat(tmp_path):
    """legacy units=1：S2 share/units=0，只跑信号状态不产仓位；S1 结果与
    单 profile 等价（S2 的 sizing 拒绝计数是显式口径，不比较）。"""
    multi = _mk_trigger_sim(tmp_path, m5_rows=_BREAK_M5,
                            system="S1+S2", units=1)
    single = _mk_trigger_sim(tmp_path, m5_rows=_BREAK_M5,
                             system="S1", units=1)
    rm = multi.run(20200160, 20200160)
    rs = single.run(20200160, 20200160)
    assert multi.subs["SHFE.rb.HOT"][1]["engine"].direction == 0
    assert rm["daily"] == rs["daily"]
    assert rm["trades"] == rs["trades"] == 1
    assert rm["realized"] == pytest.approx(rs["realized"])
    assert multi.subs["SHFE.rb.HOT"][0]["engine"].layers == \
        single.engines["SHFE.rb.HOT"].layers


def test_multi_profile_reverse_positions_keep_gross_risk_and_net_target(tmp_path):
    """反向并存不互抵：风险/保证金按 gross，broker net 才取净额。"""
    sim = _mk_trigger_sim(tmp_path, m5_rows=_BREAK_M5,
                          system="S1+S2", units=2)
    code = "SHFE.rb.HOT"
    e1 = sim.subs[code][0]["engine"]
    e2 = sim.subs[code][1]["engine"]
    e1.confirm_enter(1, 10, 100.0, n=2.0)
    e2.confirm_enter(-1, 4, 100.0, n=2.0)
    sim._sync_book(code)
    sim._settle_broker(code)
    assert sim.risk.entry(code, "S1")["dir"] == 1
    assert sim.risk.entry(code, "S2")["dir"] == -1
    assert sim.broker_pos[code] == 6
    assert sim._gross_margin() > 0
    # gross 两边都计保证金；net 6 手的反例口径不会把保证金压到 0。
    assert sim._gross_margin() == pytest.approx(
        14 * 100.0 * 10 * sim.margin_rate)


def test_trigger_sizing_uses_event_mark_not_daily_close(tmp_path):
    """盘中估值不得提前使用当日 d1 close=104。"""
    sim = _mk_trigger_sim(tmp_path, m5_rows=_BREAK_M5)
    seen = []
    original = sim._size_cap

    def spy(code, fill, n, *args, **kwargs):
        seen.append(sim.marks.get(code))
        return original(code, fill, n, *args, **kwargs)

    sim._size_cap = spy
    sim.run(20200160, 20200160)
    assert seen and seen[0] == pytest.approx(100.5)  # 触发 m5 的 open
    assert all(mark != 104.0 for mark in seen)


def test_fee_key_case_and_bad_rate_fail_loud():
    fee = fee_cfg("INE.sc.HOT", {"INE.SC": {"open": 20.0,
                                             "byvolume": True}})
    assert fee["open"] == 20.0
    with pytest.raises(KeyError, match="缺少费率"):
        fee_cfg("INE.sc.HOT", {})
    with pytest.raises(ValueError, match="比例费率单位"):
        fee_cost({"open": 20.0, "byvolume": False}, "open", 500, 1, 1000)


def test_exit_fee_is_computed_per_layer_today(tmp_path):
    sim = _mk_trigger_sim(tmp_path, m5_rows=_BREAK_M5)
    code = "SHFE.rb.HOT"
    e = sim.engines[code]
    e.confirm_enter(1, 2, 100.0, n=2.0)
    e.confirm_add(3, 105.0, n=2.0)
    sim.open_dates[code] = [20200159, 20200160]   # 昨仓 + 当仓
    sim.fee_paid = 0.0
    i = sim.data[code]["index"][20200160]
    sim._exit(code, i, {"price": 97.0, "reason": "stop"}, tdate=20200160)
    # 昨层 2×1.5，今层 3×0；旧口径会按首层日期对全部 5 手收 0 或 7.5。
    assert sim.fee_paid == pytest.approx(3.0)


def test_roll_reanchors_layers_marks_and_is_idempotent(tmp_path):
    sim = _mk_trigger_sim(tmp_path, m5_rows=_BREAK_M5)
    code = "SHFE.rb.HOT"
    e = sim.engines[code]
    e.confirm_enter(1, 2, 100.0, n=2.0)
    sim.marks[code] = 100.0
    sim._sync_book(code)
    sim.roll_calendar = {code: [{"date": 20200160, "delta": 20.0,
                                 "from": "rb1", "to": "rb2"}]}
    sim._apply_rolls(20200160)
    assert e.layers[0][1] == pytest.approx(120.0)
    assert sim.marks[code] == pytest.approx(120.0)
    assert len(sim.rolls) == 1
    sim._apply_rolls(20200160)
    assert e.layers[0][1] == pytest.approx(120.0)  # 不重复平移


def test_roll_trigger_arm_exclude_uses_new_coordinate_channels(tmp_path):
    """默认 arm_exclude_signal=True 在换月日必须用 chan[i]（历史已平移且
    仍排除信号根），不能用 chan[i-1] 的旧合约坐标覆盖已平移的引擎锚。"""
    sim = _mk_trigger_sim(tmp_path)
    code = "SHFE.rb.HOT"
    i = sim.data[code]["index"][20200160]
    sim.roll_calendar = {code: [{"date": 20200160, "delta": 20.0,
                                 "from": "rb1", "to": "rb2"}]}
    # 正常构造路径在初始化时即用 roll_calendar 建指标；测试中后置赋值
    # 后需同步重建，保持与真实 runner 口径一致。
    profile = sim.profile_specs[code]
    atr, chan = build_channels(sim.data[code], sim.base, profile.system,
                               profile=profile,
                               roll_rules=sim.roll_calendar[code])
    sim.chans[code] = {"atr": atr, "chan": chan,
                       "by_profile": {profile.key: chan}}
    sim._apply_rolls(20200160)
    sim._arm_day(code, i)
    assert sim.engines[code].armed["long"] == pytest.approx(121.0)
    old_coordinate = channel_at(sim.chans[code]["chan"], i - 1)
    assert old_coordinate["entry_upper"] == pytest.approx(101.0)


def test_roll_shifts_open_s1_sim_state(tmp_path):
    """空仓 S1 模拟单也必须随换月 re-anchor；否则后续盈利跳过会用新价格
    比较旧合约坐标。"""
    sim = _mk_trigger_sim(tmp_path)
    code = "SHFE.rb.HOT"
    e = sim.engines[code]
    e.s1_last = {"direction": 1, "entry_price": 100.0, "n": 1.0,
                 "taken": False, "status": "open", "result": None}
    sim.roll_calendar = {code: [{"date": 20200160, "delta": 20.0,
                                 "from": "rb1", "to": "rb2"}]}
    sim._apply_rolls(20200160)
    assert e.s1_last["entry_price"] == pytest.approx(120.0)


def test_roll_adjusted_indicators_do_not_spike_on_join():
    dates = np.array([1, 2, 3, 4], dtype=np.int64)
    closes = np.array([100.0, 101.0, 120.0, 121.0])
    bars = {"dates": dates, "opens": closes.copy(),
            "highs": closes + 1.0, "lows": closes - 1.0,
            "closes": closes.copy(),
            "index": {int(d): i for i, d in enumerate(dates)}}
    base = TurtleBaseConfig(atr_period=2, s1_entry=2, s1_exit=2,
                            s2_entry=2, s2_exit=2)
    rules = [{"date": 3, "delta": 20.0}]
    atr, chan = build_channels(bars, base, "S1", roll_rules=rules)
    raw_atr, raw_chan = build_channels(bars, base, "S1")
    # 换月前不受未来规则影响；换月后历史已平移，ATR 不再含拼接跳空。
    assert _roll_adjusted_hlc(bars, 1, rules)[0][0] == 101.0
    assert chan["entry_upper"][2] == pytest.approx(122.0)
    assert atr[2] == pytest.approx(2.0)
    assert raw_atr[2] > atr[2]
    assert raw_chan["entry_upper"][2] == pytest.approx(102.0)
