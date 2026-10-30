"""策略层日损闸测试 (2026-10-20)

- 当日首笔权益建基线
- stop 阈值: 停止新开/加仓
- flatten 阈值: 全平
- 新交易日复位
"""
from conftest import *  # noqa: F401,F403
from StraTurtle import StraTurtle
from schemas import InstrumentConfig, TurtleBaseConfig, RiskConfig


class GuardCtx:
    def __init__(self, date=20260101, fund=0.0, pos=0, px=3400.0):
        self.date = date
        self.fund = fund
        self.pos = pos
        self.px = px
        self.orders = []
        self.logs = []

    def stra_get_date(self):
        return self.date

    def stra_get_fund_data(self, flag=0):
        return self.fund

    def stra_get_position(self, code, *a, **k):
        return self.pos

    def stra_set_position(self, code, target, tag=""):
        self.orders.append((code, target, tag))
        self.pos = target

    def stra_get_price(self, code):
        return self.px

    def stra_log_text(self, text, level=1):
        self.logs.append((level, text))

    def add_chart_mark(self, price, icon, tag):
        pass


def _mk(stop=0.03, flat=0.05):
    inst = InstrumentConfig(code="SHFE.rb.HOT", period="d1", system="S1",
                            units=1)
    cfg = RiskConfig(fallback_capital=1_000_000,
                     daily_loss_stop_frac=stop,
                     daily_loss_flatten_frac=flat)
    stra = StraTurtle("g", inst, TurtleBaseConfig(), risk_cfg=cfg)
    # 本文件测日损阈值/复位，不涉及实盘成交证据；使用回测即时成交。
    stra._is_backtest = True
    stra._volscale = 10.0
    stra._pricetick = 1.0
    return stra


def test_daily_stop_blocks_new_entries():
    stra = _mk()
    ctx = GuardCtx(fund=0.0)
    stra._update_daily_guard(ctx)
    assert stra._day_start_equity == 1_000_000
    assert not stra._daily_blocked

    ctx.fund = -40_000.0          # 权益 96 万, -4%
    stra._update_daily_guard(ctx)
    assert stra._daily_blocked
    assert any("daily loss" in t for _, t in ctx.logs)

    lots, _ = stra._size_and_cap(ctx, "enter", n=50.0, price=3400.0,
                                 tag="1")
    assert lots == 0
    assert any("日损闸" in t for _, t in ctx.logs)


def test_daily_flatten_and_reset_next_day():
    stra = _mk()
    stra._engine.confirm_enter(1, 10, 3400.0, n=30.0)
    ctx = GuardCtx(fund=0.0, pos=10)
    stra._update_daily_guard(ctx)
    ctx.fund = -60_000.0          # -6% <= -5%
    stra._update_daily_guard(ctx)
    assert stra._daily_flattened
    assert ctx.pos == 0
    assert ctx.orders[-1] == ("SHFE.rb.HOT", 0, "DailyStop")
    assert stra._engine.direction == 0
    assert any("flatten" in t for _, t in ctx.logs)

    # 新交易日复位
    ctx.date = 20260102
    ctx.fund = 0.0
    stra._update_daily_guard(ctx)
    assert not stra._daily_blocked
    assert not stra._daily_flattened
    assert stra._day_start_equity == 1_000_000


def test_daily_guard_disabled_by_default():
    inst = InstrumentConfig(code="SHFE.rb.HOT", period="d1", system="S1")
    stra = StraTurtle("g", inst, TurtleBaseConfig())
    ctx = GuardCtx(fund=-500_000.0)
    stra._update_daily_guard(ctx)
    assert not stra._daily_blocked
    assert not stra._daily_flattened


def test_flatten_only_also_blocks_reentry():
    """只配 flatten、不配 stop 时, 全平后也必须封锁再开仓。"""
    stra = _mk(stop=0.0, flat=0.05)
    ctx = GuardCtx(fund=0.0)
    stra._update_daily_guard(ctx)
    ctx.fund = -60_000.0
    stra._update_daily_guard(ctx)
    assert stra._daily_flattened
    assert stra._daily_blocked
    lots, _ = stra._size_and_cap(ctx, "enter", n=50.0, price=3400.0,
                                 tag="1")
    assert lots == 0
