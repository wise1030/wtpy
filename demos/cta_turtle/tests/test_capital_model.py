"""Phase 1 资本模型 v2 回归 (2026-11-10): 25M 派生口径 + shadow 观察。

用户拍板真实可支配 25M。观察期生产行为零变化: shadow_capital>0 时
只按 25M 分配口径旁路计算手数并走否决日志 (带冷却+首见告警);
翻转后 base.unit_var=0 → 派生 = 分配资金 × unit_risk_frac (1%),
与 2% 单笔上限严格自洽 (1 unit 2N = 2%, 不再 0.75 截断)。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from test_stra_trigger import TrigCtx  # noqa: E402
from test_intrabar import _fill_daily  # noqa: E402

from schemas import (InstrumentConfig, RiskConfig,  # noqa: E402
                     TurtleBaseConfig)
from core.PositionSizer import PositionSizer  # noqa: E402
from core.ConfigLoader import ConfigLoader  # noqa: E402
from StraTurtle import StraTurtle  # noqa: E402


def _make(unit_var=200000.0, shadow=0.0, alloc_total=12):
    ctx = TrigCtx()
    ctx.is_backtest = True
    _fill_daily(ctx.add_store("d1"), n=90)
    risk = RiskConfig(shadow_capital=shadow)
    stra = StraTurtle("tf", InstrumentConfig(code="SHFE.rb.HOT", units=1),
                      TurtleBaseConfig(entry_mode="trigger",
                                       unit_var=unit_var),
                      risk_cfg=risk, alloc_total=alloc_total)
    return stra, ctx


def test_unit_var_explicit_wins():
    stra, _ = _make(unit_var=200000.0)
    assert stra._unit_var() == 200000.0      # 显式值优先 (黄金基线口径)


def test_unit_var_derived_from_fallback():
    stra, _ = _make(unit_var=0.0)
    stra._risk.cfg.fallback_capital = 25000000.0
    assert stra._unit_var() == 250000.0      # 25M × 1% (单品种独占口径)


def test_unit_var_derived_coherence():
    """派生口径自洽性: 1 unit 2N = 2% 单笔上限 (不再 0.75 截断)。"""
    stra, _ = _make(unit_var=0.0)
    stra._risk.cfg.fallback_capital = 25000000.0
    stra._risk.cfg.max_risk_per_mkt = 0.02
    uv = stra._unit_var()
    cap = stra._risk.cfg.fallback_capital * 0.02
    assert abs(uv * stra._base.stop_atr_mult - cap) < 1e-6


def test_sizer_per_call_unit_var():
    s = PositionSizer(200000.0)
    a = s.layer_lots(50.0, 10.0, 1)                  # 默认构造值
    b = s.layer_lots(50.0, 10.0, 1, unit_var=50000.0)
    assert a == 400 and b == 100
    assert s.layer_lots(50.0, 10.0, 1, unit_var=0.0) == 0


def test_shadow_logs_diff_but_no_behavior_change():
    stra, ctx = _make(unit_var=200000.0, shadow=25000000.0, alloc_total=12)
    stra.on_init(ctx)
    stra._volscale = 10.0
    stra._shadow_size(ctx, 50.0, 400)     # 现行 400 手
    hit = [t for _, t in ctx.logs if "shadow sizing" in t]
    assert hit, "shadow 应记录差异日志"
    assert "25M-model=41" in hit[0]       # 25M/12×1%/(50×10)=41 手
    # 行为不变: unit_var 仍显式值
    assert stra._unit_var() == 200000.0


def test_shadow_disabled_by_default():
    stra, ctx = _make(unit_var=200000.0, shadow=0.0)
    stra.on_init(ctx)
    stra._volscale = 10.0
    ctx.logs.clear()
    stra._shadow_size(ctx, 50.0, 400)
    assert not [t for _, t in ctx.logs if "shadow sizing" in t]


def test_config_validation_derived_needs_capital():
    import tempfile
    raw = ("base:\n  unit_var: 0\n"
           "instruments:\n  - code: SHFE.rb.HOT\n")
    with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                     delete=False) as f:
        f.write(raw)
        path = f.name
    try:
        try:
            ConfigLoader.load(path, warn=False)
        except ValueError as e:
            assert "unit_var" in str(e)
        else:
            raise AssertionError("unit_var=0 且无 portfolio_capital 应拒绝")
    finally:
        os.unlink(path)


def test_schemas_new_fields():
    r = RiskConfig()
    assert r.unit_risk_frac == 0.01 and r.shadow_capital == 0.0
    r.validate()
    for bad in (0.0, 0.2):
        try:
            RiskConfig(unit_risk_frac=bad).validate()
        except ValueError:
            pass
        else:
            raise AssertionError(f"unit_risk_frac={bad} 应被拒绝")
    try:
        RiskConfig(shadow_capital=-1).validate()
    except ValueError:
        pass
    else:
        raise AssertionError("shadow_capital=-1 应被拒绝")
