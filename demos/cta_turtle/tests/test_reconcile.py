"""运行时对账循环测试 (P0-3)

覆盖: 一致时计数复位、单根失配(在途订单)不动作、
持续失配按实际持仓重建/外部全平、反向持仓重建。
"""
from conftest import *  # noqa: F401,F403
import numpy as np

from StraTurtle import StraTurtle
from schemas import InstrumentConfig, TurtleBaseConfig


class ReconCtx:
    """最小对账上下文: 可外部改写 pos/avgpx 模拟脱管"""

    def __init__(self, pos=0, avgpx=0.0):
        self.pos = pos
        self.avgpx = avgpx
        self.logs = []

    def stra_get_position(self, code, *a, **k):
        return self.pos

    def stra_get_position_avgpx(self, code=""):
        return self.avgpx

    def stra_log_text(self, text, level=1):
        self.logs.append((level, text))


def _mk_stra(patience=3):
    inst = InstrumentConfig(code="SHFE.rb.HOT", period="d1", system="S1",
                            units=1)
    base = TurtleBaseConfig(recon_patience=patience)
    return StraTurtle("t", inst, base)


def _closes(last=3500.0):
    return np.array([3400.0, last])


def test_match_resets_counter():
    stra = _mk_stra()
    stra._engine.confirm_enter(1, 100, 3450.0)
    ctx = ReconCtx(pos=100, avgpx=3450.0)
    stra._reconcile(ctx, _closes())
    assert stra._recon_miss == 0


def test_single_miss_is_tolerated_in_flight_order():
    """实盘在途订单: 单根失配不动作"""
    stra = _mk_stra(patience=3)
    stra._engine.confirm_enter(1, 100, 3450.0)
    ctx = ReconCtx(pos=60, avgpx=3448.0)     # 部分成交在途
    for _ in range(2):
        stra._reconcile(ctx, _closes())
    assert stra._recon_miss == 2
    assert len(stra._engine.layers) == 1
    assert stra._engine.layers[0][0] == 100   # 未重建
    assert not any(lv >= 3 for lv, _ in ctx.logs)


def test_persistent_mismatch_rebuilds_from_broker():
    stra = _mk_stra(patience=3)
    stra._engine.confirm_enter(1, 100, 3450.0)
    stra._risk.on_filled("SHFE.rb.HOT", 1, 100, 3450.0, n=30,
                         volscale=10, stop_mult=2.0)
    ctx = ReconCtx(pos=64, avgpx=3460.0)      # 部分成交且已稳定
    for _ in range(3):
        stra._reconcile(ctx, _closes())
    assert stra._engine.direction == 1
    assert stra._engine.layers == [[64, 3460.0]]
    assert stra._risk.has_position("SHFE.rb.HOT")
    assert stra._risk._book["SHFE.rb.HOT"]["lots"] == 64
    warn = [t for lv, t in ctx.logs if lv >= 3 and "RECONCILE" in t]
    assert len(warn) == 1


def test_external_flatten_closes_engine():
    """手工平仓后: 引擎确认出场并回写 S1 过滤器结果"""
    stra = _mk_stra(patience=2)
    stra._engine.confirm_enter(1, 50, 3450.0)
    stra._risk.on_filled("SHFE.rb.HOT", 1, 50, 3450.0, n=30,
                         volscale=10, stop_mult=2.0)
    ctx = ReconCtx(pos=0)                      # 外部全平
    for _ in range(2):
        stra._reconcile(ctx, _closes())
    assert stra._engine.direction == 0
    assert stra._engine.layers == []
    assert stra._engine.s1_last["status"] == "closed"
    assert stra._engine.s1_last["result"] == "win"   # 3450→3500 浮盈
    assert not stra._risk.has_position("SHFE.rb.HOT")


def test_opposite_position_rebuild_flips_direction():
    stra = _mk_stra(patience=2)
    stra._engine.confirm_enter(1, 100, 3450.0)
    ctx = ReconCtx(pos=-30, avgpx=-3470.0)     # 反向持仓 (符号约定为负)
    for _ in range(2):
        stra._reconcile(ctx, _closes())
    assert stra._engine.direction == -1
    assert stra._engine.layers == [[30, 3470.0]]


def test_counter_reset_after_rebuild_when_matched():
    stra = _mk_stra(patience=2)
    stra._engine.confirm_enter(1, 100, 3450.0)
    ctx = ReconCtx(pos=40, avgpx=3445.0)
    stra._reconcile(ctx, _closes())
    stra._reconcile(ctx, _closes())            # 达到 patience → 重建
    assert stra._engine.layers[0][0] == 40
    ctx.pos = 40                                # 已一致
    stra._reconcile(ctx, _closes())
    assert stra._recon_miss == 0
