"""trigger 模式策略层测试 (方案C落地)

口径:
- bt 日线粒度: 当根 bar 到达 → check_bar 触碰仿真(按昨武装位) → 日线评估武装次日
- 实盘: on_tick 驱动触发; session_end 用当日 m5 聚合合成日K评估武装
- 过滤器在武装时(日评估)前置裁决; 风控在触发时复核; 否决→当日撤武装
- bt 填充模型与 close 模式一致 (信号bar收盘), 仅信号语义不同 (触碰vs收盘确认)
"""
import json
import os
import sys

import numpy as np  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "helpers"))
sys.path.insert(0, os.path.dirname(__file__))

from test_intrabar import MultiCtx, _fill_daily  # noqa: E402

from schemas import InstrumentConfig, TurtleBaseConfig  # noqa: E402
from StraTurtle import StraTurtle  # noqa: E402


class TrigCtx(MultiCtx):
    """+ tick 价格源 / 订阅记录 / 无 m5 时的安全 avgpx"""

    def __init__(self):
        super().__init__()
        self.px = 0.0
        self.subscribed = []
        self.tdate = None           # 实盘交易日; None=bt 口径不触发自愈
        self.is_backtest = False

    def stra_get_tdate(self):
        return self.tdate

    def stra_get_price(self, code):
        return self.px

    def stra_sub_ticks(self, code):
        self.subscribed.append(code)

    def stra_set_position(self, code, target, tag=""):
        old = self.pos
        self.orders.append((code, target, tag))
        self.pos = target
        if target == 0:
            self.avgpx = 0.0
            return
        added = abs(target) - abs(old)
        if added <= 0:
            return
        px = self.px or float(self.klines["d1"].data["close"][-1])
        if old == 0:
            self.avgpx = px
        else:
            self.avgpx = (self.avgpx * abs(old) + px * added) / abs(target)


def _make_trig(direction="both", units=2, intrabar=False,
               unit_var=200000.0):
    inst = InstrumentConfig(code="SHFE.rb.HOT", period="d1", system="S1",
                            units=units, intrabar_stop=intrabar,
                            direction=direction)
    base = TurtleBaseConfig(entry_mode="trigger", unit_var=unit_var)
    return StraTurtle("tb", inst, base)


def _setup(n=90):
    stra = _make_trig()
    ctx = TrigCtx()
    d1 = ctx.add_store("d1")
    _fill_daily(d1, n)
    stra.on_init(ctx)
    return stra, ctx, d1


# ------------------------------------------------------------------
# bt 日线粒度流程
# ------------------------------------------------------------------
def test_bt_daily_arm_then_touch_enter_and_stop_exit():
    stra, ctx, d1 = _setup()
    stra.on_calculate(ctx)                     # 首根评估 → 武装
    assert ctx.orders == []                    # 武装不产生订单
    e = stra._engine
    assert e.armed["long"] is not None
    lv = e.armed["long"]
    assert 3400 < lv < 3420                    # 20日通道包络

    # 次日: 高开过位 (跳空 → 开盘价触发)
    d1.append(20230090, 1500, lv + 1, lv + 20, lv + 1, lv + 15)
    stra.on_calculate(ctx)
    assert any(o[1] > 0 for o in ctx.orders), "突破应入场"
    assert e.direction == 1
    assert e.armed["stop"] is not None         # 入场后立即武装止损

    # 次日: 低点击穿止损 → 离场
    d1.append(20230091, 1500, lv + 10, lv + 12, 3000.0, 3010.0)
    stra.on_calculate(ctx)
    assert ctx.orders[-1][1] == 0
    assert e.direction == 0


def test_bt_daily_no_touch_no_order():
    stra, ctx, d1 = _setup()
    stra.on_calculate(ctx)
    d1.append(20230090, 1500, 3395, 3398, 3393, 3396)   # 通道内波动
    stra.on_calculate(ctx)
    assert ctx.orders == []


