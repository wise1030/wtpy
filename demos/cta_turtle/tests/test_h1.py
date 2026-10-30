"""h1 (小时级) 支持测试 (2026-10-21 一期: m5→h1 聚合 + 评估锚点)"""
import json

import numpy as np
import pytest

from conftest import *  # noqa: F401,F403
from core.aggregate import h1_bucket, aggregate_h1
from core.ConfigLoader import ConfigLoader

# 交易日历: 20200102(四) 20200103(五) 20200106(一) — 周末不在内
DAYS = [20200102, 20200103, 20200106]


def _m5(date, hm, o=100.0, h=101.0, l=99.0, c=100.5, v=10.0, oi=500.0):
    return (date, hm, o, h, l, c, v, oi)


class TestH1Bucket:
    def test_day_session_start_time_labeling(self):
        # 收盘时刻语义: 10:00 bar (覆盖 09:55-10:00) 起始在 09 点桶
        assert h1_bucket(20200102, 90500, DAYS) == (20200102, 900)
        assert h1_bucket(20200102, 95500, DAYS) == (20200102, 900)
        assert h1_bucket(20200102, 100000, DAYS) == (20200102, 900)
        assert h1_bucket(20200102, 100500, DAYS) == (20200102, 1000)

    def test_night_session_maps_to_next_trading_day(self):
        # 周四夜盘 (日历日周四) 属于周五交易日
        assert h1_bucket(20200102, 210500, DAYS) == (20200103, 2100)
        assert h1_bucket(20200102, 230000, DAYS) == (20200103, 2200)
        # post-midnight bar 日历日即交易日 (周五凌晨属周五)
        assert h1_bucket(20200103, 500, DAYS) == (20200103, 0)
        # 周五夜盘 → 下周一 (周末不入日历)
        assert h1_bucket(20200103, 210500, DAYS) == (20200106, 2100)

    def test_break_bars_stay_in_hour_bucket(self):
        # 午休后 10:35-11:00 bar 起始钟点 10 → 并入 10 点桶
        assert h1_bucket(20200102, 103500, DAYS) == (20200102, 1000)
        assert h1_bucket(20200102, 110000, DAYS) == (20200102, 1000)
        assert h1_bucket(20200102, 110500, DAYS) == (20200102, 1100)
        # 下午 13:35 起属 13 点桶; 15:00 bar (覆盖 14:55-15:00) 属 14 点桶
        assert h1_bucket(20200102, 133500, DAYS) == (20200102, 1300)
        assert h1_bucket(20200102, 150000, DAYS) == (20200102, 1400)


