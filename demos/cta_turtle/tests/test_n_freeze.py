"""P0-2 N 冻结口径 + P0-1 脏状态守卫 / 收养重建 回归测试

口径 (2026-09-14):
- 止损与加仓步长使用**该层入/加仓时点冻结的 N** (layer_ns), 不用当根 N。
  当根 N 漂移会让实际止损亏损偏离风控记账的 2N (rb 日线实测持有 20 根
  N 比值 p95=1.40、极值 2.14), 并让加仓位随 N 缩小时下移。
- 无 N 来源时回退最近评估 N (旧快照 v4/直测路径行为保持不变)。
- direction≠0 且 layers 为空 = 脏快照: 不抛异常, 由策略层重建。
- 收养 (adopt) 外部持仓时价格必须为正 (框架空头 avgpx 为负)。
"""
from conftest import *  # noqa: F401,F403
import pytest

from helpers.TurtleEngine import TurtleEngine
from helpers.RiskManager import RiskManager
from schemas import RiskConfig


def _ch(entry=1e9, exit_=0.0):
    """宽通道: 只让止损参与判定"""
    return {
        'entry_upper': entry, 'entry_lower': -entry,
        'exit_upper': -exit_, 'exit_lower': exit_,
        'fs_upper': entry, 'fs_lower': -entry,
        'prev_entry_upper': entry, 'prev_entry_lower': -entry,
        'prev_fs_upper': entry, 'prev_fs_lower': -entry,
    }


# ----------------------------------------------------------- N 冻结口径

def test_stop_not_loosened_when_n_expands():
    """N 放大 (60→120) 不得放宽止损: 止损位保持 2880 (2×冻结 N);
    旧口径会随当根 N 下移到 2760 → 实际止损亏损达预算 2 倍"""
    e = TurtleEngine("S1", stop_atr_mult=2.0, stop_n_mode="entry")
    e.confirm_enter(1, 10, 3000.0, n=60.0)
    assert e.layer_ns == [60.0]
    assert abs(e.stop_price() - 2880.0) < 1e-9
    # low=2800 击穿冻结止损位 2880 → 离场 (旧口径下不会触发, 放任亏损扩大)
    dec = e.on_bar(high=3000, low=2800, close=2900, prev_close=2950,
                   n=120.0, channels=_ch())
    assert dec[0]["decision"] == "exit" and dec[0]["reason"] == "stop"
    assert dec[0]["price"] == 2880.0
    # 未击穿冻结止损位时不触发
    e2 = TurtleEngine("S1", stop_atr_mult=2.0, stop_n_mode="entry")
    e2.confirm_enter(1, 10, 3000.0, n=60.0)
    assert e2.on_bar(high=3000, low=2890, close=2950, prev_close=2950,
                     n=120.0, channels=_ch()) == []


def test_stop_uses_frozen_entry_n_when_n_shrinks():
    """N 缩小 (60→30) 不得收紧止损 (防噪声打掉)"""
    e = TurtleEngine("S1", stop_atr_mult=2.0, stop_n_mode="entry")
    e.confirm_enter(1, 10, 3000.0, n=60.0)
    # 旧口径 (当根 N=30 → 止损 2940) 会把 low=2900 误判为触发
    dec = e.on_bar(high=3000, low=2900, close=2950, prev_close=2980,
                   n=30.0, channels=_ch())
    assert dec == []
    assert abs(e.stop_price() - 2880.0) < 1e-9


def test_add_level_uses_frozen_entry_n():
    """加仓位 = 最后层价 ± 0.5×冻结 N, 不随当根 N 抬高"""
    e = TurtleEngine("S1", pyramid_layers=4, pyramid_step_atr=0.5, stop_n_mode="entry")
    e.confirm_enter(1, 10, 3000.0, n=60.0)
    # 冻结加仓位 3030; 当根 N=120 时旧口径为 3060
    dec = e.on_bar(high=3040, low=3010, close=3035, prev_close=3010,
                   n=120.0, channels=_ch())
    assert dec[0]["decision"] == "add"
    dec = e.on_bar(high=3030, low=3000, close=3025, prev_close=3010,
                   n=120.0, channels=_ch())
    assert dec == []


def test_add_layer_freezes_its_own_n():
    """每层各记自己的 N: 止损/加仓锚定最后一层"""
    e = TurtleEngine("S1", pyramid_layers=4, pyramid_step_atr=0.5, stop_n_mode="entry")
    e.confirm_enter(1, 10, 3000.0, n=60.0)
    e.confirm_add(10, 3030.0, n=80.0)
    assert e.layer_ns == [60.0, 80.0]
    assert abs(e.stop_price() - 2870.0) < 1e-9          # 3030 - 2*80
    assert abs(e._next_add_price() - 3070.0) < 1e-9     # 3030 + 0.5*80