def test_bt_session_end_dedup_with_daily_arrival():
    """bt: d1到达已评估 → session_end 同日期合成评估被去重"""
    stra, ctx, d1 = _setup()
    stra.on_calculate(ctx)
    d1.append(20230090, 1500, 3395, 3398, 3393, 3396)
    stra.on_calculate(ctx)
    n_orders = len(ctx.orders)
    stra.on_session_end(ctx, 20230090)         # 同日期 → 跳过
    assert len(ctx.orders) == n_orders


# ------------------------------------------------------------------
# 实盘 tick 路径
# ------------------------------------------------------------------
def test_tick_trigger_enter_live_path():
    stra, ctx, d1 = _setup()
    assert ctx.subscribed == ["SHFE.rb.HOT"]   # trigger 模式订阅 tick
    stra.on_calculate(ctx)                     # 武装
    lv = stra._engine.armed["long"]
    ctx.px = lv - 1
    stra.on_tick(ctx, "SHFE.rb.HOT", None)
    assert ctx.orders == []                    # 未触及
    ctx.px = lv
    stra.on_tick(ctx, "SHFE.rb.HOT", None)
    assert any(o[1] > 0 for o in ctx.orders)   # 触及即入场
    # 触发后快照已持久化 (v4 含 armed)
    blob = json.loads(ctx.kv["tb_engine"])
    assert blob["v"] == 4
    assert blob["subs"]["S1"]["armed"]["stop"] is not None


def test_filter_gate_at_arm_disarms_direction():
    """方向闸在武装时裁决: long-only 配置 → 空头入场位被解除"""
    stra = _make_trig(direction="long")
    ctx = TrigCtx()
    d1 = ctx.add_store("d1")
    _fill_daily(d1)
    stra.on_init(ctx)
    stra.on_calculate(ctx)
    e = stra._engine
    assert e.armed["long"] is not None
    assert e.armed["short"] is None            # 过滤链否决 → 未武装
    ctx.px = e._entry_ch[1] - 1                # 击穿空头位也不触发
    stra.on_tick(ctx, "SHFE.rb.HOT", None)
    assert ctx.orders == []


def test_risk_veto_at_trigger_disarms_for_session():
    """触发时风控否决 → reject_pending: 当日不再触发, 次日重武装"""
    stra = _make_trig(unit_var=1.0)            # 预算极小 → lots=0
    ctx = TrigCtx()
    d1 = ctx.add_store("d1")
    _fill_daily(d1)
    stra.on_init(ctx)
    stra.on_calculate(ctx)
    e = stra._engine
    lv = e.armed["long"]
    ctx.px = lv
    stra.on_tick(ctx, "SHFE.rb.HOT", None)     # 触发但被手数闸否决
    assert ctx.orders == []
    assert e._pending is None
    assert e.armed["long"] is None             # 当日撤武装
    ctx.px = lv + 5
    stra.on_tick(ctx, "SHFE.rb.HOT", None)
    assert ctx.orders == []                    # 不再重复触发
    stra.on_calculate(ctx)                     # 次日评估 (同bar, 幂等重武装)
    assert e.armed["long"] is not None


# ------------------------------------------------------------------
# session_end 合成日K评估 (实盘路径)
# ------------------------------------------------------------------
def test_session_end_synth_daily_eval():
    stra, ctx, d1 = _setup()
    # 当日 m5 三根 (date=交易日)
    m5 = ctx.add_store("m5")
    m5.append(20230090, 905, 3400, 3410, 3398, 3405)
    m5.append(20230090, 910, 3405, 3412, 3403, 3408)
    m5.append(20230090, 1500, 3408, 3409, 3390, 3395)
    stra.on_session_end(ctx, 20230090)
    e = stra._engine
    assert stra._d1_date == 20230090
    assert e.armed["long"] is not None
    # 合成日K: o=3400 h=3412 l=3390 c=3395 → 通道含当根包络
    assert e._entry_ch[0] >= 3412.0
    # 幂等: 同日期再次调用不重评估
    snap1 = ctx.kv["tb_engine"]
    stra.on_session_end(ctx, 20230090)
    assert ctx.kv["tb_engine"] == snap1


