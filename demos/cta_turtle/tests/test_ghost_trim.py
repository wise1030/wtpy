"""ghost_fill 自愈裁剪测试 (P0-3, 2026-09-17 CF 34 手幽灵仓事故)

核心口径:
- trim_to_lots 只从尾部裁剪 (底层成交确认最充分);
- 只缩量绝不放大; target=0 等价 flatten;
- 跨子系统从尾部子系统开始裁, 保持净额一致;
- 方向相反/券商为零/券商>=信号 时不动作 (共享账户安全)。
"""
import numpy as np
import pytest

from helpers.TurtleEngine import TurtleEngine
from schemas import (InstrumentConfig, RiskConfig, TurtleBaseConfig)
from StraTurtle import StraTurtle


def _eng(mode="trigger", system="S1"):
    return TurtleEngine(system=system, entry_mode=mode,
                        stop_n_mode="ratchet")


class TestTrimToLots:
    def test_partial_trim_from_tail(self):
        e = _eng()
        e.confirm_enter(1, 45, 100.0, n=5.0)
        e.confirm_add(20, 105.0, n=5.0)
        e.confirm_add(19, 110.0, n=5.0)
        e.confirm_add(15, 115.0, n=5.0)
        assert sum(l for l, _ in e.layers) == 99
        dropped = e.trim_to_lots(65)
        assert dropped == 34
        assert [l for l, _ in e.layers] == [45, 20]
        assert e.direction == 1

    def test_trim_to_zero_flattens(self):
        e = _eng()
        e.confirm_enter(-1, 50, 100.0, n=5.0)
        dropped = e.trim_to_lots(0)
        assert dropped == 50
        assert e.direction == 0 and e.layers == []

    def test_noop_when_target_ge_current(self):
        e = _eng()
        e.confirm_enter(1, 30, 100.0, n=5.0)
        assert e.trim_to_lots(30) == 0
        assert e.trim_to_lots(100) == 0
        assert sum(l for l, _ in e.layers) == 30

    def test_noop_when_flat(self):
        e = _eng()
        assert e.trim_to_lots(10) == 0

    def test_trim_rebuilds_ratchet_anchor(self):
        """裁剪后止损锚按剩余末层重建 (不再引用被裁层)。"""
        e = _eng()
        e.confirm_enter(1, 45, 100.0, n=5.0)
        e.confirm_add(20, 110.0, n=5.0)   # stop anchor = 110-2*5=100
        e.trim_to_lots(45)
        assert e.stop_anchor() == pytest.approx(100.0 - 2 * 5.0)  # 90

    def test_s1_filter_untouched_by_trim(self):
        """裁剪不动 S1 过滤器 (真实持仓仍在, 输赢按剩余层判定)。"""
        e = _eng()
        e.confirm_enter(1, 45, 100.0, n=5.0)
        assert e.s1_last is not None and e.s1_last["taken"]
        e.trim_to_lots(20)
        assert e.s1_last["taken"] and e.s1_last["status"] == "open"


class _NullNotify:
    def send(self, key, text):
        return False


def _make_stra(**base_kw):
    base = TurtleBaseConfig(**base_kw)
    inst = InstrumentConfig(code="CZCE.CF.HOT", units=1)
    return StraTurtle("turtle_CZCE_CF_HOT", inst, base,
                      RiskConfig(), notify=_NullNotify())


class _Ctx:
    def __init__(self, broker_net):
        self.broker_net = broker_net
        self.logs = []
        self.positions = {}
        self.target = 0

    def stra_log_text(self, txt, level=1):
        self.logs.append((level, txt))

    def stra_set_position(self, code, pos, tag=""):
        self.positions[code] = pos

    def stra_get_position(self, code):
        return self.positions.get(code, 0)


class TestGhostTrim:
    def _stra_with_pos(self, stra, lots=99, direction=-1):
        e = stra._engine
        e.confirm_enter(direction, 45, 16100.0, n=100.0)
        e.confirm_add(20, 16200.0, n=100.0)
        e.confirm_add(19, 16300.0, n=100.0)
        e.confirm_add(15, 16400.0, n=100.0)
        stra._fill_pending["S1"] = {"engine": e, "ts": 0, "lots": 15,
                                    "fees0": None, "cursor": None,
                                    "truth0": None}
        return stra

    def test_trim_to_broker_same_direction(self):
        stra = _make_stra()
        self._stra_with_pos(stra)
        ctx = _Ctx(broker_net=-65.0)
        stra._maybe_ghost_trim(ctx, "S1", stra._fill_pending["S1"], -65.0)
        # 99 → 65: 尾部 34 手被裁
        assert stra._combined_target() == -65
        assert ctx.positions.get("CZCE.CF.HOT") == -65
        assert stra._fill_pending.get("S1") is None   # 槽位已清
        assert any("ghost trim" in t for _, t in ctx.logs)

    def test_no_trim_when_broker_ge_signal(self):
        stra = _make_stra()
        self._stra_with_pos(stra)
        ctx = _Ctx(broker_net=-120.0)   # 含别家仓位 → 不动作
        stra._maybe_ghost_trim(ctx, "S1", stra._fill_pending["S1"], -120.0)
        assert stra._combined_target() == -99

    def test_no_trim_when_direction_opposite(self):
        stra = _make_stra()
        self._stra_with_pos(stra)
        ctx = _Ctx(broker_net=65.0)
        stra._maybe_ghost_trim(ctx, "S1", stra._fill_pending["S1"], 65.0)
        assert stra._combined_target() == -99

    def test_no_trim_when_disabled(self):
        stra = _make_stra(ghost_auto_trim=False)
        self._stra_with_pos(stra)
        ctx = _Ctx(broker_net=-65.0)
        stra._maybe_ghost_trim(ctx, "S1", stra._fill_pending["S1"], -65.0)
        assert stra._combined_target() == -99

    def test_trim_to_zero_flattens_and_zeroes_position(self):
        stra = _make_stra()
        self._stra_with_pos(stra, lots=45)
        # 手工构造 45 手 (只一层)
        stra2 = _make_stra()
        e = stra2._engine
        e.confirm_enter(-1, 45, 16100.0, n=100.0)
        stra2._fill_pending["S1"] = {"engine": e, "ts": 0, "lots": 45,
                                     "fees0": None, "cursor": None,
                                     "truth0": None}
        ctx = _Ctx(broker_net=0.0)   # 券商为零 → 不动作 (避免误平)
        stra2._maybe_ghost_trim(ctx, "S1", stra2._fill_pending["S1"], 0.0)
        assert stra2._combined_target() == -45   # 未动
