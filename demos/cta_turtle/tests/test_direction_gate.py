"""方向闸 + 执行层集成测试 (StraTurtle._execute 路径)

验证:
- 方向闸否决入场且不触碰引擎/S1过滤器状态
- 手数不足否决
- 否决日志去重
- implied_fill 从 avgpx 反推
- HOT 换月 re-anchor
"""
from unittest.mock import MagicMock

from conftest import *  # noqa: F401,F403
from schemas import InstrumentConfig, TurtleBaseConfig
from helpers.TurtleEngine import DEC_ENTER, DEC_EXIT, DEC_ADD


class FakeCtx:
    def __init__(self, pos=0, avgpx=0.0):
        self.pos = pos
        self.avgpx = avgpx
        self.orders = []
        self.logs = []
        self.kv = {}
        self.marks = []

    def stra_get_position(self, code, *a, **k):
        return self.pos

    def stra_set_position(self, code, target, tag=""):
        self.orders.append((code, target, tag))
        # 模拟按 3500 成交
        old_pos, old_avg = self.pos, self.avgpx
        self.pos = target
        if target == 0:
            self.avgpx = 0.0
        elif old_pos == 0:
            self.avgpx = 3500.0
        else:
            added = abs(target) - abs(old_pos)
            if added > 0:
                self.avgpx = (old_avg * abs(old_pos) + 3500.0 * added) \
                    / abs(target)

    def stra_get_position_avgpx(self, code=""):
        return self.avgpx

    def stra_log_text(self, text, level=1):
        self.logs.append((level, text))

    def user_save_data(self, key, val):
        self.kv[key] = val

    def add_chart_mark(self, price, icon, tag):
        self.marks.append((price, icon))


def _make_stra(direction="both", units=2):
    from StraTurtle import StraTurtle
    inst = InstrumentConfig(code="SHFE.rb.HOT", period="d1", system="S1",
                            units=units, direction=direction)
    base = TurtleBaseConfig()
    stra = StraTurtle("test_turtle", inst, base)
    stra._volscale = 10.0     # on_init 才从框架取, 直测路径手工注入
    stra._pricetick = 1.0
    return stra


def test_direction_gate_blocks_short():
    """long_only 配置下空头信号被否决, 引擎状态不变(不过滤)"""
    stra = _make_stra(direction="long")
    ctx = FakeCtx()
    dec = {"decision": DEC_ENTER, "direction": -1, "price": 3400.0,
           "n": 30.0, "failsafe": False}
    stra._execute(ctx, dec, n=30.0)
    assert ctx.orders == []                      # 未下单
    assert stra._engine.direction == 0           # 引擎未提交
    assert stra._engine.s1_last is None          # 过滤器未受影响
    assert any("veto" in t for _, t in ctx.logs)


def test_direction_gate_allows_long():
    stra = _make_stra(direction="long")
    ctx = FakeCtx()
    dec = {"decision": DEC_ENTER, "direction": 1, "price": 3500.0,
           "n": 30.0, "failsafe": False}
    stra._execute(ctx, dec, n=30.0)
    # sizer: floor(400000/(30*10))=1333; 风控单品种预算:
    # floor(5e6×0.02/(30×10×2))=166 (FakeCtx 无资金接口 → 回退 500 万)
    assert ctx.orders == [("SHFE.rb.HOT", 166, "Enter")]
    assert stra._engine.direction == 1
    assert stra._engine.layers == [[166, 3500.0]]
    assert stra._risk.has_position("SHFE.rb.HOT")   # 风控账本已记
    assert stra._risk.open_risk() == 166 * 30 * 10 * 2.0


def test_insufficient_lots_veto():
    """N 过大预算不足 1 手 → 否决 (units=2, 预算40万; N=5万×10=50万 > 40万)"""
    stra = _make_stra(direction="both")
    ctx = FakeCtx()
    dec = {"decision": DEC_ENTER, "direction": 1, "price": 3500.0,
           "n": 50000.0, "failsafe": False}
    stra._execute(ctx, dec, n=50000.0)
    assert ctx.orders == []
    assert stra._engine.direction == 0


def test_veto_log_dedup():
    """同类否决连续触发只记一次"""
    stra = _make_stra(direction="long")
    ctx = FakeCtx()
    dec = {"decision": DEC_ENTER, "direction": -1, "price": 3400.0,
           "n": 30.0, "failsafe": False}
    stra._execute(ctx, dec, n=30.0)
    stra._execute(ctx, dec, n=30.0)
    veto_logs = [t for _, t in ctx.logs if "veto" in t]
    assert len(veto_logs) == 1


