"""StrategyMode 模式决策面测试 (A2, 2026-10-22)

_mode 在 on_init 单点算定; 全部运行期分支读 mode.xxx。
本组测试钉死派生规则真值表 — 改动派生规则必须改这里的期望。
"""
from conftest import *  # noqa: F401,F403
from execution.models import StrategyMode
from schemas import InstrumentConfig, TurtleBaseConfig


def _mode(period="d1", entry_mode="trigger", intrabar=False,
          bt=False, no_simtick=False):
    base = TurtleBaseConfig(entry_mode=entry_mode)
    inst = InstrumentConfig(code="SHFE.rb.HOT", period=period,
                            intrabar_stop=intrabar)
    return StrategyMode.resolve(base, inst, bt, no_simtick)


class TestModeTruthTable:
    def test_default_live_trigger_d1(self):
        m = _mode()
        assert m.main_period == "d1" and m.calc_path == "daily"
        assert m.session_flush == "daily_synth"
        assert m.wants_ticks and m.wants_catchup

    def test_close_mode_bt(self):
        # bt close: 日K 闭合, on_calculate 驱动, 无合成/catchup
        m = _mode(entry_mode="close", bt=True)
        assert m.session_flush == "none"
        assert not m.wants_ticks and not m.wants_catchup

    def test_close_mode_live_d1(self):
        # 2026-11-11 Phase1 激活包: 实盘 d1 永不闭合 (F3), close 决策
        # 改由 session_end 合成日K 驱动; tick 订阅只为心跳断流基准
        # (触发/意图仍不处理), 盘中止损靠 intrabar_stop 的 m5 主K扫描
        m = _mode(entry_mode="close", bt=False)
        assert m.session_flush == "daily_synth"
        assert m.wants_catchup
        assert not m.wants_ticks
        assert m.wants_tick_feed          # close 实盘: 心跳基准需 tick

    def test_close_mode_live_intrabar(self):
        # close+盘中止损: session_end 驱动收盘决策 + m5 主K扫描止损,
        # 两驱动互补
        m = _mode(entry_mode="close", intrabar=True, bt=False)
        assert m.main_period == "m5" and m.calc_path == "intrabar"
        assert m.session_flush == "daily_synth"
        assert not m.wants_ticks

    def test_h1(self):
        m = _mode(period="h1")
        assert m.main_period == "m5" and m.calc_path == "h1"
        assert m.session_flush == "h1_flush"
        assert m.wants_ticks               # trigger tick 触发仍然需要
        assert not m.wants_catchup         # h1 由 m5 闭合驱动, 无日级 catchup

    def test_intrabar(self):
        m = _mode(intrabar=True)
        assert m.main_period == "m5" and m.calc_path == "intrabar"
        assert not m.wants_catchup

    def test_bt_no_simtick(self):
        m = _mode(bt=True, no_simtick=True)
        assert not m.wants_ticks           # 退化 check_bar 日线触碰
        assert not m.wants_catchup         # bt 无 catchup

    def test_mode_frozen(self):
        m = _mode()
        try:
            m.period = "h1"
            raise AssertionError("frozen dataclass 不应可写")
        except AttributeError:
            pass
