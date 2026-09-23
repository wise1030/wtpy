"""成交确认证据链 + catchup 闸门重试 测试 (2026-09-22 事故修复)

事故背景:
- 真值链路 (rtdata.json) 因 CTP 重连查询错误自 2026-09-17 起永久卡死;
- fees 代理是信号口径 (信号落地即计费) → fail-open 把未成交瞬间
  误确认 → CF 47+47/p 29 手下单未达柜台且零告警;
- catchup 每日一次闸在首 tick 数据未就绪时被消费 → 当日不再自愈。

证据链口径: 真值增量 > 逐笔成交 journal > fees 代理 (真值陈旧时不采信)
"""
import time

from schemas import InstrumentConfig, RiskConfig, TurtleBaseConfig
from StraTurtle import StraTurtle


class _NullNotify:
    def __init__(self):
        self.events = []

    def send(self, key, text):
        self.events.append((key, text))
        return True


class _StaleTruth:
    """已配置但陈旧的真值源 (复现 2026-09-17 后的生产状态)"""

    def stale(self):
        return True

    def net_position(self, code):
        return None


class _FreshTruth:
    def stale(self):
        return False


class _Journal:
    """trades.csv journal 桩: fills_price 非 None 表示有真实成交"""

    def __init__(self, fills_price=None):
        self.fills_price = fills_price
        self.cursor_val = 42

    def cursor(self):
        return self.cursor_val

    def layer_price(self, code, cursor, lots, skip=0.0):
        return self.fills_price


def _make_stra(**kw):
    kw.setdefault("entry_mode", "trigger")
    base = TurtleBaseConfig(**kw)
    inst = InstrumentConfig(code="SHFE.rb.HOT", units=1)
    stra = StraTurtle("turtle_SHFE_rb_HOT", inst, base,
                      RiskConfig(), notify=_NullNotify())
    stra._truth = _StaleTruth()          # 直注: 已配置但陈旧
    return stra


class _FillCtx:
    def __init__(self, pos, fees):
        self._pos = pos
        self._fees = fees
        self.logs = []

    def stra_get_position(self, code):
        return self._pos

    def stra_get_fund_data(self, idx):
        return self._fees if idx == 3 else 0.0

    def stra_log_text(self, txt, level=1):
        self.logs.append((level, txt))


def _fp(lots=47):
    return {"engine": None, "ts": time.time(), "fees0": 100.0,
            "lots": lots, "cursor": 42, "skip": 0.0, "truth0": None}


class TestFillConfirmed:
    def test_journal_evidence_confirms_when_truth_stale(self):
        """真值陈旧 + fees 增长, 但 journal 有真实成交 → 确认 (证据2)"""
        stra = _make_stra()
        stra._trade_journal = _Journal(fills_price=16050.0)
        ctx = _FillCtx(pos=-159, fees=200.0)     # fees 增长 (信号口径)
        ok, src, _ = stra._fill_confirmed(ctx, _fp(47), "SHFE.rb.HOT", -159)
        assert ok is True
        assert src == "trades"

    def test_no_journal_no_confirm_when_truth_stale(self):
        """真值陈旧 + fees 增长 + 无逐笔成交 → 不确认 (保持窗口)

        旧实现在此场景 fees fail-open 瞬间误确认 → 幽灵无告警。"""
        stra = _make_stra()
        stra._trade_journal = _Journal(fills_price=None)
        ctx = _FillCtx(pos=-159, fees=200.0)
        ok, src, _ = stra._fill_confirmed(ctx, _fp(47), "SHFE.rb.HOT", -159)
        assert ok is False
        assert src == "framework"

    def test_timeout_releases_when_truth_stale(self):
        """真值陈旧 + 无成交 → 2x 超时后放行 (窗口不永久挂起)"""
        stra = _make_stra()
        stra._trade_journal = _Journal(fills_price=None)
        ctx = _FillCtx(pos=-159, fees=200.0)
        fp = _fp(47)
        ok, _, _ = stra._fill_confirmed(ctx, fp, "SHFE.rb.HOT", -159)
        assert ok is False
        fp["ts"] -= 1300                          # 越过 2x600s
        ok, _, _ = stra._fill_confirmed(ctx, fp, "SHFE.rb.HOT", -159)
        assert ok is True
        assert any("fills unverified" in t for _, t in ctx.logs)

    def test_fees_trusted_when_truth_not_configured(self):
        """真值未配置 (off/缺失) → fees 代理维持旧行为 (bt/无部署环境)"""
        stra = _make_stra()
        stra._truth = None                        # 未配置
        stra._trade_journal = _Journal(fills_price=None)
        ctx = _FillCtx(pos=-159, fees=200.0)
        ok, _, _ = stra._fill_confirmed(ctx, _fp(47), "SHFE.rb.HOT", -159)
        assert ok is True

    def test_framework_pos_mismatch_still_blocks(self):
        """框架信号仓 != 目标 (内部状态脱管) → 维持既有否决路径"""
        stra = _make_stra()
        stra._trade_journal = _Journal(fills_price=16050.0)
        ctx = _FillCtx(pos=-100, fees=200.0)      # != target -159
        ok, _, _ = stra._fill_confirmed(ctx, _fp(47), "SHFE.rb.HOT", -159)
        assert ok is False


class TestCatchupGate:
    """catchup 每日闸: 只有结论性出口才消费 (数据未就绪可重试)"""

    def _make(self):
        stra = _make_stra()
        return stra

    def test_gate_not_consumed_when_bars_short(self):
        stra = self._make()
        ctx = _ShortBarsCtx(rows=10)
        stra._catchup_eval(ctx, 20260922)
        assert stra._catchup_date is None          # 未消费 → 下一 tick 重试

    def test_gate_consumed_when_already_current(self):
        stra = self._make()
        stra._d1_date = 20260922
        ctx = _OkBarsCtx(last=20260922, rows=200)
        stra._catchup_eval(ctx, 20260922)
        assert stra._catchup_date == 20260922      # 已是最新 → 结论性消费

    def test_gate_not_consumed_on_mode_disabled(self):
        """模式性禁用 (bt/close 模式) → 消费闸 (避免每 tick 空转)"""
        stra = _make_stra(entry_mode="close")
        ctx = _ShortBarsCtx(rows=10)
        stra._catchup_eval(ctx, 20260922)
        assert stra._catchup_date == 20260922


class _ShortBarsCtx:
    """get_bars 返回不足 need 根 (首 tick 数据未就绪的复现)"""

    is_backtest = False

    def __init__(self, rows, last=20260922):
        self.rows = rows
        self.last = last

    def stra_get_bars(self, code, period, cnt, isMain=False):
        return None if self.rows <= 0 else _Bars(self.rows, self.last)

    def stra_log_text(self, txt, level=1):
        pass


class _OkBarsCtx(_ShortBarsCtx):
    pass


class _Bars:
    def __init__(self, n, last_date):
        import numpy as np
        self.closes = np.full(n, 3400.0)
        self.highs = np.full(n, 3410.0)
        self.lows = np.full(n, 3390.0)
        self.date = np.full(n, last_date)

    def __len__(self):
        return len(self.closes)
