# -*- coding: utf-8 -*-
"""S4 信号口径开关 + A1 尾项 StrategyHost + S3 h1 聚合的口径钉死 (2026-10-24)

三个引擎开关 (docs/OPTIMIZATION_PLAN §S4):
- trigger_envelope: trigger 武装位 包络(含当根) vs 纯通道(经典)
- allow_same_bar_reversal: close 同 bar 出场后再入场
- gap_stop: close 引擎层跳空止损按开盘价
默认值必须保持旧行为 (golden_bt 端到端哈希负责最终把关)。
"""
import numpy as np
import pytest

from core.TurtleEngine import TurtleEngine
from core.aggregate import aggregate_h1
from core.ConfigLoader import ConfigLoader
from execution.host import StrategyHost
from StraTurtle import StraTurtle
from schemas import InstrumentConfig, TurtleBaseConfig


def _chans(eu=100.0, el=90.0, xu=105.0, xl=85.0, fu=110.0, fl=80.0):
    return {
        "entry_upper": eu, "entry_lower": el,
        "exit_upper": xu, "exit_lower": xl,
        "fs_upper": fu, "fs_lower": fl,
        "prev_entry_upper": eu, "prev_entry_lower": el,
        "prev_exit_upper": xu, "prev_exit_lower": xl,
        "prev_fs_upper": fu, "prev_fs_lower": fl,
    }


# ----------------------------------------------------------------------
# S4-1 trigger_envelope
# ----------------------------------------------------------------------
def test_trigger_envelope_default_arms_envelope():
    """默认 (True): 武装位 = max(通道, 当根 high) 包络 (旧行为)。

    隔离包络机制需显式关 arm_exclude_signal (其默认已翻 True)。"""
    e = TurtleEngine(entry_mode="trigger", arm_exclude_signal=False)
    e.on_bar(120.0, 95.0, 118.0, 100.0, 10.0, _chans(eu=100.0))
    a = e.armed
    assert a["long"] == 120.0     # 当根 high 120 > 通道 100 → 包络取 120


def test_trigger_envelope_off_arms_channel_only():
    """False: 纯突破前 N 日通道, 不含当根极值。"""
    e = TurtleEngine(entry_mode="trigger", trigger_envelope=False,
                     arm_exclude_signal=False)
    e.on_bar(120.0, 95.0, 118.0, 100.0, 10.0, _chans(eu=100.0))
    a = e.armed
    assert a["long"] == 100.0     # 纯通道, 当根 high 不参与


def test_arm_exclude_signal_excludes_current_and_ignores_envelope():
    """arm_exclude 使用 current（已排除信号根），envelope 不得重新加入。"""
    ch = _chans(eu=100.0)
    ch["prev_entry_upper"] = 95.0
    ch["prev_exit_lower"] = 84.0
    ch.update({"next_entry_upper": 130.0, "next_entry_lower": 70.0,
               "next_exit_upper": 140.0, "next_exit_lower": 60.0,
               "next_fs_upper": 150.0, "next_fs_lower": 50.0})
    e = TurtleEngine(entry_mode="trigger", arm_exclude_signal=True)
    e.on_bar(120.0, 97.0, 118.0, 100.0, 10.0, ch)
    assert e.armed["long"] == 100.0       # 不是 max(prev/next, high)=130/120

    e2 = TurtleEngine(entry_mode="trigger", arm_exclude_signal=True,
                      trigger_envelope=False)
    e2.on_bar(120.0, 97.0, 118.0, 100.0, 10.0, ch)
    assert e2.armed["long"] == 100.0
    e2.confirm_enter(1, 10, 101.0, 10.0)
    e2.on_bar(120.0, 97.0, 118.0, 100.0, 10.0, ch)
    assert e2.armed["exit"] == 85.0      # current exit_lower，非 prev=84


def test_regular_trigger_arm_uses_next_channels():
    """非 exclude 模式使用 next_*，生产与研究不再各自错一根。"""
    ch = _chans(eu=100.0)
    ch.update({"next_entry_upper": 130.0, "next_entry_lower": 70.0,
               "next_exit_upper": 140.0, "next_exit_lower": 60.0,
               "next_fs_upper": 150.0, "next_fs_lower": 50.0})
    e = TurtleEngine(entry_mode="trigger", arm_exclude_signal=False)
    e.on_bar(120.0, 97.0, 118.0, 100.0, 10.0, ch)
    assert e.armed["long"] == 130.0
    assert e.armed["short"] == 70.0


