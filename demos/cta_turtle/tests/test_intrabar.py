"""盘中止损路径测试 (P1: m5 主K + 信号周期日期去重)

覆盖: 首call基线不回放、盘中2N即时离场、日线信号单次消费、
快照v2含去重键恢复、v1裸引擎快照兼容。
"""
import json

import numpy as np
import pytest

from conftest import *  # noqa: F401,F403
from StraTurtle import StraTurtle
from schemas import InstrumentConfig, TurtleBaseConfig

BAR_DTYPE = [('date', 'i8'), ('time', 'i8'), ('open', 'f8'),
             ('high', 'f8'), ('low', 'f8'), ('close', 'f8'),
             ('volume', 'f8'), ('open_interest', 'f8')]


class FakePeriodBars:
    """可增量的结构化 K 线桩 (暴露 ndarray 与 OHLC 属性)"""

    def __init__(self):
        self.data = np.zeros(0, dtype=BAR_DTYPE)

    @property
    def ndarray(self):
        return self.data

    @property
    def opens(self):
        return self.data["open"]

    def _col(self, name):
        return self.data[name]

    @property
    def closes(self):
        return self._col("close")

    @property
    def highs(self):
        return self._col("high")

    @property
    def lows(self):
        return self._col("low")

    @property
    def volumes(self):
        return self._col("volume")

    def append(self, date, time, o, h, l, c, vol=10000.0, oi=50000.0):
        row = np.zeros(1, dtype=BAR_DTYPE)
        row["date"], row["time"] = date, time
        row["open"], row["high"], row["low"], row["close"] = o, h, l, c
        row["volume"], row["open_interest"] = vol, oi
        self.data = np.concatenate([self.data, row])

    def __len__(self):
        return len(self.data)


class MultiCtx:
    """双周期上下文: stra_get_bars 按 period 分发"""

    def __init__(self):
        self.klines = {}
        self.pos = 0
        self.avgpx = 0.0
        self.orders = []
        self.logs = []
        self.kv = {}
        self.marks = []

    def add_store(self, period):
        self.klines[period] = FakePeriodBars()
        return self.klines[period]

    # --- 框架数据 ---
    def stra_prepare_bars(self, code, period, cnt, isMain=False):
        self.prepared = (code, period, cnt, isMain)

    def stra_get_bars(self, code, period, cnt, isMain=False):
        return self.klines.get(period)

    def stra_get_comminfo(self, code):
        m = type("Comm", (), {})()
        m.volscale = 10.0
        m.pricetick = 1.0
        return m

    # --- 持仓 ---
    def stra_get_position(self, code, *a, **k):
        return self.pos

    def stra_set_position(self, code, target, tag=""):
        old = self.pos
        self.orders.append((code, target, tag))
        self.pos = target
        if target == 0:
            self.avgpx = 0.0
        elif old == 0:
            self.avgpx = float(
                self.klines.get("m5").data["close"][-1])
        else:
            added = abs(target) - abs(old)
            px = float(self.klines.get("m5").data["close"][-1])
            if added > 0:
                self.avgpx = (self.avgpx * abs(old)
                              + px * added) / abs(target)

    def stra_get_position_avgpx(self, code=""):
        return self.avgpx

    # --- 图表/日志/持久化 ---
    def set_chart_kline(self, code, period):
        pass

    def register_index(self, name, idxType=1):
        pass

    def register_index_line(self, idx, line, lineType=0):
        pass

    def add_index_baseline(self, idx, line, val):
        pass

    def set_index_value(self, idx, line, val):
        pass

    def add_chart_mark(self, price, icon, tag):
        self.marks.append((price, icon))

    def stra_log_text(self, text, level=1):
        self.logs.append((level, text))

    def user_save_data(self, key, val):
        self.kv[key] = val

    def user_load_data(self, key, default=None, vtype=str):
        return self.kv.get(key, default)


def _make_stra(intrabar=True):
    inst = InstrumentConfig(code="SHFE.rb.HOT", period="d1", system="S1",
                            units=2, intrabar_stop=intrabar)
    base = TurtleBaseConfig()
    return StraTurtle("tb", inst, base)


def _fill_daily(store: FakePeriodBars, n=90):
    """确定性正弦日K (收盘始终位于通道内, 无边沿), 返回末根收盘"""
    px = 3400.0
    for i in range(n):
        o = px
        c = 3400.0 + 8.0 * np.sin(i / 3.0)
        h = max(o, c) + 2.0
        l = min(o, c) - 2.0
        store.append(20230000 + i, 1500, o, h, l, c)
        px = c
    # 压制末根: 确保 prev_close <= prev_entry_upper (注入突破时边沿成立)
    store.data[-1]["close"] = 3395.0
    store.data[-1]["high"] = 3397.0
    store.data[-1]["low"] = 3393.0
    return float(store.data[-1]["close"])