def test_veto_log_per_key_cooldown_and_count():
    """P2: 逐 key 冷却 (原全局单槽在交替原因下会刷屏); 复发带累计次数,
    且成功入场不再重置冷却"""
    stra = _make_stra(direction="long")
    ctx = FakeCtx()
    stra._veto_log(ctx, "risk1", "veto A")
    stra._veto_log(ctx, "rscale1", "veto B")      # 不同 key 独立记录
    stra._veto_log(ctx, "risk1", "veto A")        # 同 key 冷却抑制
    stra._veto_log(ctx, "rscale1", "veto B")
    logs = [t for _, t in ctx.logs]
    assert sum("veto A" in t for t in logs) == 1
    assert sum("veto B" in t for t in logs) == 1
    # 冷却过期后再次记录, 带被抑制期间累计次数 (A 共 2 次命中)
    stra._veto_last["risk1"] -= stra._VETO_LOG_COOLDOWN + 1
    stra._veto_log(ctx, "risk1", "veto A")
    tail = [t for _, t in ctx.logs if "veto A" in t][-1]
    assert "[x2 since last log]" in tail


def test_confirm_price_is_decision_price_not_signal_book_avgpx():
    """记账价 = 决策价 (触发/收盘), 不再从信号账面 avgpx 反推。

    旧 `_implied_fill` 的输入是"信号口径"的 position/avgpx: set_position
    即改写信号仓, 真实成交要等下一个 tick 落地, 故 confirm 时反推得到的
    是"新信号仓 × 旧成交均价"的混合值 (加仓场景甚至等于加仓前旧均价
    3400, 而非真实成交价) — 该口径 2026-09-15 删除, 真实成交价统一由
    `_reanchor_actual_fill` (逐笔成交优先) 回写。
    """
    stra = _make_stra()
    ctx = FakeCtx(pos=100, avgpx=3400.0)
    stra._engine.confirm_enter(1, 100, 3400.0)
    dec = {"decision": DEC_ENTER, "direction": 1, "price": 3500.0,
           "n": 30.0, "failsafe": False}
    stra._execute(ctx, dec, n=30.0)
    assert abs(stra._engine.layers[-1][1] - 3500.0) < 1e-9   # 决策价
    assert not hasattr(stra, "_implied_fill")                # 旧口径已删


def test_add_confirm_price_is_decision_price():
    """加仓同理: 记账价=加仓位决策价, 不受旧均价干扰 (待回写修正)"""
    stra = _make_stra()
    ctx = FakeCtx(pos=100, avgpx=3400.0)
    stra._engine.confirm_enter(1, 100, 3400.0, n=30.0)
    ctx.stra_set_position("SHFE.rb.HOT", 100, "Keep")     # 同步框架仓
    dec = {"decision": DEC_ADD, "price": 3500.0, "n": 30.0}
    stra._execute(ctx, dec, n=30.0)
    assert abs(stra._engine.layers[-1][1] - 3500.0) < 1e-9


def test_reanchor_short_signed_avgpx_no_false_roll():
    """空头持仓: avgpx 符号不应触发假换月"""
    stra = _make_stra()
    stra._engine.confirm_enter(-1, 100, 3773.0)
    ctx = FakeCtx(pos=-100, avgpx=-3773.0)   # 空头负 avgpx, 无真实漂移
    stra._reanchor_on_roll(ctx)
    assert stra._engine.layers[0][1] == 3773.0   # 未被错误平移
    assert not any("roll" in t for _, t in ctx.logs)


def test_reanchor_on_hot_roll():
    """持仓不变但 avgpx 漂移(换月) → 层价格平移"""
    stra = _make_stra()
    stra._engine.confirm_enter(1, 100, 3400.0)
    stra._engine.confirm_add(100, 3500.0)
    # 框架侧换月: 持仓量不变, 均价升 50 (新合约升水)
    ctx = FakeCtx(pos=200, avgpx=3500.0)
    stra._reanchor_on_roll(ctx)
    assert stra._engine.layers[0][1] == 3450.0
    assert stra._engine.layers[1][1] == 3550.0
    assert any("roll" in t for _, t in ctx.logs)


def test_no_reanchor_when_position_mismatch():
    """持仓量与层数不符(部分成交) → 不 re-anchor"""
    stra = _make_stra()
    stra._engine.confirm_enter(1, 100, 3400.0)
    ctx = FakeCtx(pos=50, avgpx=3600.0)   # 量不符
    stra._reanchor_on_roll(ctx)
    assert stra._engine.layers[0][1] == 3400.0


def test_exit_executes_and_records_result():
    stra = _make_stra()
    ctx = FakeCtx(pos=100, avgpx=3400.0)
    stra._engine.confirm_enter(1, 100, 3400.0)
    dec = {"decision": DEC_EXIT, "reason": "stop", "price": 3340.0}
    stra._execute(ctx, dec, n=30.0)
    assert ctx.orders == [("SHFE.rb.HOT", 0, "Exit")]
    assert stra._engine.direction == 0
    assert stra._engine.s1_last["result"] == "loss"