def test_strategy_next_channels_are_last_n_including_closed_bar():
    stra = StraTurtle("next", InstrumentConfig(code="SHFE.rb.HOT"),
                      TurtleBaseConfig())
    highs = np.arange(1.0, 100.0)
    lows = highs - 2.0
    closes = highs - 1.0
    _atr, _n, chan = stra._calc_indicators(highs, lows, closes)
    ch = chan["S1"]
    assert ch["entry_upper"] == np.max(highs[-21:-1])  # current 排除当根
    assert ch["next_entry_upper"] == np.max(highs[-20:])
    assert ch["next_entry_lower"] == np.min(lows[-20:])
    assert ch["next_exit_upper"] == np.max(highs[-10:])
    assert ch["next_fs_upper"] == np.max(highs[-55:])


# ----------------------------------------------------------------------
# S4-2 allow_same_bar_reversal (close 模式)
# ----------------------------------------------------------------------
def _exit_then_breakout_engine(**kw):
    """持仓中: 本 bar 触发通道出场, 且同 bar 收盘又突破反向入场通道。"""
    e = TurtleEngine(entry_mode="close", **kw)
    # 先有 1 层多仓 @100, N=10 → 通道出场位 = 数据字典给的 exit_lower
    e.confirm_enter(1, 10, 100.0, 10.0)
    return e


def test_same_bar_reversal_allowed_by_default():
    e = _exit_then_breakout_engine()
    # low 不触止损(99 > 80), close 85 < exit_lower 90 → 出场;
    # 同 bar close 85 < entry_lower 90 → 反向 (空) 边沿: prev_close 100
    # >= prev_entry_lower 90 → 触发
    decs = e.on_bar(95.0, 84.0, 85.0, 100.0, 10.0,
                    _chans(el=90.0, xl=90.0))
    kinds = [d["decision"] for d in decs]
    assert "exit" in kinds and "enter" in kinds


def test_same_bar_reversal_disabled():
    e = _exit_then_breakout_engine(allow_same_bar_reversal=False)
    decs = e.on_bar(95.0, 84.0, 85.0, 100.0, 10.0,
                    _chans(el=90.0, xl=90.0))
    kinds = [d["decision"] for d in decs]
    assert "exit" in kinds and "enter" not in kinds


# ----------------------------------------------------------------------
# S4-3 gap_stop (close 模式引擎层跳空止损)
# ----------------------------------------------------------------------
def _gap_engine(**kw):
    e = TurtleEngine(entry_mode="close", **kw)
    e.confirm_enter(1, 10, 100.0, 10.0)   # 止损 = 100 - 2×10 = 80
    return e


def test_gap_stop_default_fills_at_open():
    """默认 (True) 且传 open: 开盘 75 越过止损 80 → 按 75 成交 (诚实口径)。"""
    e = _gap_engine()
    decs = e.on_bar(76.0, 74.0, 78.0, 100.0, 10.0, _chans(), open=75.0)
    stop = [d for d in decs if d["decision"] == "exit"
            and d.get("reason") == "stop"]
    assert stop and stop[0]["price"] == 75.0


def test_gap_stop_old_caliber_without_open():
    """不传 open (旧调用方): 即使 low 深穿止损也按止损价 80 (旧行为)。"""
    e = _gap_engine()
    decs = e.on_bar(79.0, 74.0, 78.0, 100.0, 10.0, _chans())
    stop = [d for d in decs if d["decision"] == "exit"
            and d.get("reason") == "stop"]
    assert stop and stop[0]["price"] == 80.0


def test_gap_stop_disabled_fills_at_stop_price():
    """False: 传 open 也按止损价 (回退旧口径, A/B 用)。"""
    e = _gap_engine(gap_stop=False)
    decs = e.on_bar(76.0, 74.0, 78.0, 100.0, 10.0, _chans(), open=75.0)
    stop = [d for d in decs if d["decision"] == "exit"
            and d.get("reason") == "stop"]
    assert stop and stop[0]["price"] == 80.0


def test_gap_stop_intrabar_touch_unchanged():
    """无跳空 (open 在止损上方) 的盘中触碰: 仍按止损价成交。"""
    e = _gap_engine()
    decs = e.on_bar(95.0, 79.0, 90.0, 100.0, 10.0, _chans(), open=95.0)
    stop = [d for d in decs if d["decision"] == "exit"
            and d.get("reason") == "stop"]
    assert stop and stop[0]["price"] == 80.0