def test_config_rejects_bad_subperiod():
    import os
    import tempfile

    from helpers.ConfigLoader import ConfigLoader
    bad = """
base:
  intrabar_period: m3
instruments:
  - code: SHFE.rb.HOT
"""
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "c.yaml")
        with open(p, "w", encoding="utf-8") as f:
            f.write(bad)
        with pytest.raises(ValueError, match="intrabar_period"):
            ConfigLoader.load(p)


def test_first_call_builds_baseline_without_orders():
    stra = _make_stra()
    ctx = MultiCtx()
    d1 = ctx.add_store("d1")
    _fill_daily(d1)
    m5 = ctx.add_store("m5")
    m5.append(20230089, 2105, 3400, 3401, 3399, 3400)
    stra.on_init(ctx)
    stra.on_calculate(ctx)
    assert ctx.orders == []                    # 基线 call 不回放历史信号
    assert stra._d1_date == 20230089           # d1 基线已建
    assert stra._m5_key is not None            # m5 键值基线已建


def test_intrabar_stop_exits_immediately():
    stra = _make_stra()
    ctx = MultiCtx()
    d1 = ctx.add_store("d1")
    last = _fill_daily(d1)
    m5 = ctx.add_store("m5")
    m5.append(20230089, 2105, last, last, last - 1, last)
    stra.on_init(ctx)
    stra.on_calculate(ctx)                     # 建基线
    # 手工建立多头持仓: 入场价抬高使止损位于近期波动带上方
    entry = last + 30.0                        # N≈5 → stop=entry-2N≈last+20
    stra._engine.confirm_enter(1, 50, entry)
    stra._risk.on_filled("SHFE.rb.HOT", 1, 50, entry,
                         n=stra._last_n or 5.0, volscale=10.0,
                         stop_mult=2.0)
    ctx.pos, ctx.avgpx = 50, entry
    # 新 m5: low 击穿止损
    stop_est = entry - 2.0 * float(stra._last_n)
    m5.append(20230089, 2110, stop_est + 5, stop_est + 6,
              stop_est - 3, stop_est - 2)
    stra.on_calculate(ctx)
    assert ctx.orders[-1] == ("SHFE.rb.HOT", 0, "ExitI")
    assert stra._engine.direction == 0
    assert stra._engine.s1_last["result"] == "loss"
    assert not stra._risk.has_position("SHFE.rb.HOT")
    assert any("intrabar exit" in t for _, t in ctx.logs)


def test_daily_signal_consumed_once_per_date():
    stra = _make_stra()
    ctx = MultiCtx()
    d1 = ctx.add_store("d1")
    _fill_daily(d1)
    m5 = ctx.add_store("m5")
    m5.append(20230089, 2105, 3400, 3401, 3399, 3400)
    stra.on_init(ctx)
    stra.on_calculate(ctx)
    # 注入突破边沿的新日线 (close 上破前 20 根 high)
    prev_high_max = float(np.max(d1.highs[-20:]))
    brk = prev_high_max * 1.01
    d1.append(20230090, 1500, brk - 1, brk + 2, brk - 3, brk)
    m5.append(20230090, 2105, brk, brk + 1, brk - 1, brk)
    stra.on_calculate(ctx)
    enters = [o for o in ctx.orders if o[1] > 0]
    assert len(enters) >= 1                    # 新日期触发一次入场
    n_enters_after_first_day = len(enters)
    # 同一天再喂多根 m5 → 不重复评估
    for tm in (2110, 2115, 2120):
        m5.append(20230090, tm, brk, brk + 1, brk - 1, brk)
        stra.on_calculate(ctx)
    enters = [o for o in ctx.orders if o[1] > 0]
    assert len(enters) == n_enters_after_first_day


def test_snapshot_v3_roundtrip_preserves_dedup():
    stra = _make_stra()
    ctx = MultiCtx()
    d1 = ctx.add_store("d1")
    _fill_daily(d1)
    ctx.add_store("m5").append(20230089, 2105, 3400, 3401, 3399, 3400)
    stra.on_init(ctx)
    stra.on_calculate(ctx)
    blob = json.loads(ctx.kv["tb_engine"])
    assert blob["v"] == 4                  # v4: +trigger武装状态
    assert "S1" in blob["subs"]
    assert blob["d1_date"] == 20230089
    # 同 kv 恢复到新实例: 同数据不再产生信号 (去重键生效)
    stra2 = _make_stra()
    stra2.on_init(ctx)
    assert stra2._d1_date == 20230089
    stra2.on_calculate(ctx)
    assert ctx.orders == []


def test_legacy_v1_blob_still_restores_engine():
    stra = _make_stra()
    ctx = MultiCtx()
    d1 = ctx.add_store("d1")
    _fill_daily(d1)
    ctx.add_store("m5")
    ctx.kv["tb_engine"] = json.dumps({
        "system": "S1", "direction": 1, "layers": [[10, 3400.0]],
        "s1_last": None, "fs_armed": 0})
    stra.on_init(ctx)
    assert stra._engine.direction == 1
    assert stra._d1_date is None               # v1 无去重键 → 走基线
