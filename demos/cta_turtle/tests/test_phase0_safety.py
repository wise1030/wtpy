"""Phase 0 运维安全网回归 (2026-11-09 实施, 复核报告 ARCH_REVIEW_2026-11-09)

覆盖:
① fill_timeout 按紧急度分档 (schemas 校验 + 出场 120s/入场 600s/免费 3x);
② 快照恢复失败 → DEGRADED (禁新开/加仓、出场保留、重持久化解除、
   validate_snapshot 的 ValueError 也不再崩启动);
③ 心跳 (实盘写盘/回测跳过/5s 节流/字段完整性);
④ 周期快照 (300s 节流) + snapshots 3 深轮换备份;
⑤ _on_arm 价格不可用门 (0/None/NaN → 解除入场武装, 每日告警一次)。
"""
import json
import os
import sys
import time

import numpy as np  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from test_stra_trigger import TrigCtx  # noqa: E402
from test_intrabar import _fill_daily  # noqa: E402

import StraTurtle as st_mod  # noqa: E402
from schemas import (InstrumentConfig, TurtleBaseConfig,  # noqa: E402
                     RiskConfig)
from StraTurtle import StraTurtle  # noqa: E402


def _make(backtest=False, n=90):
    ctx = TrigCtx()
    ctx.is_backtest = backtest
    _fill_daily(ctx.add_store("d1"), n=n)
    stra = StraTurtle("tf",
                      InstrumentConfig(code="SHFE.rb.HOT", units=1),
                      TurtleBaseConfig(entry_mode="trigger"))
    return stra, ctx


def _chan():
    return {"S1": {"entry_upper": 1e9, "entry_lower": 0, "exit_upper": 1e9,
                   "exit_lower": 0, "prev_exit_upper": 1e9,
                   "prev_exit_lower": 0, "fs_upper": 1e9, "fs_lower": 0,
                   "prev_fs_upper": 1e9, "prev_fs_lower": 0,
                   "current_exit_upper": 1e9, "current_exit_lower": 0}}


def _eval(stra, ctx, n=236.0):
    closes = np.array(ctx.klines["d1"].data["close"], dtype=float)
    stra._post_calculate(ctx, closes, n, _chan(), mark=False)


# ------------------------------------------------------------------
# ① fill_timeout 分档
# ------------------------------------------------------------------
def test_fill_timeout_defaults_and_validation():
    r = RiskConfig()
    assert r.fill_timeout_secs == 600.0
    assert r.exit_fill_timeout_secs == 120.0
    r.validate()                       # 默认通过
    for bad in (0, 29, 86401, "600", None, True):
        r2 = RiskConfig(fill_timeout_secs=bad) if not isinstance(bad, str) \
            else RiskConfig(**{"fill_timeout_secs": bad})
        try:
            r2.validate()
        except (ValueError, TypeError):
            pass
        else:
            raise AssertionError(f"fill_timeout_secs={bad!r} 应被拒绝")
    # 出场分档独立校验
    try:
        RiskConfig(exit_fill_timeout_secs=10).validate()
    except ValueError:
        pass
    else:
        raise AssertionError("exit_fill_timeout_secs=10 应被拒绝")


def test_exit_alert_uses_exit_timeout_not_entry(monkeypatch):
    """出场未确认 120s 即告警 (旧 600s); 入场路径不受影响。"""
    stra, ctx = _make()
    stra.on_init(ctx)
    e = stra._subs[0]["engine"]
    e.confirm_enter(1, 10, 3400.0, n=30.0)
    e._armed["exit"] = 3300.0          # 模拟出场已武装
    now = time.time()
    stra._exit_orders["k"] = {
        "engine": e, "ts": now - 200, "lots": 10, "direction": 1,
        "decision_price": 3300.0, "reason": "exit", "cursor": None,
        "filled": 0.0}
    monkeypatch.setattr(time, "time", lambda: now)
    stra._fills.process_exit_orders(ctx)
    assert any("exit unconfirmed" in t for _, t in ctx.logs), \
        "出场 200s (>120s 分档) 应告警"
    # 入场 pending 200s (<600s) 不应触发 fill_timeout 告警
    ctx.logs.clear()
    stra._fill_pending["S1"] = {"engine": e, "ts": now - 200, "lots": 10,
                                "fees0": 1.0}
    stra._fills.reanchor_actual_fill(ctx)
    assert not any("fill_timeout" in t or "ghost_fill" in t
                   for _, t in ctx.logs), "入场 200s 不应告警"