def test_reanchor_last_price_keeps_frozen_n():
    """成交价回写只改价格, 不改该层冻结 N"""
    e = TurtleEngine("S1", stop_atr_mult=2.0, stop_n_mode="entry")
    e.confirm_enter(1, 10, 3000.0, n=60.0)
    e.reanchor_last_price(3005.0)
    assert e.layer_ns == [60.0]
    assert abs(e.stop_price() - 2885.0) < 1e-9


def test_update_n_supplies_fallback_without_on_bar():
    """close/intrabar 路径下引擎不经 on_bar 也能拿到当前 N (否则 NaN)"""
    e = TurtleEngine("S1", stop_atr_mult=2.0)
    e.adopt_position(1, 10, 3000.0)                     # 收养时无 N
    assert e.stop_price() != e.stop_price()             # NaN
    e.update_n(50.0)
    assert abs(e.stop_price() - 2900.0) < 1e-9


def test_legacy_snapshot_without_layer_ns_falls_back():
    """旧快照 (无 layer_ns) 回退最近评估 N, 行为与升级前一致"""
    e = TurtleEngine("S1", stop_atr_mult=2.0)
    e.restore({"direction": 1, "layers": [[10, 100.0]]})
    assert e.layer_ns == [None]
    assert e.stop_price() != e.stop_price()             # 尚无 N 来源
    dec = e.on_bar(high=99, low=89, close=93, prev_close=95, n=5.0,
                   channels=_ch())
    assert dec[0]["reason"] == "stop" and dec[0]["price"] == 90.0


def test_booked_risk_equals_frozen_stop_loss():
    """P0-2 一致性: 风控记账的 2N 潜亏 == 冻结 N 止损的实际亏损"""
    e = TurtleEngine("S1", stop_atr_mult=2.0, stop_n_mode="entry")
    e.confirm_enter(1, 10, 3000.0, n=60.0)
    rm = RiskManager(RiskConfig(fallback_capital=5e6))
    rm.on_filled("SHFE.rb.HOT", 1, 10, 3000.0, 60.0, 10.0, 2.0, "black")
    loss_at_stop = (3000.0 - e.stop_price()) * 10 * 10.0
    assert abs(loss_at_stop - rm.open_risk()) < 1e-6
    # 后续 N 漂移不再改变已记账风险与实际止损亏损
    e.update_n(120.0)
    assert abs((3000.0 - e.stop_price()) * 100.0 - rm.open_risk()) < 1e-6


def test_booked_risk_by_actual_stop_is_lower_for_pyramid():
    """P1 口径: 按实际止损位记账比 2N 口径小得多 (金字塔+棘轮收紧后)。

    实盘 2026-09-16: 2N 口径 52.7 万 vs 止损口径 20.4 万 → 组合预算被
    占满, 加仓全被 total_risk(cap=0) 否决。
    """
    e = TurtleEngine("S1", stop_atr_mult=2.0, pyramid_layers=4)
    e.confirm_enter(1, 100, 3000.0, n=50.0)
    e.confirm_add(100, 3100.0, n=50.0)      # 止损 = 3100 - 2*50 = 3000
    assert abs(e.stop_anchor() - 3000.0) < 1e-9
    by_stop = e.booked_risk(10.0, 2.0, stop=e.stop_anchor())
    by_2n = e.booked_risk(10.0, 2.0)
    # 止损口径: (3000-3000)*100*10 + (3100-3000)*100*10 = 100,000
    assert abs(by_stop - 100_000.0) < 1e-6
    assert abs(by_2n - 200_000.0) < 1e-6
    assert by_stop < by_2n


def test_booked_risk_by_stop_floors_negative():
    """止损在成本有利一侧 (锁盈) 的层贡献为负, 组合取净额并下限 0"""
    e = TurtleEngine("S1", stop_atr_mult=2.0, pyramid_layers=4)
    e.confirm_enter(1, 100, 2900.0, n=50.0)
    e.confirm_add(100, 3200.0, n=50.0)
    # 止损 3000: 首层锁盈 -100k, 末层亏 +200k → 净额 100k (小于"只算
    # 正贡献"的 200k, 这才是组合最坏亏损的正确口径)
    assert abs(e.booked_risk(10.0, 2.0, stop=3000.0) - 100_000.0) < 1e-6
    # 单层且止损高于成本 → 净额 ≤ 0 → 下限归零 (不产生负风险)
    e2 = TurtleEngine("S1", stop_atr_mult=2.0)
    e2.confirm_enter(1, 100, 2900.0, n=50.0)
    assert e2.booked_risk(10.0, 2.0, stop=3000.0) == 0.0


