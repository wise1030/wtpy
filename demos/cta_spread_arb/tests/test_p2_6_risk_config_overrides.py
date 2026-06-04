"""P2-6: 验证 quick_stop / profit_take 规则的阈值从 RiskConfig 读取(可配置化)。

默认值 = 原硬编码值,确保零回归;自定义值能改变触发行为,证明配置生效。
"""
from conftest import make_sub_config  # noqa: F401


def _make_riskmgr(**risk_overrides):
    from schemas import RiskConfig
    from RiskMgr import RiskMgr
    cfg = RiskConfig(**risk_overrides)
    return RiskMgr(cfg)


def _make_ctx(rm, pnl_total: float, hold_bars: int):
    """构造 RiskContext stub 直接喂规则函数。"""
    from schemas import RiskContext
    rm._hold_bars = hold_bars
    sub = make_sub_config()
    return RiskContext(
        sub_config=sub,
        spread=None,
        spread_calc=None,
        positions={"A": 1, "B": -1},
        pnl={"A": pnl_total / 2, "B": pnl_total / 2},
        risk_cfg=rm._risk_cfg,
        hold_bars=hold_bars,
        coint_p=1.0,
        current_direction=1,
    )


# ============================================================
# RiskConfig 字段存在 + 默认值正确
# ============================================================

def test_risk_config_has_quick_stop_defaults():
    from schemas import RiskConfig
    cfg = RiskConfig()
    assert cfg.quick_stop_early_bars == 6
    assert cfg.quick_stop_early_loss == 300
    assert cfg.quick_stop_confirm_bars == 12
    assert cfg.quick_stop_confirm_loss == 500
    assert cfg.quick_stop_pure_loss == 300


def test_risk_config_has_profit_tier_defaults():
    from schemas import RiskConfig
    cfg = RiskConfig()
    assert cfg.profit_tier1_peak == 1000
    assert cfg.profit_tier2_peak == 2000
    assert cfg.profit_tier2_inc == 0.1
    assert cfg.profit_tier3_inc == 0.2


def test_risk_config_from_yaml_loads_quick_stop_overrides():
    from schemas import RiskConfig
    cfg = RiskConfig.from_yaml({
        'quick_stop_early_bars': 4,
        'quick_stop_early_loss': 200,
        'quick_stop_pure_loss': 150,
    })
    assert cfg.quick_stop_early_bars == 4
    assert cfg.quick_stop_early_loss == 200
    assert cfg.quick_stop_pure_loss == 150
    # 未覆盖字段仍走默认
    assert cfg.quick_stop_confirm_bars == 12


def test_risk_config_from_yaml_loads_profit_tier_overrides():
    from schemas import RiskConfig
    cfg = RiskConfig.from_yaml({
        'profit_tier1_peak': 500,
        'profit_tier2_peak': 1500,
        'profit_tier2_inc': 0.15,
        'profit_tier3_inc': 0.25,
    })
    assert cfg.profit_tier1_peak == 500
    assert cfg.profit_tier2_peak == 1500
    assert cfg.profit_tier2_inc == 0.15
    assert cfg.profit_tier3_inc == 0.25


# ============================================================
# quick_stop 规则: 默认行为(零回归)
# ============================================================

def test_quick_stop_default_early_triggers_at_300_loss_within_6bars():
    rm = _make_riskmgr()
    ctx = _make_ctx(rm, pnl_total=-350, hold_bars=4)
    msg = rm._builtin_quick_stop_rule(ctx)
    assert msg is not None
    assert "quick_stop_early" in msg


def test_quick_stop_default_does_not_trigger_at_250_loss():
    rm = _make_riskmgr()
    ctx = _make_ctx(rm, pnl_total=-250, hold_bars=4)
    assert rm._builtin_quick_stop_rule(ctx) is None


def test_quick_stop_default_confirm_triggers_at_500_loss_within_12bars():
    rm = _make_riskmgr()
    ctx = _make_ctx(rm, pnl_total=-550, hold_bars=10)
    msg = rm._builtin_quick_stop_rule(ctx)
    assert msg is not None
    assert "quick_stop_confirm" in msg


# ============================================================
# quick_stop 规则: 自定义阈值生效
# ============================================================

def test_quick_stop_custom_early_loss_override():
    """把 early_loss 从 300 改成 100 → -150 浮亏即触发(默认下不会触发)。"""
    # 默认配置: -150 不触发
    rm_default = _make_riskmgr()
    ctx_d = _make_ctx(rm_default, pnl_total=-150, hold_bars=3)
    assert rm_default._builtin_quick_stop_rule(ctx_d) is None

    # 自定义: early_loss=100 → -150 触发
    rm_custom = _make_riskmgr(quick_stop_early_loss=100)
    ctx_c = _make_ctx(rm_custom, pnl_total=-150, hold_bars=3)
    msg = rm_custom._builtin_quick_stop_rule(ctx_c)
    assert msg is not None and "quick_stop_early" in msg


def test_quick_stop_custom_early_bars_override():
    """把 early_bars 从 6 缩到 3 → hold=5 时 -350 不再走 early,只看 confirm。"""
    rm = _make_riskmgr(
        quick_stop_early_bars=3,
        quick_stop_early_loss=300,
        quick_stop_pure_loss=99999,  # 隔离 pure_loss 通道,只测 early_bars 边界
    )
    # hold_bars=5 已超 early_bars=3,且 -350 < confirm_loss=500 → 都不触发
    ctx = _make_ctx(rm, pnl_total=-350, hold_bars=5)
    assert rm._builtin_quick_stop_rule(ctx) is None


