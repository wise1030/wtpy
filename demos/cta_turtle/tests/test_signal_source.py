"""Phase 1 合约信号源回归 (2026-11-10)。

口径: 移仓后新合约自身真实序列重算信号 (无复权)。
- live: hots.json 当前主力单合约 (配 20:35/20:40 运维节奏);
- bt:  common/roll_calendar.json 逐日主力 (每个历史日期指标都在当日
       主力自身序列上计算, init 预订阅全部主力合约副K);
- 品种级 signal_source 覆盖优先; 解析失败退化 hot + 告警;
- 默认 hot 零漂移 (黄金基线口径不变)。
"""
import json
import os
import sys

import numpy as np  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from test_stra_trigger import TrigCtx  # noqa: E402
from test_intrabar import _fill_daily  # noqa: E402

import StraTurtle as st_mod  # noqa: E402
from core.hotsutil import align_window  # noqa: E402
from schemas import InstrumentConfig, TurtleBaseConfig  # noqa: E402
from StraTurtle import StraTurtle  # noqa: E402


def _tmp_tree(tmp_path, hots_rules=None, calendar=None):
    """构造 dev 布局: <tmp>/grp/(=GROUP_DIR) + <tmp>/common/。"""
    grp = tmp_path / "grp"
    grp.mkdir(exist_ok=True)
    common = tmp_path / "common"
    common.mkdir(exist_ok=True)
    if hots_rules is not None:
        (common / "hots.json").write_text(json.dumps(
            {"SHFE": {"rb": hots_rules}}))
    if calendar is not None:
        (common / "roll_calendar.json").write_text(json.dumps(calendar))
    return str(grp)


def _make(source="contract", backtest=True):
    ctx = TrigCtx()
    ctx.is_backtest = backtest
    _fill_daily(ctx.add_store("d1"), n=90)
    inst = InstrumentConfig(code="SHFE.rb.HOT", units=1,
                            signal_source="")
    base = TurtleBaseConfig(entry_mode="trigger",
                            signal_source=source)
    return StraTurtle("tf", inst, base), ctx


class _MP:
    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, val):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, val)

    def undo(self):
        for obj, name, val in reversed(self._undo):
            setattr(obj, name, val)
        self._undo.clear()


_CAL = {"codes": {"SHFE.rb.HOT": {"segments": [
    ["20200101", "20200514", "2005"],
    ["20200515", "20201014", "2010"],
    ["20201015", "20261109", "2701"],
]}}}


# ------------------------------------------------------------------
# align_window 纯函数
# ------------------------------------------------------------------
def test_align_window_tail_truncation():
    hot_dates = list(range(100, 110))
    highs = [float(i) for i in range(10)]
    lows = [h - 1 for h in highs]
    closes = highs
    sig_dates = list(range(104, 110))
    sig_c = [1000.0 + i for i in range(6)]
    out = align_window(hot_dates, highs, lows, closes,
                       sig_dates, sig_c, sig_c, sig_c, min_bars=5)
    assert out is not None
    oh, ol, oc = out
    assert len(oc) == 6 and oc[-1] == sig_c[-1] and oh[0] == 1000.0


def test_align_window_interior_carry():
    hot_dates = list(range(200, 210))
    zeros = [0.0] * 10
    sig_dates = [d for d in hot_dates if d != 205]
    sig_c = [3000.0 + (i if d < 205 else i - 1)
             for i, d in enumerate(sig_dates)]
    out = align_window(hot_dates, zeros, zeros, zeros,
                       sig_dates, sig_c, sig_c, sig_c, min_bars=3)
    assert out is not None and len(out[2]) == 10
    assert out[2][5] == out[2][4]


def test_align_window_insufficient():
    hot_dates = list(range(300, 310))
    sig_dates = list(range(308, 310))
    z = [1.0, 2.0]
    assert align_window(hot_dates, z + [0] * 8, [0] * 10, [0] * 10,
                        sig_dates, z, z, z, min_bars=5) is None


# ------------------------------------------------------------------
# live 路径: hots.json 当前主力
# ------------------------------------------------------------------
def test_live_mode_resolves_hots(tmp_path):
    mp = _MP()
    try:
        mp.setattr(st_mod, "_GROUP_DIR",
                   _tmp_tree(tmp_path, hots_rules=[
                       {"date": 20260828, "to": "rb2701"}]))
        stra, ctx = _make("contract", backtest=False)
        stra.on_init(ctx)
        assert stra._sig_contract == "SHFE.rb.2701"
        assert stra._sig_calendar is None
        assert ctx.prepared[0] == "SHFE.rb.2701"
        assert any("signal source" in t for _, t in ctx.logs)
    finally:
        mp.undo()