def test_snapshot_v4_armed_roundtrip():
    stra, ctx, d1 = _setup()
    stra.on_calculate(ctx)
    stra2 = _make_trig()
    stra2.on_init(ctx)                         # 同 kv 恢复
    e2 = stra2._engine
    assert e2.armed["long"] == stra._engine.armed["long"]
    assert e2._n == stra._engine._n


# ------------------------------------------------------------------
# 实盘自愈评估 (session_end 缺失/盘后启动兜底)
# ------------------------------------------------------------------
def test_catchup_eval_on_first_tick_of_new_tdate():
    """实盘新交易日首 tick: his 末根未评估 → 立即评估武装"""
    stra, ctx, d1 = _setup()
    assert stra._d1_date is None               # 未评估 (如盘后启动)
    assert stra._engine.armed["long"] is None
    ctx.tdate = 20230090                       # 夜盘 tick → 新交易日
    ctx.px = 3395.0                            # 通道内, 不触发入场
    stra.on_tick(ctx, "SHFE.rb.HOT", None)
    e = stra._engine
    assert stra._d1_date == 20230089           # his 末根已评估
    assert e.armed["long"] is not None         # 已武装
    assert ctx.orders == []                    # 评估只武装不下单


def test_catchup_eval_idempotent_same_day():
    """同日重复 tick: _catchup_date 闸 + _d1_date 已最新 → 不重复评估"""
    stra, ctx, d1 = _setup()
    ctx.tdate = 20230090
    ctx.px = 3395.0
    stra.on_tick(ctx, "SHFE.rb.HOT", None)
    snap1 = ctx.kv["tb_engine"]
    stra.on_tick(ctx, "SHFE.rb.HOT", None)     # 同日再来
    assert ctx.kv["tb_engine"] == snap1
    # 下一交易日: his 末根未变 (仍 20230089) → _d1_date 已最新, 不评估
    ctx.tdate = 20230091
    stra.on_tick(ctx, "SHFE.rb.HOT", None)
    assert stra._d1_date == 20230089


def test_catchup_eval_skipped_in_bt():
    """bt 语义保护: is_backtest=True 时不自愈 (bt 走 on_bar 闭合评估)"""
    stra, ctx, d1 = _setup()
    ctx.is_backtest = True
    ctx.tdate = 20230090
    ctx.px = 3395.0
    stra.on_tick(ctx, "SHFE.rb.HOT", None)
    assert stra._d1_date is None
    assert stra._engine.armed["long"] is None


def test_catchup_eval_skipped_when_already_evaluated():
    """session_end 正常评估过的场景: 次日 tick 不重复评估"""
    stra, ctx, d1 = _setup()
    stra.on_calculate(ctx)                     # bt 口径评估 → _d1_date=末根
    assert stra._d1_date == 20230089
    ctx.tdate = 20230090
    ctx.px = 3395.0
    snap1 = ctx.kv["tb_engine"]
    stra.on_tick(ctx, "SHFE.rb.HOT", None)
    assert stra._d1_date == 20230089
    assert ctx.kv["tb_engine"] == snap1        # 快照无变化=未重复评估


