"""可插拔过滤器框架测试

覆盖: 注册表/参数校验、DirectionGate、AdxTrend(对照朴素循环)、
VolatilityBand 分位带、LiquidityFloor、build_chain 装配。
"""
import math

import numpy as np
import pytest

from conftest import *  # noqa: F401,F403
from filters.base import (FilterContext, get_filter_class,
                          known_filters, SignalFilterBase)
from filters.builtins import (DirectionGateFilter, AdxTrendFilter,
                              VolatilityBandFilter, LiquidityFloorFilter)
from filters import build_chain
from helpers.indicators import wilder_adx


def _ctx(sig_dir=1, nbars=120, trend=True, **kw):
    rng = np.random.RandomState(11 if trend else 3)
    step = rng.randn(nbars) * (0.6 if trend else 3.5) + \
        (1.8 if trend else 0.0)
    closes = 3400.0 + np.cumsum(step)
    highs = closes + 4.0
    lows = closes - 4.0
    ctx = FilterContext(code="X", sig_dir=sig_dir, price=float(closes[-1]),
                        n=12.0, n_series=np.full(nbars, 12.0),
                        highs=highs, lows=lows, closes=closes,
                        volumes=kw.pop("volumes", np.full(nbars, 50000.0)),
                        open_interests=kw.pop("open_interests", None))
    for k, v in kw.items():
        setattr(ctx, k, v)
    return ctx


# ---------- 注册表 ----------

def test_builtin_filters_registered():
    names = known_filters()
    for n in ("DirectionGate", "AdxTrend", "VolatilityBand",
              "LiquidityFloor"):
        assert n in names


def test_unknown_filter_type_raises():
    with pytest.raises(ValueError, match="未知过滤器类型"):
        get_filter_class("Nope")


def test_unknown_param_rejected():
    with pytest.raises(ValueError, match="未知参数"):
        AdxTrendFilter({"typo": 1})


def test_base_setup_not_implemented():
    class F(SignalFilterBase):
        name = "F"
    with pytest.raises(NotImplementedError):
        F({})


# ---------- DirectionGate ----------

def test_direction_gate_semantics():
    g_long = DirectionGateFilter({"direction": "long"})
    assert g_long.allow_entry(_ctx(sig_dir=1))[0] is True
    assert g_long.allow_entry(_ctx(sig_dir=-1)) == (False, "gate=long")
    assert DirectionGateFilter().allow_entry(_ctx(sig_dir=-1))[0] is True
    with pytest.raises(ValueError, match="非法"):
        DirectionGateFilter({"direction": "up"})


# ---------- AdxTrend ----------

def _naive_adx(h, l, c, p):
    """朴素循环参照实现 (口径: 种子=前 p 项和/均值, 同型 EMA 递推)"""
    n = len(h)

    def tr(i):
        return h[0] - l[0] if i == 0 else max(
            h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))

    def dm(i):
        if i == 0:
            return 0.0, 0.0
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pu = up if (up > dn and up > 0) else 0.0
        md = dn if (dn > up and dn > 0) else 0.0
        return pu, md

    xs = [tr(i) for i in range(n)]
    xp, xm = [], []
    for i in range(n):
        pu, md = dm(i)
        xp.append(pu)
        xm.append(md)

    st, sp, sm = sum(xs[:p]), sum(xp[:p]), sum(xm[:p])
    di_p = [math.nan] * n
    di_m = [math.nan] * n
    if st > 0:
        di_p[p - 1] = 100.0 * sp / st
        di_m[p - 1] = 100.0 * sm / st
    for i in range(p, n):
        st += (xs[i] - st) / p
        sp += (xp[i] - sp) / p
        sm += (xm[i] - sm) / p
        if st > 0:
            di_p[i] = 100.0 * sp / st
            di_m[i] = 100.0 * sm / st

    dx = [math.nan] * n
    for i in range(p - 1, n):
        den = di_p[i] + di_m[i]
        if den > 0:
            dx[i] = 100.0 * abs(di_p[i] - di_m[i]) / den
    adx = [math.nan] * n
    start = p - 1
    win = dx[start:start + p]
    if not any(math.isnan(v) for v in win):
        a = sum(win) / p
        adx[start + p - 1] = a
        for i in range(start + p, n):
            a += (dx[i] - a) / p
            adx[i] = a
    return di_p, di_m, adx


def test_adx_matches_naive_reference():
    rng = np.random.RandomState(42)
    n = 300
    close = 3000 + np.cumsum(rng.randn(n) * 7)
    h = close + np.abs(rng.randn(n)) * 5 + 2
    lo = close - np.abs(rng.randn(n)) * 5 + 2
    got = wilder_adx(h, lo, close, 14)
    exp = _naive_adx(list(h), list(lo), list(close), 14)
    for g, e in zip(got, exp):
        e = np.asarray(e, dtype=float)
        mask = np.isfinite(e)
        assert np.allclose(g[mask], e[mask], atol=1e-9)