class TestAggregateH1:
    def test_ohlcv_aggregation_and_closure(self):
        rows = [
            _m5(20200102, 90500, o=100, h=102, l=99, c=101),
            _m5(20200102, 91000, o=101, h=103, l=100, c=102, v=20, oi=510),
            _m5(20200102, 91500, o=102, h=104, l=101, c=103),
            # 10 点桶 (跨午休): 10:05, 10:15, 10:35
            _m5(20200102, 100500, o=103, h=105, l=102, c=104),
            _m5(20200102, 101500, o=104, h=106, l=103, c=105),
            _m5(20200102, 103500, o=105, h=107, l=104, c=106),
            _m5(20200102, 110000, o=106, h=108, l=105, c=107, oi=520),
        ]
        out = aggregate_h1(rows, DAYS)
        # 09 点桶闭合 (后有 10 点桶), 10 点桶是尾部未完成
        assert out["n_closed"] == 1
        assert out["keys"] == [202001020900, 202001021000]
        assert out["opens"][0] == 100          # 首根 open
        assert out["highs"][0] == 104          # max(102,103,104)
        assert out["lows"][0] == 99
        assert out["closes"][0] == 103         # 末根 close (09:15 bar)
        assert out["volumes"][0] == 40.0       # 10+20+10
        assert out["ois"][0] == 500.0          # 桶内最新一根 (09:15) 的 OI
        # 10 点桶并入午休后 bar
        assert out["highs"][1] == 108
        assert out["closes"][1] == 107

    def test_night_and_post_midnight_same_trading_day(self):
        rows = [
            _m5(20200102, 210500, o=50, h=51, l=49, c=50.5),   # 周四夜→周五
            _m5(20200102, 220000, o=50.5, h=52, l=50, c=51.5),
            _m5(20200103, 500, o=51.5, h=53, l=51, c=52.5),    # 周五凌晨
            _m5(20200103, 90500, o=52.5, h=54, l=52, c=53.5),
        ]
        out = aggregate_h1(rows, DAYS)
        # 21 点桶 (21:05, 22:00[起始21:55]) / 0 点桶 (00:05) / 9 点桶 (尾部)
        assert out["keys"][:3] == [202001032100, 202001030000,
                                   202001030900]
        assert out["dates"][0] == 20200103     # 夜盘归属周五
        assert out["n_closed"] == 2

    def test_keys_monotonic(self):
        import random
        random.seed(7)
        rows = []
        for d in DAYS:
            for hm in range(905, 1500, 5):
                if 1130 <= hm < 1335 or not (0 <= hm % 100 < 60):
                    continue
                rows.append(_m5(d, hm * 100))
        random.shuffle(rows)
        rows.sort(key=lambda r: (r[0], r[1]))
        out = aggregate_h1(rows, DAYS)
        assert out["keys"] == sorted(out["keys"])

    def test_arrays_numpy_convertible(self):
        out = aggregate_h1([_m5(20200102, 90500), _m5(20200102, 91000)],
                           DAYS)
        for k in ("dates", "times", "opens", "highs", "lows", "closes",
                  "volumes", "ois"):
            np.asarray(out[k])               # 不得抛异常


VALID_YAML = """\
base:
  unit_var: 200000
instruments:
  - code: SHFE.rb.HOT
    period: h1
    system: S1
    units: 1
"""


def test_config_loader_accepts_h1(tmp_path):
    """h1 一期后 load 必须放行 (原显式拒绝已移除)"""
    p = tmp_path / "cfg.yaml"
    p.write_text(VALID_YAML, encoding="utf-8")
    cfg = ConfigLoader.load(str(p), warn=False)
    assert cfg.instruments[0].period == "h1"


# ----------------------------------------------------------------------
# 策略级端到端 (StraTurtle h1 路径)
# ----------------------------------------------------------------------
from StraTurtle import StraTurtle                     # noqa: E402
from schemas import InstrumentConfig, TurtleBaseConfig  # noqa: E402
from test_intrabar import FakePeriodBars, MultiCtx    # noqa: E402

_H1_DAYS = [20230001 + i for i in range(13)]          # 13 个"交易日"
_LAST = _H1_DAYS[-1]


def _make_h1_stra(entry_mode="close"):
    inst = InstrumentConfig(code="SHFE.rb.HOT", period="h1",
                            system="S1", units=1)
    base = TurtleBaseConfig(entry_mode=entry_mode)
    return StraTurtle("h1t", inst, base)


def _fill_h1_fixture(ctx, breakout_last_bucket=False):
    """13 天日盘 m5 (每天 5 个钟点桶 × 12 根) + d1 日历。
    breakout_last_bucket: 末日 14 点桶 high=3450/close=3445 (突破)。"""
    d1 = ctx.add_store("d1")
    for d in _H1_DAYS:
        d1.append(d, 1500, 3400, 3401, 3399, 3400.5)
    m5 = ctx.add_store("m5")
    for d in _H1_DAYS:
        for hh in (9, 10, 11, 13, 14):
            for k in range(12):
                t = (hh * 10000 + (k + 1) * 500) if k < 11 \
                    else (hh + 1) * 10000
                h, c = 3401.0, 3400.5
                if breakout_last_bucket and d == _LAST and hh == 14:
                    h, c = 3450.0, 3445.0
                m5.append(d, t, 3400.0, h, 3399.0, c)
    return m5


