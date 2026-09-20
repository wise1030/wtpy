"""涨跌停锁死检测测试 (P2-10, 有界实现)

口径: tick 价 pinned > limit_lock_secs 且存在未闭合 fill_pending
→ 置位 + 告警; 置位期间 veto 加仓 (出场/止损不受影响); 价格恢复
移动即清除; 无 fill_pending 不误判 (低波动≠锁死)。
"""
import time

from schemas import InstrumentConfig, RiskConfig, TurtleBaseConfig
from StraTurtle import StraTurtle


class _NullNotify:
    def send(self, key, text):
        return False


def _make_stra(**base_kw):
    kw = {"entry_mode": "trigger", "limit_lock_secs": 30}
    kw.update(base_kw)
    base = TurtleBaseConfig(**kw)
    inst = InstrumentConfig(code="SHFE.rb.HOT", units=1)
    return StraTurtle("turtle_SHFE_rb_HOT", inst, base,
                      RiskConfig(), notify=_NullNotify())


class _Ctx:
    def __init__(self):
        self.logs = []

    def stra_log_text(self, txt, level=1):
        self.logs.append((level, txt))


def _tick(stra, ctx, px, ts_shift=0):
    """模拟一次 on_tick 的价格钉住检测 (直接调内部方法)。

    ts_shift: 先把钉住基线前移 (模拟时间流逝), 再检测。"""
    if ts_shift and stra._pin_price is not None:
        stra._pin_price[1] -= ts_shift
    stra._detect_limit_lock(ctx, stra._inst.code, px)


def test_pin_with_pending_sets_lock_and_alerts():
    stra = _make_stra()
    ctx = _Ctx()
    stra._fill_pending["S1"] = {"engine": stra._engine, "ts": time.time()}
    _tick(stra, ctx, 3500.0)             # 建立钉住基线
    _tick(stra, ctx, 3500.0, ts_shift=40)  # pinned 40s > 30s
    assert stra._limit_lock.get("SHFE.rb.HOT") is True
    assert any("limit-lock suspected" in t for _, t in ctx.logs)
    assert any(k == 2 for k, _ in ctx.logs)


def test_no_pending_no_lock():
    stra = _make_stra()
    ctx = _Ctx()
    _tick(stra, ctx, 3500.0)
    _tick(stra, ctx, 3500.0, ts_shift=3600)
    assert stra._limit_lock.get("SHFE.rb.HOT") is None


def test_price_moving_clears_lock():
    stra = _make_stra()
    ctx = _Ctx()
    stra._fill_pending["S1"] = {"engine": stra._engine, "ts": time.time()}
    _tick(stra, ctx, 3500.0)
    _tick(stra, ctx, 3500.0, ts_shift=40)
    assert stra._limit_lock
    _tick(stra, ctx, 3501.0)             # 价格动了 → 清除
    assert stra._limit_lock.get("SHFE.rb.HOT") is None
    assert any("limit-lock cleared" in t for _, t in ctx.logs)


def test_disabled_when_secs_zero():
    stra = _make_stra(limit_lock_secs=0)
    ctx = _Ctx()
    stra._fill_pending["S1"] = {"engine": stra._engine, "ts": time.time()}
    _tick(stra, ctx, 3500.0)
    _tick(stra, ctx, 3500.0, ts_shift=3600)
    assert stra._limit_lock.get("SHFE.rb.HOT") is None


def test_add_vetoed_under_lock():
    """锁死置位 → DEC_ADD 被 veto 且 reject_pending (与风控否决对齐)"""
    stra = _make_stra()
    ctx = _Ctx()
    stra._limit_lock["SHFE.rb.HOT"] = True
    e = stra._engine
    e.confirm_enter(1, 10, 3400.0, n=50.0)
    assert e._pending is None
    e._pending = {"kind": "add", "dir": 1}   # 引擎内部挂起格式
    dec = {"decision": "add", "price": 3450.0}
    stra._execute(ctx, dec, 50.0, None)
    assert e._pending is None            # reject_pending 已解除武装
    assert any("limit-locked" in t for _, t in ctx.logs)