# ----------------------------------------------------------------------
# ConfigLoader 校验 (AGENTS 约定 #4)
# ----------------------------------------------------------------------
def test_configloader_rejects_nonbool_caliber(tmp_path):
    import yaml
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.dump({
        "base": {"trigger_envelope": "yes"},
        "instruments": [{"code": "SHFE.rb.HOT"}],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="trigger_envelope"):
        ConfigLoader.load(str(bad), warn=False)


def test_configloader_accepts_caliber_defaults(tmp_path):
    import yaml
    ok = tmp_path / "ok.yaml"
    ok.write_text(yaml.dump({
        "base": {"trigger_envelope": False, "gap_stop": False,
                 "allow_same_bar_reversal": False},
        "instruments": [{"code": "SHFE.rb.HOT"}],
    }), encoding="utf-8")
    cfg = ConfigLoader.load(str(ok), warn=False)
    assert cfg.base.trigger_envelope is False
    assert cfg.base.gap_stop is False
    assert cfg.base.allow_same_bar_reversal is False


# ----------------------------------------------------------------------
# A1 尾项 StrategyHost
# ----------------------------------------------------------------------
def test_host_passes_declared_and_blocks_undeclared():
    class Stra:
        name = "demo"
        _engine = object()

    h = StrategyHost(Stra(), "name", "_engine")
    assert h.name == "demo"
    assert h._engine is not None
    with pytest.raises(AttributeError, match="未声明的依赖"):
        _ = h._risk
    with pytest.raises(AttributeError, match="未声明的依赖"):
        h._risk = 1


def test_host_write_through_to_strategy():
    class Stra:
        _truth_probed = False

    s = Stra()
    h = StrategyHost(s, "_truth_probed")
    h._truth_probed = True
    assert s._truth_probed is True


def test_module_deps_cover_all_usage():
    """DEPS 审计: 模块源码里出现的 self.h.X 必须都在 DEPS 内
    (本测试就是 host 白名单的回归网 — 新增依赖未声明即红)。"""
    import re
    from execution.router import ExecutionRouter
    from execution.fill_tracker import FillTracker
    from execution.truth_monitor import TruthMonitor
    from execution.telemetry import Telemetry
    for cls in (ExecutionRouter, FillTracker, TruthMonitor, Telemetry):
        src_file = sys_modules_file(cls)
        src = open(src_file, encoding="utf-8").read()
        used = set(re.findall(r"self\.h\.([A-Za-z_][A-Za-z_0-9]*)", src))
        used.discard("X")   # 迁移注释占位符 (self.X → self.h.X), 非真实依赖
        missing = used - set(cls.DEPS)
        assert not missing, f"{cls.__name__} 使用但未声明: {missing}"


def sys_modules_file(cls):
    import inspect
    return inspect.getsourcefile(cls)


# ----------------------------------------------------------------------
# S3 h1 聚合 (build_h1_bars 的关键口径)
# ----------------------------------------------------------------------
def test_h1_bars_night_session_mapping():
    """夜盘 bar (time>=200000) 归属下一交易日; day_idx 按日分组。"""
    from research.run_portfolio_bt import build_h1_bars
    # 交易日历: 20200102(四), 20200103(五), 20200106(一)
    cal = [20200102, 20200103, 20200106]
    m5 = {
        (20200102, 210500): (10, 11, 9, 10.5, 100, 1),   # 周四夜 → 周五
        (20200103, 90500):  (10.5, 12, 10, 11, 200, 2),  # 周五日盘
        (20200103, 210500): (11, 11.5, 10.8, 11.2, 150, 3),  # 周五夜 → 周一
        (20200106, 90500):  (11.2, 12, 11, 11.8, 180, 4),    # 周一日盘
    }
    bars = build_h1_bars(m5, cal)
    # 桶: (20200103, 2100) 来自周四夜; (20200103, 900); (20200106, 2100);
    #     (20200106, 900)
    assert list(bars["tdate"]) == [20200103, 20200103, 20200106, 20200106]
    assert bars["day_idx"][20200103] == [0, 1]
    assert bars["day_idx"][20200106] == [2, 3]
    # OHLCV: 周五日盘桶由单根 m5 组成
    i = bars["index"][20200103 * 10000 + 900]
    assert bars["opens"][i] == 10.5 and bars["highs"][i] == 12.0
    assert bars["closes"][i] == 11.0 and bars["volumes"][i] == 200


def test_h1_bars_bucket_ohlcv_merge():
    """同一桶多根 m5: high/low 取极值, close 取末根, volume 累加。"""
    from research.run_portfolio_bt import build_h1_bars
    cal = [20200102]
    m5 = {
        (20200102, 90500):  (10, 12, 9, 11, 100, 1),
        (20200102, 91000):  (11, 13, 10.5, 12.5, 50, 2),
        (20200102, 91500):  (12.5, 12.8, 12, 12.2, 70, 3),
    }
    bars = build_h1_bars(m5, cal)
    i = bars["index"][20200102 * 10000 + 900]
    assert bars["opens"][i] == 10.0
    assert bars["highs"][i] == 13.0
    assert bars["lows"][i] == 9.0
    assert bars["closes"][i] == 12.2
    assert bars["volumes"][i] == 220
    assert bars["ois"][i] == 3