def test_adx_trend_filter_gates():
    f_allow_warmup = AdxTrendFilter({"period": 14, "threshold": 25})
    # 强趋势序列: ADX 高 → 放行
    ok, why = f_allow_warmup.allow_entry(_ctx(trend=True))
    assert ok is True
    # 震荡序列: ADX 低 → 否决且带原因
    ok, why = f_allow_warmup.allow_entry(_ctx(trend=False))
    assert ok is False and "adx" in why
    # 预热段: 数据不足按 on_warmup 策略
    short_ctx = _ctx(nbars=10)
    ok, why = AdxTrendFilter({"on_warmup": "allow"}).allow_entry(short_ctx)
    assert ok is True and why == "adx_warmup"
    ok, _ = AdxTrendFilter({"on_warmup": "deny"}).allow_entry(short_ctx)
    assert ok is False


# ---------- VolatilityBand ----------

def test_volatility_band_rank_logic():
    f = VolatilityBandFilter({"window": 100, "min_pct": 0.10,
                              "max_pct": 0.90})
    # 恒定 N → rank=1 (含自身) > 0.90 → 否决
    ok, why = f.allow_entry(_ctx())
    assert ok is False and "vol_rank" in why
    # 最新 N 处于中位 → 放行
    ctx = _ctx()
    ctx.n_series = np.linspace(5.0, 20.0, 120)
    ctx.n = 12.5
    ok, _ = f.allow_entry(ctx)
    assert ok is True
    # 极端高波动 → 否决
    ctx.n = 19.9
    ok, why = f.allow_entry(ctx)
    assert ok is False
    # 样本不足 → 预热策略
    ctx2 = _ctx()
    ctx2.n_series = np.array([10.0, 11.0])
    ctx2.n = 10.5
    ok, why = VolatilityBandFilter({"on_warmup": "deny"}).allow_entry(ctx2)
    assert ok is False and why == "vol_warmup"


# ---------- LiquidityFloor ----------

def test_liquidity_floor_volume_and_oi():
    f = LiquidityFloorFilter({"k": 5, "min_volume": 40000})
    ok, _ = f.allow_entry(_ctx())          # 均量 5 万 ≥ 4 万
    assert ok is True
    ok, why = LiquidityFloorFilter(
        {"min_volume": 60000}).allow_entry(_ctx())
    assert ok is False and "vol=" in why
    # OI 下限: 有数据且达标
    ctx = _ctx(open_interests=np.full(120, 999999.0))
    ok, _ = LiquidityFloorFilter({"min_oi": 500000}).allow_entry(ctx)
    assert ok is True
    # OI 数据缺失 → on_warmup 策略
    ok, why = LiquidityFloorFilter({"min_oi": 500000}).allow_entry(_ctx())
    assert ok is True and why == "liq_no_oi"
    ok, _ = LiquidityFloorFilter(
        {"min_oi": 500000, "on_warmup": "deny"}).allow_entry(_ctx())
    assert ok is False
    # 成交量数据整体缺失
    ok, why = LiquidityFloorFilter(
        {"on_warmup": "deny"}).allow_entry(_ctx(volumes=None))
    assert ok is False and why == "liq_no_volume"


# ---------- build_chain ----------

def _mk_inst(filters=None, direction="both"):
    from schemas import InstrumentConfig, FilterConfig
    return InstrumentConfig(code="SHFE.rb.HOT", period="d1", system="S1",
                            units=1, direction=direction,
                            filters=filters or [])


def test_build_chain_default_has_implicit_gate():
    chain = build_chain(_mk_inst(direction="short"))
    assert len(chain) == 1
    ok, why = chain[0].allow_entry(FilterContext(sig_dir=-1))
    assert ok is True
    ok, why = chain[0].allow_entry(FilterContext(sig_dir=1))
    assert ok is False and why == "gate=short"


def test_build_chain_appends_configured_order():
    from schemas import FilterConfig
    inst = _mk_inst([
        FilterConfig(type="AdxTrend", params={"threshold": 30}),
        FilterConfig(type="VolatilityBand"),
    ])
    chain = build_chain(inst)
    assert [type(f) for f in chain] == \
        [DirectionGateFilter, AdxTrendFilter, VolatilityBandFilter]


def test_filter_exception_fail_open_in_strategy():
    """过滤器抛异常时策略层记录并放行 (不阻断交易)"""
    from StraTurtle import StraTurtle
    from schemas import InstrumentConfig, TurtleBaseConfig, FilterConfig

    class Boom(SignalFilterBase):
        name = "Boom"

        def _setup(self, p):
            pass

        def allow_entry(self, ctx):
            raise RuntimeError("boom")

    inst = InstrumentConfig(code="SHFE.rb.HOT", period="d1", system="S1")
    stra = StraTurtle("t", inst, TurtleBaseConfig())
    stra._chain.append(Boom())
    logs = []

    class C:
        def stra_log_text(self, t, level=1):
            logs.append((level, t))

    assert stra._run_filter_chain(C(), 1, {"price": 1.0},
                                  FilterContext()) is True
    assert any("filter Boom error" in t for _, t in logs)