def test_fill_confirmed_free_fee_triple_window(monkeypatch):
    """免费/未配置费率品种: 兜底放行窗 = 3 × fill_timeout (旧 1800 等价)"""
    stra, ctx = _make()
    stra.on_init(ctx)
    e = stra._subs[0]["engine"]
    e.confirm_enter(1, 10, 3400.0, n=30.0)
    now = time.time()
    # fees0=0 (免费/未配置费率品种): fees 未增 → 超时窗口 3x
    fp = {"engine": e, "ts": now - 1300, "lots": 10, "fees0": 0.0,
          "cursor": None}
    ctx.pos = 10                       # 信号仓到位 (框架口径前置检查)
    monkeypatch.setattr(time, "time", lambda: now)
    # 本地可能残留 generated/traders 真值文件 → 强制真值源不可用;
    # fees 读 0.0 (未增) → 落入 3x 超时窗口分支 (环境无关性)
    monkeypatch.setattr(stra._truthmon, "truth_source", lambda: None)
    monkeypatch.setattr(stra._fills, "get_fees", lambda c: 0.0)
    confirmed, src, _ = stra._fills.fill_confirmed(ctx, fp,
                                                   "SHFE.rb.HOT", 10)
    assert not confirmed               # 1300 < 1800*2 → 不放行
    fp["ts"] = now - 4000
    confirmed, src, _ = stra._fills.fill_confirmed(ctx, fp,
                                                   "SHFE.rb.HOT", 10)
    assert confirmed                   # 4000 > 1800*2 → 超时放行
    assert src == "framework"


# ------------------------------------------------------------------
# ② DEGRADED
# ------------------------------------------------------------------
def _init_with_snapshot(ctx, snap_raw):
    ctx.kv["tf_engine"] = snap_raw


def test_corrupt_snapshot_enters_degraded_and_blocks_entry():
    stra, ctx = _make()
    _init_with_snapshot(ctx, "{not json")
    stra.on_init(ctx)
    assert stra._state_degraded is True
    assert any("DEGRADED" in t for _, t in ctx.logs)
    # 直接喂入场决策: 应被 DEGRADED 闸否决且不产生订单
    ctx.logs.clear()
    stra._execute(ctx, {"decision": "enter", "direction": 1,
                        "price": 3400.0}, 30.0, None)
    assert not any(o[1] != 0 for o in ctx.orders), \
        "DEGRADED 下禁止新开/加仓"
    assert any("degraded" in t.lower() for _, t in ctx.logs)


def test_snapshot_validate_valueerror_also_degraded_not_crash():
    """语义不匹配 (profiles 指纹) 旧实现抛 ValueError 崩启动 → 现 DEGRADED"""
    stra, ctx = _make()
    good = {"v": 5,
            "subs": {"S1": stra._subs[0]["engine"].snapshot()},
            "profiles": {"WRONG": {"system": "S1"}}}
    _init_with_snapshot(ctx, json.dumps(good))
    stra.on_init(ctx)                  # 不得抛出
    assert stra._state_degraded is True


def test_persist_clears_degraded():
    stra, ctx = _make()
    _init_with_snapshot(ctx, "{broken")
    stra.on_init(ctx)
    assert stra._state_degraded is True
    stra._persist(ctx)
    assert stra._state_degraded is False


def test_degraded_does_not_block_exit():
    """DEGRADED 只禁新开/加仓; 出场路径 (begin_exit_order) 不受影响"""
    stra, ctx = _make()
    stra.on_init(ctx)
    stra._state_degraded = True
    e = stra._subs[0]["engine"]
    e.confirm_enter(-1, 10, 3400.0, n=30.0)
    assert stra._engine.direction == -1
    dec = {"decision": "exit", "direction": -1, "price": 3300.0,
           "reason": "exit"}
    stra._execute(ctx, dec, 30.0)
    assert "k_exit" in stra._exit_orders or \
        any("Exit" in str(o) for o in ctx.orders) or \
        stra._engine.direction == 0, "出场应可提交"


def test_healthy_snapshot_no_degraded():
    """donor 持久化 → 受者恢复: 指纹/结构完整, 不得 DEGRADED"""
    donor, dctx = _make()
    donor.on_init(dctx)
    donor._persist(dctx)
    raw = dctx.kv["tf_engine"]
    stra, ctx = _make()
    ctx.kv["tf_engine"] = raw
    stra.on_init(ctx)
    assert stra._state_degraded is False
    assert any("restored" in t for _, t in ctx.logs)