# ------------------------------------------------------------------
# 成交均价回写 (P1: confirm 时 fallback 触发价 → avgpx 更新后修正)
# ------------------------------------------------------------------
def _trigger_enter_with_pending_avgpx(stra, ctx):
    """tick 触发入场, 但 confirm 瞬间 avgpx 读不到 (模拟分批窗口未闭合)
    → 记账价 fallback 为触发价; 返回触发价"""
    stra.on_calculate(ctx)                       # 武装
    lv = stra._engine.armed["long"]
    orig = ctx.stra_get_position_avgpx
    ctx.stra_get_position_avgpx = lambda code: 0.0
    ctx.px = lv
    stra.on_tick(ctx, "SHFE.rb.HOT", None)       # 触发入场
    ctx.stra_get_position_avgpx = orig
    assert stra._engine.direction == 1
    assert stra._fill_pending is not None        # 回写标记已挂
    assert stra._engine.layers[-1][1] == lv      # 记账=触发价
    return lv


def test_fill_reanchor_on_next_tick():
    """分批窗口闭合后下一 tick: avgpx 反推实际均价, 层价与武装位跟随"""
    stra, ctx, d1 = _setup()
    lv = _trigger_enter_with_pending_avgpx(stra, ctx)
    e = stra._engine
    n = e._n
    old_stop = e.armed["stop"]
    assert abs(old_stop - (lv - 2.0 * n)) < 1e-9
    ctx.avgpx = lv - 3.0                         # 执行层回报实际均价
    ctx.px = lv - 1.0                            # 未触 stop/add 的任意价
    stra.on_tick(ctx, "SHFE.rb.HOT", None)       # 回写在触发检查前
    assert stra._fill_pending is None
    assert e.layers[-1][1] == lv - 3.0
    assert abs(e.armed["stop"] - (lv - 3.0 - 2.0 * n)) < 1e-9
    assert abs(e.armed["add"] - (lv - 3.0 + 0.5 * n)) < 1e-9
    assert e.s1_last["entry_price"] == lv - 3.0  # 首层同步输赢判定基准


def test_fill_reanchor_ignores_sub_tick_deviation():
    """偏差 <= 0.5 tick 视为同一价位, 不动 (防抖动)"""
    stra, ctx, d1 = _setup()
    lv = _trigger_enter_with_pending_avgpx(stra, ctx)
    e = stra._engine
    ctx.avgpx = lv - 0.4                         # pricetick=1.0 → 0.4<0.5
    ctx.px = lv - 1.0
    stra.on_tick(ctx, "SHFE.rb.HOT", None)
    assert stra._fill_pending is None            # 标记清除
    assert e.layers[-1][1] == lv                 # 未修正


def test_fill_reanchor_waits_for_partial_fill():
    """分批成交中 (框架持仓 < 目标净仓) 不回写, 持仓齐后下一 tick 修正"""
    stra, ctx, d1 = _setup()
    lv = _trigger_enter_with_pending_avgpx(stra, ctx)
    e = stra._engine
    target = int(ctx.pos)
    ctx.pos = target - 1                         # 模拟还差 1 手未成交
    ctx.avgpx = lv - 3.0
    ctx.px = lv - 1.0
    stra.on_tick(ctx, "SHFE.rb.HOT", None)
    assert stra._fill_pending is not None        # 等待中
    assert e.layers[-1][1] == lv                 # 未修正
    ctx.pos = target                             # 分批完成
    stra.on_tick(ctx, "SHFE.rb.HOT", None)
    assert stra._fill_pending is None
    assert e.layers[-1][1] == lv - 3.0