def test_quick_stop_custom_pure_loss_override():
    """pure_loss 从 300 改成 100 → -150 且 peak=0 触发。"""
    rm = _make_riskmgr(
        quick_stop_pure_loss=100,
        quick_stop_early_loss=99999,    # 关掉 early 通道
        quick_stop_confirm_loss=99999,  # 关掉 confirm 通道
    )
    # hold_bars 大,确保只能走 pure_loss 通道
    ctx = _make_ctx(rm, pnl_total=-150, hold_bars=50)
    assert rm._peak_unrealized == 0.0  # 从未盈利
    msg = rm._builtin_quick_stop_rule(ctx)
    assert msg is not None and "pure_loss" in msg


# ============================================================
# profit_take 规则: 默认行为(零回归)
# ============================================================

def test_profit_take_default_tier1_triggers_at_20pct_drawdown_under_1000():
    """peak=800 < tier1_peak=1000 → drawdown_ratio=0.2; 当前 < 800*0.8=640 时触发。"""
    rm = _make_riskmgr(profit_take_threshold=500, profit_drawdown_ratio=0.2)
    # 先把 peak 顶到 800
    ctx_peak = _make_ctx(rm, pnl_total=800, hold_bars=10)
    assert rm._builtin_profit_take_rule(ctx_peak) is None  # 还在 peak,无回撤
    # 当前回到 600 → 回撤 200/800=25% > 20% → 触发
    ctx_now = _make_ctx(rm, pnl_total=600, hold_bars=11)
    msg = rm._builtin_profit_take_rule(ctx_now)
    assert msg is not None and "profit_take" in msg


def test_profit_take_default_tier2_triggers_at_30pct_drawdown():
    """peak=1500 ∈ [1000,2000) → 0.2+0.1=0.3 → 当前<1500*0.7=1050 触发。"""
    rm = _make_riskmgr(profit_take_threshold=500, profit_drawdown_ratio=0.2)
    ctx_peak = _make_ctx(rm, pnl_total=1500, hold_bars=10)
    rm._builtin_profit_take_rule(ctx_peak)
    # 1100 > 1050 → 不触发
    ctx_not = _make_ctx(rm, pnl_total=1100, hold_bars=11)
    assert rm._builtin_profit_take_rule(ctx_not) is None
    # 1000 < 1050 → 触发
    ctx_hit = _make_ctx(rm, pnl_total=1000, hold_bars=12)
    assert rm._builtin_profit_take_rule(ctx_hit) is not None


def test_profit_take_default_tier3_triggers_at_40pct_drawdown():
    """peak=2500 >= tier2_peak=2000 → 0.2+0.2=0.4 → 当前<2500*0.6=1500 触发。"""
    rm = _make_riskmgr(profit_take_threshold=500, profit_drawdown_ratio=0.2)
    ctx_peak = _make_ctx(rm, pnl_total=2500, hold_bars=10)
    rm._builtin_profit_take_rule(ctx_peak)
    # 1600 > 1500 → 不触发
    ctx_not = _make_ctx(rm, pnl_total=1600, hold_bars=11)
    assert rm._builtin_profit_take_rule(ctx_not) is None
    # 1400 < 1500 → 触发
    ctx_hit = _make_ctx(rm, pnl_total=1400, hold_bars=12)
    assert rm._builtin_profit_take_rule(ctx_hit) is not None


# ============================================================
# profit_take 规则: 自定义阈值生效
# ============================================================

def test_profit_take_custom_tier_boundaries_shift_drawdown_band():
    """把 tier2_peak 从 2000 提到 5000 → peak=2500 仍走 tier2 (0.3) 而非默认 tier3 (0.4)。"""
    rm = _make_riskmgr(
        profit_take_threshold=500,
        profit_drawdown_ratio=0.2,
        profit_tier1_peak=1000,
        profit_tier2_peak=5000,   # 提高边界
        profit_tier2_inc=0.1,
        profit_tier3_inc=0.2,
    )
    ctx_peak = _make_ctx(rm, pnl_total=2500, hold_bars=10)
    rm._builtin_profit_take_rule(ctx_peak)
    # tier2 ratio=0.3 → 触发阈值=2500*0.7=1750
    # 当前=1700 < 1750 → 触发(若走默认 tier3 ratio=0.4 则阈值=1500,1700 不会触发)
    ctx_hit = _make_ctx(rm, pnl_total=1700, hold_bars=11)
    msg = rm._builtin_profit_take_rule(ctx_hit)
    assert msg is not None and "tier_ratio=30%" in msg


def test_profit_take_custom_inc_changes_drawdown_tolerance():
    """tier3_inc 从 0.2 改到 0.5 → tier3 ratio=0.7 → peak=3000 时阈值=900,极宽容。"""
    rm = _make_riskmgr(
        profit_take_threshold=500,
        profit_drawdown_ratio=0.2,
        profit_tier3_inc=0.5,  # 默认 0.2
    )
    ctx_peak = _make_ctx(rm, pnl_total=3000, hold_bars=10)
    rm._builtin_profit_take_rule(ctx_peak)
    # 默认: ratio=0.4 → 阈值=1800,1500 触发
    # 自定: ratio=0.7 → 阈值=900,1500 不触发
    ctx_now = _make_ctx(rm, pnl_total=1500, hold_bars=11)
    assert rm._builtin_profit_take_rule(ctx_now) is None
    # 当前=800 < 900 → 触发
    ctx_hit = _make_ctx(rm, pnl_total=800, hold_bars=12)
    msg = rm._builtin_profit_take_rule(ctx_hit)
    assert msg is not None and "tier_ratio=70%" in msg
