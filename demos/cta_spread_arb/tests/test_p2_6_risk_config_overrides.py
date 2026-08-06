"""P2-6: 验证 quick_stop 规则的阈值从 RiskConfig 读取(可配置化)。

默认值 = 原硬编码值,确保零回归;自定义值能改变触发行为,证明配置生效。

B-1(2026-07): profit_take 规则及其测试已删除 — 与 ExitJudgeStep
._pnl_trailing_check 双重追踪止盈, 统一保留信号层软止盈。
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


def test_profit_take_rule_removed_b1():
    """B-1(2026-07): profit_take 规则已从 RiskMgr 删除(与 ExitJudge 双重止盈),
    规则表与方法均不存在, 防止复活。"""
    rm = _make_riskmgr()
    assert 'profit_take' not in rm._risk_rules
    assert not hasattr(rm, '_builtin_profit_take_rule')