def test_fill_reanchor_add_layer_only_fixes_last():
    """加仓回写只修最后一层, 首层价格保持 (增量成本归因)"""
    from helpers.RiskManager import RiskManager
    from schemas import RiskConfig
    stra, ctx, d1 = _setup()
    stra._risk = RiskManager(RiskConfig(max_margin_usage=0.95))  # 留加仓空间
    lv = _trigger_enter_with_pending_avgpx(stra, ctx)
    e = stra._engine
    n = e._n
    ctx.avgpx = lv                               # 首层实际价=触发价, 回写无变化
    ctx.px = lv - 1.0
    stra.on_tick(ctx, "SHFE.rb.HOT", None)
    assert stra._fill_pending is None
    lots1 = e.layers[0][0]
    # 触发加仓: confirm 时 avgpx 读不到 → fallback
    ctx.px = e.armed["add"]
    add_px = ctx.px
    orig = ctx.stra_get_position_avgpx
    ctx.stra_get_position_avgpx = lambda code: 0.0
    stra.on_tick(ctx, "SHFE.rb.HOT", None)
    ctx.stra_get_position_avgpx = orig
    assert len(e.layers) == 2
    assert e.layers[-1][1] == add_px             # 记账=触发价
    # 执行层回报: 加仓实际均价高于触发价 2 元 → 整体均价上移
    tot = lots1 + e.layers[1][0]
    ctx.avgpx = (lv * lots1 + (add_px + 2.0) * e.layers[1][0]) / tot
    ctx.px = add_px + 1.0
    stra.on_tick(ctx, "SHFE.rb.HOT", None)
    assert e.layers[0][1] == lv                  # 首层不动
    assert abs(e.layers[-1][1] - (add_px + 2.0)) < 1e-9   # 仅最后一层修正
    assert abs(e.armed["stop"] - (add_px + 2.0 - 2.0 * n)) < 1e-9


def test_catchup_eval_reanchors_stale_fill():
    """重启丢失 _fill_pending 后, catchup 日评估兜底修正记账价偏差
    (实盘 d1 主K不闭合, on_calculate 路径的换月检测永不执行,
    re-anchor 必须挂在日评估路径)"""
    stra, ctx, d1 = _setup()
    lv = _trigger_enter_with_pending_avgpx(stra, ctx)   # 入场, 记账=触发价
    e = stra._engine
    stra._fill_pending = None                            # 模拟重启丢失标记
    ctx.avgpx = lv - 3.0                                 # 框架实际均价
    ctx.tdate = 20230090                                 # 新交易日首 tick
    ctx.px = lv + 1.0                                    # stop<px<add 不触发任何信号
    stra.on_tick(ctx, "SHFE.rb.HOT", None)
    # his 末根已评估过 (_d1_date=20230089) → catchup 跳过, 不修正
    assert e.layers[-1][1] == lv
    # his 到达新 bar → 下一交易日 catchup 评估 → 漂移修正
    d1.append(20230090, 1500, 3395.0, 3397.0, 3393.0, 3396.0)
    ctx.tdate = 20230091
    stra.on_tick(ctx, "SHFE.rb.HOT", None)
    assert abs(e.layers[-1][1] - (lv - 3.0)) < 1e-9
    # 止损/加仓武装位随平移后的层价重算
    n = e._n
    assert abs(e.armed["stop"] - (lv - 3.0 - 2.0 * n)) < 1e-9


def test_session_end_reanchors_stale_fill():
    """session_end 日评估同样兜底 avgpx 漂移 (路径覆盖)"""
    stra, ctx, d1 = _setup()
    lv = _trigger_enter_with_pending_avgpx(stra, ctx)
    e = stra._engine
    stra._fill_pending = None
    ctx.avgpx = lv - 3.0
    stra._d1_date = None                      # 让 session_end 走评估分支
    stra.on_session_end(ctx, 20230089)
    assert abs(e.layers[-1][1] - (lv - 3.0)) < 1e-9


def test_restore_syncs_last_n_for_trigger():
    """快照恢复后 _last_n 同步: 重启~下次日评估窗口内触发不被丢弃
    (2026-09-04 夜盘实测 bug: sc 穿越 add 位无响应)"""
    stra, ctx, d1 = _setup()
    stra.on_calculate(ctx)                       # 武装 + 设置 _last_n
    lv = stra._engine.armed["long"]
    # 模拟重启: 新实例共享 kv (快照) 与 K线数据
    stra2 = _make_trig()
    ctx2 = TrigCtx()
    ctx2.kv.update(ctx.kv)
    ctx2.klines["d1"] = d1                       # 同一 his 数据视图
    stra2.on_init(ctx2)
    assert np.isfinite(stra2._last_n)            # 恢复同步, 非 NaN
    assert stra2._engine.armed["long"] == lv     # 武装位恢复
    ctx2.px = lv                                 # 触及入场位
    stra2.on_tick(ctx2, "SHFE.rb.HOT", None)
    assert any(o[1] > 0 for o in ctx2.orders)    # 触发执行, 未被丢弃