def test_layer_ns_backfilled_on_first_eval():
    """legacy 层 (layer_ns=None) 在首次评估时用当根 N 回填, 之后按冻结走"""
    e = TurtleEngine("S1", stop_atr_mult=2.0)
    e.restore({"direction": 1, "layers": [[10, 3000.0]]})
    assert e.layer_ns == [None]
    e.update_n(50.0)
    assert e.layer_ns == [50.0]
    assert abs(e.stop_price() - 2900.0) < 1e-9
    e.update_n(120.0)               # 回填后不再跟随当根 N (棘轮不放宽)
    assert abs(e.stop_price() - 2900.0) < 1e-9


@pytest.mark.parametrize("n_bad", [None, 0.0, -1.0, float("nan")])
def test_invalid_n_falls_back_to_market_n(n_bad):
    """无效 n 不写入 layer_ns (回退最近评估 N); 无来源时止损 NaN"""
    e = TurtleEngine("S1", stop_atr_mult=2.0)
    e.confirm_enter(1, 10, 3000.0, n=n_bad)
    assert e.stop_price() != e.stop_price()
    e.update_n(50.0)
    assert abs(e.stop_price() - 2900.0) < 1e-9


# ------------------------------------------------------- ratchet (默认口径)

def test_ratchet_tightens_on_n_shrink_but_never_loosens():
    """ratchet: 当根 N 但只收紧不放宽 (多头止损只上移)"""
    e = TurtleEngine("S1", stop_atr_mult=2.0, stop_n_mode="ratchet")
    e.confirm_enter(1, 10, 3000.0, n=60.0)
    assert abs(e.stop_price() - 2880.0) < 1e-9        # 初始 = 2×60
    # N 缩小 60→30 → 止损上移到 2940 (收紧)
    e.update_n(30.0)
    assert abs(e.stop_price() - 2940.0) < 1e-9
    # N 放大 30→120 → 候选 2760 更松, 棘轮保持 2940 (不放宽)
    e.update_n(120.0)
    assert abs(e.stop_price() - 2940.0) < 1e-9
    # 加仓位同为单调 (风险敞口只减不增): N 缩小不会让加仓位下移
    # (否则价格未实际推进 0.5N 也会触发加仓); N 放大时才抬到 3060
    assert abs(e._next_add_price() - 3060.0) < 1e-9


def test_ratchet_add_reanchors_to_new_layer():
    """加仓按新单位重锚 (经典口径): 棘轮只在两次加仓之间生效"""
    e = TurtleEngine("S1", pyramid_layers=4, stop_atr_mult=2.0,
                     stop_n_mode="ratchet")
    e.confirm_enter(1, 10, 3000.0, n=30.0)
    assert abs(e.stop_price() - 2940.0) < 1e-9
    e.confirm_add(10, 3100.0, n=30.0)                 # 新层 3100
    assert abs(e.stop_price() - 3040.0) < 1e-9        # 3100 - 2×30


def test_ratchet_survives_snapshot_and_roll():
    """棘轮位随快照持久化, 换月平移同步"""
    e = TurtleEngine("S1", stop_atr_mult=2.0, stop_n_mode="ratchet")
    e.confirm_enter(1, 10, 3000.0, n=60.0)
    e.update_n(30.0)
    assert abs(e.stop_price() - 2940.0) < 1e-9
    e2 = TurtleEngine("S1", stop_atr_mult=2.0, stop_n_mode="ratchet")
    e2.restore(e.snapshot())
    e2.update_n(120.0)
    assert abs(e2.stop_price() - 2940.0) < 1e-9       # 不放宽
    e2.shift_prices(50.0)                             # HOT 换月
    assert abs(e2.stop_price() - 2990.0) < 1e-9


def test_current_mode_still_loosens_for_ab():
    """current (A/B) 口径保留旧行为: N 放大则止损放宽"""
    e = TurtleEngine("S1", stop_atr_mult=2.0, stop_n_mode="current")
    e.confirm_enter(1, 10, 3000.0, n=60.0)
    assert abs(e.stop_price() - 2880.0) < 1e-9
    e.update_n(120.0)
    assert abs(e.stop_price() - 2760.0) < 1e-9        # 放宽 (旧行为)


# ------------------------------------------------------- 脏状态守卫 P0-1

def test_orphan_direction_without_layers_no_crash():
    """direction≠0 且 layers 为空 (脏快照): 不得抛 IndexError/ZeroDivision"""
    e = TurtleEngine("S1", stop_atr_mult=2.0)
    e.restore({"direction": 1, "layers": []})
    assert e.stop_price() != e.stop_price()             # NaN
    assert e.on_bar(high=101, low=99, close=101, prev_close=99, n=5.0,
                    channels=_ch()) == []
    e.rearm()
    e.confirm_exit(100.0)                              # 只复位不判定输赢
    assert e.direction == 0 and e.layers == []


