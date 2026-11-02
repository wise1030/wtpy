"""运行时对账 (reconcile) 抗重启回归 (2026-11-02 生产事故根治).

事故: l/pp 引擎陈旧层账 (10-23 出场证据链断裂残留, 引擎 267/246 手
vs 信号/券商 137/89 手) 潜伏 7 个交易日未被运行时对账发现 —
RECONCILE 历史上零触发。根因: 生产每日双重启 (08:47 盘前 +
20:40 hotdaily) 使进程内 _recon_miss 归零, 而 reconcile 只在日评估
路径 (_post_calculate) 运行、每个重启窗口仅 1 次 → 失配永远无法
累计到 recon_patience=3。

修复 (StraTurtle):
① _recon_miss 随快照持久化 (v5 键 recon_miss, 旧快照缺省=0 兼容);
② on_init 恢复+收养后引擎净仓仍与信号仓不符 → 差异已跨重启存续,
   播种计数到 patience-1, 下一次日评估即重建。

口径: 重建=引擎收养成信号仓单层 (TruthMonitor.reconcile 既有语义),
出场在途/真值口径不在本测试范围。
"""
import json
import os
import sys

import numpy as np  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
sys.path.insert(0, os.path.dirname(__file__))

from test_stra_trigger import TrigCtx  # noqa: E402
from test_intrabar import _fill_daily  # noqa: E402

from schemas import InstrumentConfig, TurtleBaseConfig  # noqa: E402
from StraTurtle import StraTurtle  # noqa: E402


def _make():
    ctx = TrigCtx()
    _fill_daily(ctx.add_store("d1"), n=120)
    stra = StraTurtle("tf", InstrumentConfig(code="SHFE.rb.HOT", units=1),
                      TurtleBaseConfig())
    return stra, ctx


def _desync(stra, ctx, eng_lots=130, fw_pos=0):
    """制造脱管: 引擎持 eng_lots, 框架/券商 fw_pos"""
    e = stra._subs[0]["engine"]
    if eng_lots:
        e.confirm_enter(1, eng_lots, 9151.0, n=236.0)
    ctx.pos = fw_pos


def _chan():
    return {"S1": {"entry_upper": 1e9, "entry_lower": 0, "exit_upper": 1e9,
                   "exit_lower": 0, "prev_exit_upper": 1e9,
                   "prev_exit_lower": 0, "fs_upper": 1e9, "fs_lower": 0,
                   "prev_fs_upper": 1e9, "prev_fs_lower": 0,
                   "current_exit_upper": 1e9, "current_exit_lower": 0}}


def _eval_once(stra, ctx):
    closes = np.array(ctx.klines["d1"].data["close"], dtype=float)
    stra._post_calculate(ctx, closes, 236.0, _chan(), mark=False)


def test_reconcile_fires_after_persistence_when_mismatch():
    """基础语义: 连续失配达 patience → 引擎按信号仓重建 (既有行为)"""
    stra, ctx = _make()
    stra.on_init(ctx)
    _desync(stra, ctx)
    for _ in range(int(stra._base.recon_patience)):
        _eval_once(stra, ctx)
    assert stra._subs[0]["engine"].direction == 0
    assert stra._recon_miss == 0


def test_recon_miss_survives_restart_via_snapshot():
    """根治①: 失配计数随快照跨重启持久, 不再每日归零"""
    stra, ctx = _make()
    stra.on_init(ctx)
    _desync(stra, ctx)
    _eval_once(stra, ctx)           # miss=1
    _eval_once(stra, ctx)           # miss=2 (未达 patience, 未重建)
    assert stra._recon_miss == 2
    assert stra._subs[0]["engine"].direction == 1
    snap = json.loads(ctx.kv["tf_engine"])
    assert snap.get("recon_miss") == 2

    # === 模拟重启: 新实例恢复快照 ===
    ctx2 = TrigCtx()
    ctx2.kv["tf_engine"] = ctx.kv["tf_engine"]
    _fill_daily(ctx2.add_store("d1"), n=120)
    ctx2.pos = 0
    stra2 = StraTurtle("tf", InstrumentConfig(code="SHFE.rb.HOT", units=1),
                       TurtleBaseConfig())
    stra2.on_init(ctx2)
    assert stra2._recon_miss == 2     # 计数未被重启归零
    _eval_once(stra2, ctx2)           # miss=3 → 重建
    assert stra2._subs[0]["engine"].direction == 0


def test_boot_mismatch_seeds_reconcile_counter():
    """根治②: 恢复点引擎净仓≠信号仓(差异已跨重启存续) → 播种
    到 patience-1, 下一次日评估即重建 (无需再等满 patience 次)"""
    stra, ctx = _make()
    stra.on_init(ctx)
    _desync(stra, ctx)
    stra._persist(ctx)              # 落带层账的快照
    snap = json.loads(ctx.kv["tf_engine"])
    ctx2 = TrigCtx()
    ctx2.kv["tf_engine"] = json.dumps(snap)
    _fill_daily(ctx2.add_store("d1"), n=120)
    ctx2.pos = 0                    # 信号仓与快照引擎层账不符
    stra2 = StraTurtle("tf", InstrumentConfig(code="SHFE.rb.HOT", units=1),
                       TurtleBaseConfig())
    stra2.on_init(ctx2)
    assert stra2._recon_miss == max(
        0, int(stra2._base.recon_patience) - 1)
    assert any("engine/book mismatch" in t for _, t in ctx2.logs)
    _eval_once(stra2, ctx2)         # miss=patience → 重建
    assert stra2._subs[0]["engine"].direction == 0


def test_no_seed_when_consistent_at_boot():
    """一致状态启动不播种 (防误报): 计数保持 0"""
    stra, ctx = _make()
    stra.on_init(ctx)
    stra._persist(ctx)
    ctx2 = TrigCtx()
    ctx2.kv["tf_engine"] = ctx.kv["tf_engine"]
    _fill_daily(ctx2.add_store("d1"), n=120)
    stra2 = StraTurtle("tf", InstrumentConfig(code="SHFE.rb.HOT", units=1),
                       TurtleBaseConfig())
    stra2.on_init(ctx2)
    assert stra2._recon_miss == 0
    assert not any("engine/book mismatch" in t for _, t in ctx2.logs)