def test_restore_rebuilds_risk_book():
    """重启恢复持仓后组合风控账本同步重建 (否则持仓期间
    组合总敞口/单位数约束被低估, 2026-09-04 审计发现)"""
    stra, ctx, d1 = _setup()
    _trigger_enter_with_pending_avgpx(stra, ctx)       # 入场 1 层
    assert stra._risk.has_position("SHFE.rb.HOT")
    # 模拟重启: 新实例 + 新 RiskManager (空账本), 共享 kv/K线
    stra2 = _make_trig()
    ctx2 = TrigCtx()
    ctx2.kv.update(ctx.kv)
    ctx2.klines["d1"] = d1
    ctx2.pos = ctx.pos                                 # 框架持仓延续
    ctx2.avgpx = ctx.avgpx
    stra2.on_init(ctx2)
    assert stra2._engine.direction == 1                # 引擎持仓恢复
    assert stra2._risk.has_position("SHFE.rb.HOT")     # 账本重建
    e = stra2._risk._book["SHFE.rb.HOT"]
    assert e["lots"] == stra2._engine.layers[0][0]
    assert e["risk"] > 0                               # 2N 潜亏已记账


# ------------------------------------------------------------------
# P0-2: 关键事件告警接入
# ------------------------------------------------------------------
class _RecNotifier:
    def __init__(self):
        self.events = []

    def send(self, key, text):
        self.events.append((key, text))
        return True


def test_veto_alerts_on_first_occurrence():
    """风控否决首见 → webhook 告警 (同类去重, 复用 veto key)"""
    stra = _make_trig(unit_var=1.0)            # 预算极小 → lots=0 否决
    nt = _RecNotifier()
    stra._notify = nt
    ctx = TrigCtx()
    d1 = ctx.add_store("d1")
    _fill_daily(d1)
    stra.on_init(ctx)
    stra.on_calculate(ctx)
    ctx.px = stra._engine.armed["long"]
    stra.on_tick(ctx, "SHFE.rb.HOT", None)     # 触发 → lots=0 否决
    assert any("veto_" in k for k, _ in nt.events)
    n1 = len(nt.events)
    stra.on_tick(ctx, "SHFE.rb.HOT", None)     # 重复否决不再告警
    assert len(nt.events) == n1


def test_fill_timeout_alerts_once():
    """分批窗口 >10min 未闭合 → fill_timeout 告警一次 (执行单元
    放弃/涨跌停无法成交的可观测代理)"""
    stra, ctx, d1 = _setup()
    nt = _RecNotifier()
    stra._notify = nt
    lv = _trigger_enter_with_pending_avgpx(stra, ctx)
    ctx.pos = ctx.pos - 1                      # 模拟 1 手始终未成交
    stra._fill_pending["ts"] -= 700            # 回拨 700s 越过超时线
    ctx.avgpx = lv
    ctx.px = lv + 1.0
    stra.on_tick(ctx, "SHFE.rb.HOT", None)
    assert any("fill_timeout" in k for k, _ in nt.events)
    n1 = len(nt.events)
    stra.on_tick(ctx, "SHFE.rb.HOT", None)     # 不重复告警
    assert len(nt.events) == n1
    assert stra._fill_pending is not None      # 继续等待成交