def test_h1_eval_cadence_anchor_and_snapshot():
    """首 call 评最新闭合桶(11→13 随数据推进), 锚点/日级日期随动,
    快照含 h1_key, 重启恢复后不重复评估"""
    stra = _make_h1_stra()
    ctx = MultiCtx()
    _fill_h1_fixture(ctx)
    stra.on_init(ctx)
    assert ctx.prepared[1] == "m5"               # 主K=m5 (h1 无原生K线)
    stra.on_calculate(ctx)
    # 末日尾部桶=14 (trailing), 最新闭合桶=13
    assert stra._h1_key == _LAST * 10000 + 1300
    assert stra._d1_date == _LAST
    assert ctx.orders == []                      # 横盘无信号
    snap = json.loads(ctx.kv["h1t_engine"])
    assert snap["h1_key"] == _LAST * 10000 + 1300
    assert snap["h1_m5_key"] is not None

    # live 推进: 追加末日 15:05 bar (起始 15:00 → 15 点桶) → 尾部
    # 桶 14 闭合 → 评估推进 (注意 fixture 已含 14:00/14:05, 不能重复)
    m5 = ctx.klines["m5"]
    m5.append(_LAST, 150500, 3400.0, 3401.0, 3399.0, 3400.5)
    stra.on_calculate(ctx)
    assert stra._h1_key == _LAST * 10000 + 1400    # 桶 14 已评估
    assert stra._d1_date == _LAST

    # 重启恢复: 同名新实例加载快照, 无新 bar → 不重复评估
    stra2 = _make_h1_stra()
    ctx2 = MultiCtx()
    ctx2.kv["h1t_engine"] = ctx.kv["h1t_engine"]
    _fill_h1_fixture(ctx2)
    stra2.on_init(ctx2)
    assert stra2._h1_key == _LAST * 10000 + 1400   # 快照=推进后的锚点
    stra2.on_calculate(ctx2)
    assert stra2._h1_key == _LAST * 10000 + 1400  # 无新 bar, 锚点挡住重放


def test_h1_close_mode_breakout_enters_on_flush():
    """末日 14 点桶突破 20h 通道: on_calculate 评 13 点桶(无信号),
    session_end flush 闭合 14 点桶 → 收盘入场 @3445"""
    stra = _make_h1_stra("close")
    ctx = MultiCtx()
    _fill_h1_fixture(ctx, breakout_last_bucket=True)
    stra.on_init(ctx)
    stra.on_calculate(ctx)
    assert ctx.orders == []
    assert stra._h1_key == _LAST * 10000 + 1300
    stra.on_session_end(ctx, _LAST)
    assert stra._h1_key == _LAST * 10000 + 1400   # flush 评估尾部桶
    assert len(ctx.orders) == 1
    code, target, _tag = ctx.orders[0]
    assert code == "SHFE.rb.HOT" and target > 0    # 多头入场
    assert stra._engine.direction == 1


def test_h1_trigger_mode_arms_on_flush():
    """trigger: flush 后使用排除信号根的突破位，不把 3450 包络加回"""
    stra = _make_h1_stra("trigger")
    ctx = MultiCtx()
    _fill_h1_fixture(ctx, breakout_last_bucket=True)
    stra.on_init(ctx)
    stra.on_calculate(ctx)
    stra.on_session_end(ctx, _LAST)
    assert ctx.orders == []                       # 武装不产生订单
    lv = stra._engine.entry_levels()[1]
    assert lv is not None and lv == pytest.approx(3401.0)


def test_h1_bt_trigger_checks_m5_before_new_h1_eval():
    stra = _make_h1_stra("trigger")
    ctx = MultiCtx()
    ctx.is_backtest = True
    _fill_h1_fixture(ctx)
    stra.on_init(ctx)
    stra.on_calculate(ctx)                       # 建立推进基线
    events = []
    stra._run_trigger_bar = lambda *args: events.append("check_m5")
    stra._eval_h1_bar = lambda *args: events.append("eval_h1")

    m5 = ctx.klines["m5"]
    m5.append(_LAST, 150500, 3400.0, 3401.0, 3399.0, 3400.5)
    stra.on_calculate(ctx)
    assert events == ["check_m5", "eval_h1"]