def test_adopt_position_rejects_nonpositive_price():
    """收养价非法 (空头 avgpx 未取绝对值) → 不建仓, 不产生负止损"""
    e = TurtleEngine("S1", stop_atr_mult=2.0)
    assert e.adopt_position(-1, 5, -3150.0) == 0
    assert e.direction == 0 and e.layers == []
    assert e.adopt_position(-1, 5, 3150.0, n=60.0) == 5
    assert e.layers == [[5, 3150.0]]
    assert abs(e.stop_price() - 3270.0) < 1e-9          # 空头止损在上方


def test_adopt_position_trigger_mode_arms_stop():
    """trigger 模式收养后立即纳入管理; 空仓复位幂等"""
    e = TurtleEngine("S1", stop_atr_mult=2.0, entry_mode="trigger")
    e.update_n(60.0)
    e.adopt_position(-1, 5, 3150.0)
    assert abs(e.armed["stop"] - 3270.0) < 1e-9
    assert e.check_bar(o=3150, h=3160, l=3140, c=3155) == []
    dec = e.check_bar(o=3260, h=3280, l=3255, c=3275)
    assert dec[0]["decision"] == "exit" and dec[0]["price"] == 3270.0
    e.flatten_book()
    assert e.direction == 0 and e.layers == [] and e.layer_ns == []


def test_flatten_book_keeps_s1_filter_state():
    """flatten_book 只复位持仓, 不动 S1 过滤器记录"""
    e = TurtleEngine("S1", stop_atr_mult=2.0)
    e.confirm_enter(1, 10, 3000.0, n=60.0)
    e.flatten_book()
    assert e.s1_last is not None and e.s1_last.get("taken") is True


# --------------------------------------------- 启动收养 (B3) 端到端回归

class _Comm:
    volscale = 10.0
    pricetick = 1.0


class _InitCtx:
    """on_init 最小上下文: 券商有 116 手空头, 策略快照为空 (手工清状态/
    新部署)。框架 avgpx 对空头返回负值 (avgpx×pos=成本>0)。"""

    is_backtest = True          # 跳过执行单元预热

    def __init__(self, pos=-116, avgpx=-3460.0):
        self.pos = pos
        self.avgpx = avgpx
        self.logs = []
        self.orders = []

    def stra_prepare_bars(self, *a, **k):
        pass

    def stra_sub_ticks(self, code):
        pass

    def stra_get_comminfo(self, code):
        return _Comm()

    def set_chart_kline(self, *a, **k):
        pass

    def register_index(self, *a, **k):
        pass

    def register_index_line(self, *a, **k):
        pass

    def add_index_baseline(self, *a, **k):
        pass

    def user_load_data(self, key, *a, **k):
        return ""               # 无快照 → 空引擎持仓

    def stra_get_position(self, code, *a, **k):
        return self.pos

    def stra_get_position_avgpx(self, code=""):
        return self.avgpx

    def stra_get_price(self, code):
        return abs(self.avgpx)

    def stra_set_position(self, code, target, tag=""):
        self.orders.append((code, target, tag))

    def stra_log_text(self, text, level=1):
        self.logs.append((level, text))


def _mk_init_stra():
    from StraTurtle import StraTurtle
    from schemas import InstrumentConfig, TurtleBaseConfig
    inst = InstrumentConfig(code="SHFE.rb.HOT", period="d1", system="S1",
                            units=1)
    return StraTurtle("t", inst, TurtleBaseConfig(entry_mode="trigger"))


def test_on_init_adopts_short_with_positive_layer_price():
    """B3 回归: 收养空头时层价取 |avgpx| — 否者止损落到价格另一侧,
    下一根 bar 立刻以噪声价平掉真实持仓"""
    stra = _mk_init_stra()
    ctx = _InitCtx(pos=-116, avgpx=-3460.0)
    stra.on_init(ctx)
    e = stra._engine
    assert e.direction == -1
    assert e.layers == [[116, 3460.0]]                  # 正价, 不是 -3460
    stra._set_n(60.0)
    assert abs(e.stop_price() - 3580.0) < 1e-9          # 空头止损在上方
    # 价格在止损下方运行 → 不得触发任何离场
    assert e.check_bar(o=3460, h=3475, l=3450, c=3465) == []
    assert ctx.orders == []
    # 上穿止损才离场
    dec = e.check_bar(o=3575, h=3590, l=3570, c=3585)
    assert dec[0]["decision"] == "exit" and dec[0]["price"] == 3580.0


def test_on_init_adopt_falls_back_when_avgpx_unavailable():
    """avgpx 与最新价都拿不到时不硬收养 (留待运行时对账), 不产生负止损"""
    stra = _mk_init_stra()
    ctx = _InitCtx(pos=-116, avgpx=0.0)
    ctx.stra_get_price = lambda code: 0.0
    stra.on_init(ctx)
    assert stra._engine.direction == 0
    assert any(lv >= 3 and "adopt skipped" in t for lv, t in ctx.logs)