def test_live_mode_fallback_on_empty_rules(tmp_path):
    mp = _MP()
    try:
        mp.setattr(st_mod, "_GROUP_DIR",
                   _tmp_tree(tmp_path, hots_rules=[]))
        stra, ctx = _make("contract", backtest=False)
        stra.on_init(ctx)
        assert stra._sig_contract is None
        assert any("signal_source_fallback" in t or "退化" in t
                   for _, t in ctx.logs)
    finally:
        mp.undo()


# ------------------------------------------------------------------
# bt 路径: roll_calendar 逐日主力
# ------------------------------------------------------------------
def test_bt_mode_loads_calendar_and_prepares_all(tmp_path):
    mp = _MP()
    try:
        mp.setattr(st_mod, "_GROUP_DIR",
                   _tmp_tree(tmp_path, calendar=_CAL))
        stra, ctx = _make("contract", backtest=True)
        ctx.logs.clear()
        stra.on_init(ctx)
        assert stra._sig_calendar is not None and \
            len(stra._sig_calendar) == 3
        assert stra._sig_contract == "SHFE.rb.2701"
        # bt 不走 C++ 副K: 无合约订阅, 数据 contracts 缓存直读
        assert not any(c[0].startswith("SHFE.rb.2")
                       for c in ([ctx.prepared] if ctx.prepared else []))
        assert any("roll_calendar" in t for _, t in ctx.logs)
    finally:
        mp.undo()


def test_dominant_for_date_lookup(tmp_path):
    mp = _MP()
    try:
        mp.setattr(st_mod, "_GROUP_DIR",
                   _tmp_tree(tmp_path, calendar=_CAL))
        stra, ctx = _make("contract", backtest=True)
        stra.on_init(ctx)
        assert stra._dominant_for(20200301) == "SHFE.rb.2005"
        assert stra._dominant_for(20200601) == "SHFE.rb.2010"
        assert stra._dominant_for(20261109) == "SHFE.rb.2701"
        assert stra._dominant_for(19990101) == "SHFE.rb.2005"  # 窗前
        assert stra._dominant_for(20990101) == "SHFE.rb.2701"  # 窗后
    finally:
        mp.undo()


# ------------------------------------------------------------------
# 零漂移 / 覆盖
# ------------------------------------------------------------------
def test_hot_mode_zero_drift_passthrough(tmp_path):
    mp = _MP()
    try:
        mp.setattr(st_mod, "_GROUP_DIR",
                   _tmp_tree(tmp_path, hots_rules=[
                       {"date": 20260828, "to": "rb2701"}]))
        stra, ctx = _make("hot", backtest=True)
        stra.on_init(ctx)
        assert stra._sig_contract is None
        h = np.asarray([1.0, 2.0, 3.0])
        out = stra._signal_arrays(ctx, ctx.klines["d1"], h, h - 0.5, h)
        assert out[0] is h
    finally:
        mp.undo()


def test_instrument_override_beats_base(tmp_path):
    mp = _MP()
    try:
        mp.setattr(st_mod, "_GROUP_DIR",
                   _tmp_tree(tmp_path, calendar=_CAL))
        ctx = TrigCtx()
        ctx.is_backtest = True
        _fill_daily(ctx.add_store("d1"), n=90)
        inst = InstrumentConfig(code="SHFE.rb.HOT", units=1,
                                signal_source="hot")
        stra = StraTurtle("tf", inst, TurtleBaseConfig(
            entry_mode="trigger", signal_source="contract"))
        stra.on_init(ctx)
        assert stra._sig_contract is None
        assert stra._sig_calendar is None
    finally:
        mp.undo()


def test_signal_source_config_validation():
    from core.ConfigLoader import ConfigLoader
    import tempfile
    raw = ("base:\n  signal_source: wrong\n"
           "instruments:\n  - code: SHFE.rb.HOT\n")
    with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                     delete=False) as f:
        f.write(raw)
        path = f.name
    try:
        try:
            ConfigLoader.load(path, warn=False)
        except ValueError as e:
            assert "signal_source" in str(e)
        else:
            raise AssertionError("非法 signal_source 应被拒绝")
    finally:
        os.unlink(path)