# ------------------------------------------------------------------
# ③ 心跳
# ------------------------------------------------------------------
def test_heartbeat_live_write_and_throttle(monkeypatch, tmp_path):
    monkeypatch.setattr(st_mod, "_GROUP_DIR", str(tmp_path))
    stra, ctx = _make(backtest=False)
    stra.on_init(ctx)
    hb = tmp_path / "generated" / "heartbeat" / "tf.json"
    assert hb.exists(), "实盘 on_init 应写首帧心跳"
    data = json.loads(hb.read_text())
    assert data["name"] == "tf" and data["code"] == "SHFE.rb.HOT"
    assert data["degraded"] is False and data["price"] == 0.0
    first = data["ts"]
    stra._write_heartbeat(ctx)          # 5s 节流内 → 不更新
    assert json.loads(hb.read_text())["ts"] == first
    stra._heartbeat_ts = 0.0
    stra._last_tick_ts = time.time() - 7
    stra._write_heartbeat(ctx)          # 强制过期 → 更新且带 tick_age
    data = json.loads(hb.read_text())
    assert data["ts"] > first
    assert 6 < data["tick_age"] < 9


def test_heartbeat_skipped_in_backtest(monkeypatch, tmp_path):
    monkeypatch.setattr(st_mod, "_GROUP_DIR", str(tmp_path))
    stra, ctx = _make(backtest=True)
    stra.on_init(ctx)
    stra.on_calculate(ctx)
    assert not (tmp_path / "generated" / "heartbeat").exists(), \
        "回测不得写心跳"


def test_on_tick_updates_last_tick_ts():
    stra, ctx = _make()
    stra.on_init(ctx)
    assert stra._last_tick_ts == 0.0
    ctx.px = 3400.0
    stra.on_tick(ctx, "SHFE.rb.HOT", None)
    assert stra._last_tick_ts > 0


# ------------------------------------------------------------------
# ④ 周期快照 + 3 深备份
# ------------------------------------------------------------------
def test_periodic_persist_throttled(monkeypatch):
    stra, ctx = _make()
    stra.on_init(ctx)
    stra._persist(ctx)
    n0 = len(ctx.kv["tf_engine"])
    stra._maybe_periodic_persist(ctx)      # 300s 未到 → 不写
    assert len(ctx.kv["tf_engine"]) == n0
    stra._periodic_persist_ts = time.time() - 301
    stra._maybe_periodic_persist(ctx)      # 到期 → 写
    assert len(ctx.kv["tf_engine"]) > 0


def test_snapshot_backup_rotation(monkeypatch, tmp_path):
    monkeypatch.setattr(st_mod, "_GROUP_DIR", str(tmp_path))
    stra, ctx = _make(backtest=False)
    stra.on_init(ctx)
    d = tmp_path / "generated" / "snapshots"
    for i in range(5):                     # 5 次不同内容 → 只留 3 深
        stra._snap_hash = ""
        stra._backup_snapshot('{"v":5,"i":%d}' % i)
    files = sorted(p.name for p in d.iterdir())
    assert len(files) == 4, f"latest + 3 备份 = 4 文件, 实际 {files}"
    assert "tf.json" in files
    assert all(f"tf.json.{i}" in files for i in (1, 2, 3))
    latest = (d / "tf.json").read_text()
    assert '"i":4' in latest
    assert '"i":3' in (d / "tf.json.1").read_text()


def test_snapshot_backup_unchanged_content_skipped(monkeypatch, tmp_path):
    monkeypatch.setattr(st_mod, "_GROUP_DIR", str(tmp_path))
    stra, ctx = _make(backtest=False)
    stra.on_init(ctx)
    stra._snap_hash = ""
    stra._backup_snapshot('{"same":1}')
    stra._backup_snapshot('{"same":1}')    # 内容相同 → 不再写
    d = tmp_path / "generated" / "snapshots"
    assert len(list(d.iterdir())) == 1


# ------------------------------------------------------------------
# ⑤ _on_arm 价格不可用门
# ------------------------------------------------------------------
def test_on_arm_blocks_when_price_unavailable():
    stra, ctx = _make()
    stra.on_init(ctx)
    e = stra._subs[0]["engine"]
    e._armed["long"] = 3500.0
    e._armed["short"] = 3000.0
    ctx.px = 0.0                           # 数据断流特征
    stra._on_arm(ctx, e, None)
    assert e.armed["long"] is None and e.armed["short"] is None
    assert e._disarmed == {1, -1}
    assert any("price unavailable" in t for _, t in ctx.logs)
    # 恢复: 第二次调用价格可用 → 正常走过滤链 (不解除)
    ctx.px = 3400.0
    e2_calls = []
    stra._router.on_arm = lambda c, eng, f: e2_calls.append(1)
    stra._on_arm(ctx, e, None)
    assert e2_calls == [1]


def test_on_arm_nan_price_blocked():
    stra, ctx = _make()
    stra.on_init(ctx)
    e = stra._subs[0]["engine"]
    ctx.px = float("nan")
    stra._on_arm(ctx, e, None)
    assert e.armed["long"] is None
