"""F4 重放幂等回归测试 (DATA_OPS.md §0 F4)

C++ 事实: trader 进程重启后 WtDataReader 的 rt 游标归零, 当日已推送过的
bar 会整体重放。策略层必须幂等 —— 不重复下单、不重复评估、状态不漂移。

幂等锚点 (全部入快照 v4):
- armed/pending: 入场触发后同向入场位已撤, 重放触碰不再产生决策
- _d1_date: 信号周期日期去重, 重放的日线评估/session_end 跳过
- _m5_key: 盘中段键值去重, 重放的 m5 bar 不参与触碰仿真
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "helpers"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from test_intrabar import _fill_daily  # noqa: E402
from test_stra_trigger import TrigCtx, _make_trig  # noqa: E402


def _setup(n=90, intrabar=False):
    stra = _make_trig(intrabar=intrabar)
    ctx = TrigCtx()
    d1 = ctx.add_store("d1")
    _fill_daily(d1, n)
    stra.on_init(ctx)
    return stra, ctx, d1


def _restart(ctx, intrabar=False):
    """模拟进程重启: 同名新实例从 kv 快照恢复"""
    stra2 = _make_trig(intrabar=intrabar)
    stra2.on_init(ctx)
    return stra2


# ------------------------------------------------------------------
# 日线路径: 重启后重放当日 bar
# ------------------------------------------------------------------
def test_restart_replay_signal_bar_no_duplicate_order():
    stra, ctx, d1 = _setup()
    stra.on_calculate(ctx)                        # 评估+武装
    lv = stra._engine.armed["long"]

    d1.append(20230090, 1500, lv + 1, lv + 20, lv + 1, lv + 15)
    stra.on_calculate(ctx)                        # 触碰 → 入场
    n_orders = len(ctx.orders)
    assert any(o[1] > 0 for o in ctx.orders)
    assert stra._engine.direction == 1

    # --- 重启 + 重放同一根信号 bar ---
    stra2 = _restart(ctx)
    assert stra2._engine.direction == 1           # 持仓恢复
    assert stra2._d1_date == 20230090             # 去重键恢复

    stra2.on_calculate(ctx)                       # 重放: 同 bar 再到
    assert len(ctx.orders) == n_orders            # 无重复订单
    assert stra2._engine.direction == 1
    assert stra2._engine.armed["stop"] is not None

    # 重放 tick: 入场位已撤 → 价格介于止损/加仓之间不再产生任何决策
    e2 = stra2._engine
    assert e2.armed["long"] is None               # 幂等锚点: 入场位已撤
    ctx.px = e2.armed["stop"] + 1                 # stop < px < add
    stra2.on_tick(ctx, "SHFE.rb.HOT", None)
    assert len(ctx.orders) == n_orders


def test_restart_replay_session_end_dedup():
    stra, ctx, d1 = _setup()
    stra.on_calculate(ctx)
    d1.append(20230090, 1500, 3395, 3398, 3393, 3396)
    stra.on_calculate(ctx)                        # 日线评估, _d1_date=20230090
    snap_before = ctx.kv["tb_engine"]

    stra2 = _restart(ctx)
    m5 = ctx.add_store("m5")
    m5.append(20230090, 1500, 3395, 3398, 3393, 3396)
    stra2.on_session_end(ctx, 20230090)           # 重放的会话收尾
    assert ctx.kv["tb_engine"] == snap_before     # 快照逐字节一致


# ------------------------------------------------------------------
# m5 盘中路径: 重启后 rt 当日段整体重放
# ------------------------------------------------------------------
def test_restart_replay_m5_segment_no_duplicate(tmp_path=None):
    stra, ctx, d1 = _setup(intrabar=True)
    m5 = ctx.add_store("m5")
    m5.append(20230090, 905, 3400, 3410, 3398, 3405)
    m5.append(20230090, 910, 3405, 3412, 3403, 3408)
    stra.on_calculate(ctx)                        # 建基线 (不回放历史)

    d1.append(20230090, 1500, 3408, 3412, 3390, 3395)
    m5.append(20230090, 915, 3408, 3409, 3404, 3406)
    stra.on_calculate(ctx)                        # 日线评估 → 武装
    lv = stra._engine.armed["long"]
    assert lv is not None

    m5.append(20230090, 920, lv - 1, lv + 3, lv - 2, lv + 2)
    stra.on_calculate(ctx)                        # m5 触碰 → 入场
    n_orders = len(ctx.orders)
    assert any(o[1] > 0 for o in ctx.orders)

    # --- 重启 + rt 游标归零: 当日 m5 段整体重放 ---
    stra2 = _restart(ctx, intrabar=True)
    assert stra2._m5_key == stra._m5_key          # 扫描键恢复
    m5.append(20230090, 905, 3400, 3410, 3398, 3405)   # 重放段
    m5.append(20230090, 910, 3405, 3412, 3403, 3408)
    m5.append(20230090, 915, 3408, 3409, 3404, 3406)
    m5.append(20230090, 920, lv - 1, lv + 3, lv - 2, lv + 2)
    stra2.on_calculate(ctx)
    assert len(ctx.orders) == n_orders            # 重放段全部跳过
    assert stra2._engine.direction == 1

    # 状态与重放前逐字节一致 (幂等)
    assert json.loads(ctx.kv["tb_engine"])["subs"]["S1"]["armed"] == \
        stra._engine.snapshot()["armed"]