# ------------------------------------------------------------------
# 孤儿 pending 自愈 (2026-09-07 事故: 决策丢失→pending 经快照
# 持久化/恢复永久挂起, on_price/check_bar pending 闸吞掉后续
# 全部触发, sc 盘中 709 穿越 add 位 688.32 无响应)
# ------------------------------------------------------------------
def _restart(stra_old, ctx_old):
    """模拟进程重启: 持久化快照 → 新实例同 kv/持仓 恢复"""
    stra_old._persist(ctx_old)
    stra2 = _make_trig()
    ctx2 = TrigCtx()
    ctx2.kv = ctx_old.kv            # 共享持久化存储
    ctx2.pos = ctx_old.pos          # 券商实际持仓延续
    ctx2.avgpx = ctx_old.avgpx
    d1b = ctx2.add_store("d1")
    _fill_daily(d1b)
    stra2.on_init(ctx2)
    return stra2, ctx2


def test_orphan_pending_dropped_on_restart():
    """重启恢复: 快照里的 pending 必须清除 (武装位保留),
    价格仍在位上方时下一 tick 立即补票触发"""
    stra, ctx, d1 = _setup()
    _trigger_enter_with_pending_avgpx(stra, ctx)
    assert len(stra._engine.layers) == 1
    # 模拟决策丢失现场: 引擎 pending=add 挂起 (决策未下单未确认)
    stra._engine._pending = {"kind": "add", "dir": 1}
    stra2, ctx2 = _restart(stra, ctx)
    e2 = stra2._engine
    assert e2._pending is None                    # 孤儿 pending 已清
    assert e2.armed["add"] is not None            # 武装位保留
    assert any("drop orphan pending" in t for _, t in ctx2.logs)
    assert ctx2.pos != 0                            # 持仓经对账延续
    # 补票: 价格仍在 add 位上方 → 下一 tick 重新触发; 本 mock 场景
    # 首仓 733 手已打满预算 → 风控否决+reject (关键: 决策到达风控
    # 层被消费, 不再被 pending 闸静默吞掉)
    ctx2.px = e2.armed["add"] + 1.0
    stra2.on_tick(ctx2, "SHFE.rb.HOT", None)
    assert any("veto add" in t for _, t in ctx2.logs)
    assert e2._pending is None                      # reject 清挂起
    assert e2.armed["add"] is None                  # 当日撤武装防刷屏


def test_stale_pending_dropped_on_daily_eval():
    """日评估自愈: pending 挂起跨日 → bar_eval 评估前清除,
    新一轮武装位可正常触发"""
    stra, ctx, d1 = _setup()
    _trigger_enter_with_pending_avgpx(stra, ctx)
    stra._engine._pending = {"kind": "add", "dir": 1}
    d1.append(20230091, 1500, 3395, 3398, 3393, 3396)   # 新日K → bar_eval
    stra.on_calculate(ctx)
    assert stra._engine._pending is None
    assert any("drop stale pending" in t for _, t in ctx.logs)
    # 清除后触发链恢复
    if stra._engine.armed["add"] is not None:
        ctx.px = stra._engine.armed["add"]
        stra.on_tick(ctx, "SHFE.rb.HOT", None)
        assert stra._engine._pending is None      # 决策被消费(confirm)


def test_add_veto_lot0_rejects_trigger():
    """DEC_ADD lots=0 否决必须 reject_pending (与 enter 分支对齐,
    修复前 pending 永久挂起吞掉后续触发)"""
    stra, ctx, d1 = _setup()
    _trigger_enter_with_pending_avgpx(stra, ctx)
    stra._sizer.layer_lots = lambda *a, **k: 0    # add 手数为 0
    ctx.px = stra._engine.armed["add"] + 1.0
    stra.on_tick(ctx, "SHFE.rb.HOT", None)
    assert stra._engine._pending is None          # reject 已清挂起
    assert stra._engine.armed["add"] is None      # 当日撤武装防刷屏
    assert any("veto add" in t for _, t in ctx.logs)
