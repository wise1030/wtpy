"""P0-3 交易通道真值源测试

覆盖: rtdata.json 解析/前缀匹配/多合约汇总/资金提取/陈旧判定/缓存,
以及 StraTurtle 侧三个接入点 (on_init 收养 / 运行时对账 / 成交确认)。
"""
from conftest import *  # noqa: F401,F403
import json
import os
import time

from helpers.LiveTruth import (ChannelTruth, resolve_truth_path, load_rtdata,
                               net_position, account_funds, trader_ids,
                               TradeJournal)
from schemas import InstrumentConfig, TurtleBaseConfig, RiskConfig
from helpers.RiskManager import RiskManager
from StraTurtle import StraTurtle


SAMPLE = {
    "positions": [
        {"code": "SHFE.rb.2701",
         "long": {"newvol": 40.0, "newavail": 40.0,
                  "prevol": 100.0, "preavail": 100.0},
         "short": {"newvol": 0.0, "newavail": 0.0,
                   "prevol": 0.0, "preavail": 0.0}},
        # 换月窗口: 同品种两个月份合约 → 必须汇总
        {"code": "SHFE.rb.2610",
         "long": {"newvol": 0.0, "newavail": 0.0,
                  "prevol": 20.0, "preavail": 20.0},
         "short": {"newvol": 5.0, "newavail": 0.0,
                   "prevol": 0.0, "preavail": 0.0}},
        {"code": "INE.sc.2610",
         "long": {"newvol": 0.0, "newavail": 0.0,
                  "prevol": 0.0, "preavail": 0.0},
         "short": {"newvol": 0.0, "newavail": 0.0,
                   "prevol": 0.0, "preavail": 0.0}},
        {"code": "DCE.i.2701",
         "long": {"newvol": 0.0, "newavail": 0.0,
                  "prevol": 0.0, "preavail": 0.0},
         "short": {"newvol": 3.0, "newavail": 0.0,
                   "prevol": 12.0, "preavail": 12.0}},
    ],
    "funds": {"CNY": {"balance": 23364117.3, "available": 4370706.19,
                      "margin": 18294446.3, "fee": 515.84}},
}


def _write(tmp_path, data=None, age: float = 0.0) -> str:
    p = os.path.join(str(tmp_path), "rtdata.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(SAMPLE if data is None else data, f)
    if age:
        t = time.time() - age
        os.utime(p, (t, t))
    return p


# ------------------------------------------------------------- 纯函数

def test_net_position_sums_hot_roll_window():
    """HOT 换月窗口两个月份合约 → 汇总 (多-空)"""
    assert net_position(SAMPLE, "SHFE.rb.HOT") == 155.0   # 40+100+20-5
    assert net_position(SAMPLE, "DCE.i.HOT") == -15.0     # 空 3+12
    assert net_position(SAMPLE, "INE.sc.HOT") == 0.0


def test_net_position_unknown_or_malformed():
    """未匹配品种/异常入参 → 0.0, 不抛异常"""
    assert net_position(SAMPLE, "CZCE.MA.HOT") == 0.0
    assert net_position(SAMPLE, "SHFE.cu.HOT") == 0.0
    # 两段式代码按品种前缀匹配 (容错: 调用方可能只给 EXCHG.product)
    assert net_position(SAMPLE, "SHFE.rb") == 155.0
    assert net_position(SAMPLE, "rb") == 0.0              # 段数不足
    assert net_position({}, "SHFE.rb.HOT") == 0.0
    assert net_position({"positions": ["x", None]}, "SHFE.rb.HOT") == 0.0
    assert net_position({"positions": [{"code": "SHFE.rb.2701",
                                        "long": {"prevol": "bad"}}]},
                        "SHFE.rb.HOT") == 0.0


def test_account_funds_and_available():
    assert account_funds(SAMPLE)["available"] == 4370706.19
    assert account_funds({"funds": {"USD": {"available": 1.0}}}) \
        ["available"] == 1.0
    assert account_funds({}) == {}
    assert account_funds({"funds": "x"}) == {}


def test_load_rtdata_missing_or_corrupt(tmp_path):
    assert load_rtdata(os.path.join(str(tmp_path), "none.json")) == {}
    bad = os.path.join(str(tmp_path), "bad.json")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("{not json")
    assert load_rtdata(bad) == {}


def test_trader_ids_and_resolve(tmp_path):
    cfg = os.path.join(str(tmp_path), "tdtraders.yaml")
    with open(cfg, "w", encoding="utf-8") as f:
        f.write("traders:\n- id: simnow\n  active: true\n")
    assert trader_ids(cfg) == ["simnow"]
    assert trader_ids(os.path.join(str(tmp_path), "nope.yaml")) == []
    # 自动探测到 generated/traders/<id>/rtdata.json
    d = os.path.join(str(tmp_path), "generated", "traders", "simnow")
    os.makedirs(d)
    with open(os.path.join(d, "rtdata.json"), "w", encoding="utf-8") as f:
        json.dump(SAMPLE, f)
    got = resolve_truth_path(str(tmp_path), env={})
    assert got == os.path.join(d, "rtdata.json")
    # off/env 覆盖
    assert resolve_truth_path(str(tmp_path), enabled="off", env={}) == ""
    assert resolve_truth_path(str(tmp_path), env={"WT_TRUTH_JSON": got}) == got
    assert resolve_truth_path(str(tmp_path),
                              env={"WT_TRUTH_JSON": "/tmp/nope.json"}) == ""


# ------------------------------------------------------- ChannelTruth

def test_channel_truth_read_cache_and_refresh(tmp_path):
    p = _write(tmp_path)
    t = ChannelTruth(p, max_age=600)
    assert t.ok and t.net_position("SHFE.rb.HOT") == 155.0
    assert t.available() == 4370706.19
    assert not t.stale()
    # mtime+size 未变 → 复用缓存 (改内容同时改 mtime 才重读)
    t._data["positions"] = []
    assert t.net_position("SHFE.rb.HOT") == 0.0
    t.reload()
    assert t.net_position("SHFE.rb.HOT") == 155.0


def test_channel_truth_stale_and_missing(tmp_path):
    p = _write(tmp_path, age=7200)
    t = ChannelTruth(p, max_age=1800)
    assert t.ok and t.stale()                 # 陈旧仍可读 (仅动作闸门收紧)
    t2 = ChannelTruth(os.path.join(str(tmp_path), "none.json"))
    assert not t2.ok and t2.stale()
    assert t2.net_position("SHFE.rb.HOT") is None
    assert t2.funds() == {} and t2.available() is None


# ------------------------------------------- StraTurtle 集成 (三个接入点)

class TruthCtx:
    """最小上下文: 框架信号口径持仓与真值可分别设定"""

    def __init__(self, sig_pos=0, avgpx=0.0, fees=None):
        self.pos = sig_pos
        self.avgpx = avgpx
        self.fees = fees
        self.logs = []
        self.orders = []
        self.alerts = []

    def stra_get_position(self, code, *a, **k):
        return self.pos

    def stra_get_position_avgpx(self, code=""):
        return self.avgpx

    def stra_set_position(self, code, target, tag=""):
        self.orders.append((code, target, tag))

    def stra_get_fund_data(self, flag=0):
        return self.fees if (flag == 3 and self.fees is not None) else 0.0

    def stra_log_text(self, text, level=1):
        self.logs.append((level, text))


class _Notify:
    def __init__(self):
        self.sent = []

    def send(self, key, text):
        self.sent.append((key, text))
        return True


def _mk_stra(truth_path="", patience=3, risk_mgr=None, system="S1",
             units=1, rebuild_mode=None):
    inst = InstrumentConfig(code="SHFE.rb.HOT", period="d1", system=system,
                            units=units)
    kw = {} if rebuild_mode is None else {"truth_rebuild_mode": rebuild_mode}
    base = TurtleBaseConfig(recon_patience=patience, truth_json=truth_path,
                            **kw)
    stra = StraTurtle("t", inst, base, risk_mgr=risk_mgr)
    stra._volscale = 10.0
    stra._pricetick = 1.0
    return stra


def _rt_pos(code="SHFE.rb.2701", lots=0.0, side="long"):
    """构造一条真值持仓 (side='long'/'short', lots 为总量)"""
    return {"code": code,
            "long": {"newvol": lots if side == "long" else 0.0,
                     "newavail": lots if side == "long" else 0.0,
                     "prevol": 0.0, "preavail": 0.0},
            "short": {"newvol": lots if side == "short" else 0.0,
                      "newavail": lots if side == "short" else 0.0,
                      "prevol": 0.0, "preavail": 0.0}}


def _closes(last=3500.0):
    import numpy as np
    return np.array([3400.0, last])


def test_reconcile_uses_truth_position(tmp_path):
    """引擎持 100 手, 券商真值 60 手 (框架信号账仍是 100) →
    rebuild 模式下按 60 重建 (仅独占账户部署使用)"""
    p = _write(tmp_path, data={
        "positions": [{"code": "SHFE.rb.2701",
                       "long": {"newvol": 60.0, "newavail": 60.0,
                                "prevol": 0.0, "preavail": 0.0},
                       "short": {"newvol": 0.0, "newavail": 0.0,
                                 "prevol": 0.0, "preavail": 0.0}}]})
    stra = _mk_stra(p, patience=3, rebuild_mode="rebuild")
    stra._engine.confirm_enter(1, 100, 3450.0, n=30.0)
    ctx = TruthCtx(sig_pos=100, avgpx=3450.0)
    for _ in range(2):
        stra._reconcile(ctx, _closes())
    assert stra._recon_miss == 2
    assert stra._engine.layers[0][0] == 100          # 耐心未到, 未重建
    stra._reconcile(ctx, _closes())
    assert stra._engine.layers == [[60, 3450.0]]     # 按真值重建
    assert any("RECONCILE" in t and "truth" in t for _, t in ctx.logs)


def test_truth_alert_mode_never_rebuilds(tmp_path):
    """默认 alert 模式: 券商是账户级持仓 (含其他策略), 只告警不重建/收养"""
    p = _write(tmp_path, data={"positions": [_rt_pos(lots=60.0)]})
    stra = _mk_stra(p, patience=2)          # 默认 alert
    assert stra._truth_mode == "alert"
    # 1) 对账: 引擎/框架仓一致 (100) → 不因真值 60 重建
    stra._engine.confirm_enter(1, 100, 3450.0, n=30.0)
    ctx = TruthCtx(sig_pos=100, avgpx=3450.0)
    for _ in range(3):
        stra._reconcile(ctx, _closes())
    assert stra._recon_miss == 0
    assert stra._engine.layers == [[100, 3450.0]]
    assert any("truth mismatch" in t and "差 -40" in t for _, t in ctx.logs)
    # 2) 收养: 框架空仓不按真值收养
    stra2 = _mk_stra(p)
    assert stra2._truth_position(TruthCtx(sig_pos=0), "SHFE.rb.HOT") \
        == (0.0, "framework")


def test_reconcile_truth_stale_falls_back_to_framework(tmp_path):
    """真值文件缺失 → 回退框架口径 (行为与升级前一致)"""
    stra = _mk_stra(os.path.join(str(tmp_path), "none.json"), patience=2)
    stra._engine.confirm_enter(1, 100, 3450.0, n=30.0)
    ctx = TruthCtx(sig_pos=100, avgpx=3450.0)
    for _ in range(3):
        stra._reconcile(ctx, _closes())
    assert stra._recon_miss == 0                     # 框架口径一致 → 无失配
    assert stra._engine.layers == [[100, 3450.0]]


def test_stale_truth_not_used_for_decisions(tmp_path):
    """P2 实测教训: 陈旧 rtdata.json (残留样本/停更) 不得参与对账与重建 —
    否则会把引擎持仓按过期券商仓位平掉 (回测基线被污染)"""
    p = _write(tmp_path, data={"positions": [{
        "code": "SHFE.rb.2701",
        "long": {"newvol": 0.0, "newavail": 0.0, "prevol": 0.0,
                 "preavail": 0.0},
        "short": {"newvol": 0.0, "newavail": 0.0, "prevol": 0.0,
                  "preavail": 0.0}}]}, age=7200)          # 2 小时前的文件
    # truth_max_age 显式收紧 (生产配置按"无成交不刷新"的实情放宽到 24h,
    # 本用例只验证时效机制本身)
    inst = InstrumentConfig(code="SHFE.rb.HOT", period="d1", system="S1",
                            units=1)
    base = TurtleBaseConfig(recon_patience=2, truth_json=p,
                            truth_max_age=1800.0)
    stra = StraTurtle("t", inst, base)
    stra._volscale = 10.0
    stra._pricetick = 1.0
    stra._engine.confirm_enter(1, 100, 3450.0, n=30.0)
    ctx = TruthCtx(sig_pos=100, avgpx=3450.0)           # 信号账持 100 手
    for _ in range(3):
        stra._reconcile(ctx, _closes())
    assert stra._recon_miss == 0                        # 用框架口径 → 无失配
    assert stra._engine.layers == [[100, 3450.0]]       # 未被真值平掉
    assert stra._account_available() is None            # 陈旧资金也不可用
    assert any("truth source stale" in t for _, t in ctx.logs)


def test_fill_confirm_truth_blocks_reanchor_and_alerts(tmp_path):
    """执行单元卡死: 下单窗口内账户持仓**无增量** → 不回写 avgpx 且
    ghost_fill 告警 (真值增量口径下 fees 不再作为证据)"""
    p = _write(tmp_path, data={"positions": [_rt_pos(lots=0.0)]})
    stra = _mk_stra(p)
    stra._notify = _Notify()
    stra._engine.confirm_enter(1, 50, 3400.0, n=30.0)
    recorded = stra._engine.layers[-1][1]
    stra._fill_pending = {"S1": {"engine": stra._engine, "ts": time.time(),
                                 "fees0": 0.0, "lots": 50, "truth0": 0.0}}
    ctx = TruthCtx(sig_pos=50, avgpx=3410.0)         # 信号账已达标
    stra._reanchor_actual_fill(ctx)
    assert stra._engine.layers[-1][1] == recorded    # 未回写
    assert stra._fill_pending
    stra._fill_pending["S1"]["ts"] -= 700
    stra._reanchor_actual_fill(ctx)
    assert any("ghost_fill" in t for _, t in ctx.logs)
    assert stra._notify.sent[0][0].endswith("ghost_fill")


def test_fill_confirm_truth_allows_reanchor(tmp_path):
    """账户持仓增量达到本层手数 → 允许回写 (即使 fees 无变化)"""
    p = _write(tmp_path, data={"positions": [_rt_pos(lots=50.0)]})
    stra = _mk_stra(p)
    stra._engine.confirm_enter(1, 50, 3400.0, n=30.0)
    stra._fill_pending = {"S1": {"engine": stra._engine, "ts": time.time(),
                                 "fees0": 100.0, "lots": 50, "truth0": 0.0}}
    ctx = TruthCtx(sig_pos=50, avgpx=3405.0, fees=100.0)
    stra._reanchor_actual_fill(ctx)
    assert not stra._fill_pending
    assert abs(stra._engine.layers[-1][1] - 3405.0) < 1e-9


def test_fill_confirm_fees_timeout_unblocks(tmp_path):
    """无真值源 + fees 恒不增 (免费品种) → 超时 2 倍后放行, 不永久卡住"""
    stra = _mk_stra(os.path.join(str(tmp_path), "none.json"))
    stra._engine.confirm_enter(1, 50, 3400.0, n=30.0)
    stra._fill_pending = {"S1": {"engine": stra._engine,
                                 "ts": time.time() - 4000, "fees0": 0.0}}
    ctx = TruthCtx(sig_pos=50, avgpx=3410.0, fees=0.0)
    stra._reanchor_actual_fill(ctx)
    assert not stra._fill_pending                # 放行并完成回写
    assert abs(stra._engine.layers[-1][1] - 3410.0) < 1e-9
    assert any("放行" in t for _, t in ctx.logs)


# --------------------------------------- 共享账户可用资金闸门 (P1)

def _avail_truth(tmp_path, avail, age=0.0):
    return _write(tmp_path, data={"positions": [], "funds": {
        "CNY": {"balance": 23e6, "available": avail, "margin": 18e6}}},
        age=age)


def test_account_available_from_truth(tmp_path):
    stra = _mk_stra(_avail_truth(tmp_path, 4_370_706.19))
    assert abs(stra._account_available() - 4370706.19) < 1e-6
    # 文件陈旧 → 不参与决策
    stra2 = _mk_stra(_avail_truth(tmp_path, 1e6, age=7200))
    assert stra2._account_available() is None
    # 无真值源 → None (回测/未部署)
    stra3 = _mk_stra(os.path.join(str(tmp_path), "none.json"))
    assert stra3._account_available() is None


def _enter_dec(price=3400.0, n=30.0):
    return {"decision": "enter", "direction": 1, "price": price, "n": n,
            "failsafe": False}


def test_account_gate_veto_scales_entry(tmp_path):
    """账户可用资金不足 → 手数按可负担量缩减 (共享账户资金耗尽防线)"""
    stra = _mk_stra(_avail_truth(tmp_path, 20_000.0),
                    risk_mgr=RiskManager(RiskConfig(account_gate_mode="veto",
                                                    account_reserve_frac=0.2)))
    ctx = TruthCtx(sig_pos=0)
    stra._execute(ctx, _enter_dec(), n=30.0)
    # 单手保证金 = 3400×10×0.12 = 4080; (20000×0.8)//4080 = 3 手
    assert ctx.orders == [("SHFE.rb.HOT", 3, "Enter")]
    assert any("account_margin" in t for _, t in ctx.logs)


def test_account_gate_warn_keeps_lots_but_logs(tmp_path):
    """warn (默认): 手数不变, 只告警留痕"""
    stra = _mk_stra(_avail_truth(tmp_path, 20_000.0))   # 默认 warn
    ctx = TruthCtx(sig_pos=0)
    stra._execute(ctx, _enter_dec(), n=30.0)
    lots = ctx.orders[0][1]
    assert lots > 3                                     # 未被资金闸门缩量
    assert any("账户可用资金偏紧" in t for _, t in ctx.logs)


# ------------------------------- 执行单元卡死的有界自愈 (P1)

def _recover_setup(tmp_path, monkeypatch, stale=False):
    p = _avail_truth(tmp_path, 1e6, age=7200 if stale else 0)
    stra = _mk_stra(p)
    stra._notify = _Notify()
    calls = []
    monkeypatch.setattr(os, "_exit", lambda rc: calls.append(rc))
    monkeypatch.setenv("WT_RECOVER_DIR", str(tmp_path))
    return stra, calls


def test_exec_recover_default_is_alert_only(tmp_path, monkeypatch):
    """默认 (未设 WT_EXEC_RECOVER): 不重启, 由人工处置"""
    stra, calls = _recover_setup(tmp_path, monkeypatch)
    monkeypatch.delenv("WT_EXEC_RECOVER", raising=False)
    stra._maybe_exec_recover(TruthCtx(), "target=50 vs broker=0 [truth]")
    assert calls == []


def test_exec_recover_restart_requires_fresh_truth(tmp_path, monkeypatch):
    """真值陈旧时不得触发重启 (排除误判)"""
    stra, calls = _recover_setup(tmp_path, monkeypatch, stale=True)
    monkeypatch.setenv("WT_EXEC_RECOVER", "restart")
    ctx = TruthCtx()
    stra._maybe_exec_recover(ctx, "x")
    assert calls == []
    assert any("exec recover skipped" in t for _, t in ctx.logs)


def test_exec_recover_restart_once_per_day(tmp_path, monkeypatch):
    """restart 模式: 真值新鲜 → 自退一次 + 写 marker; 再次调用被 marker 拦住"""
    stra, calls = _recover_setup(tmp_path, monkeypatch)
    monkeypatch.setenv("WT_EXEC_RECOVER", "restart")
    ctx = TruthCtx()
    stra._maybe_exec_recover(ctx, "target=50 vs broker=0 [truth]")
    assert calls == [1]
    assert stra._notify.sent[0][0].endswith("exec_recover")
    markers = [f for f in os.listdir(str(tmp_path))
               if f.startswith(".exec_recover_")]
    assert len(markers) == 1
    # 第二次 (模拟重启后仍未消解) → 只告警, 不再自退
    stra2, calls2 = _recover_setup(tmp_path, monkeypatch)
    ctx2 = TruthCtx()
    stra2._maybe_exec_recover(ctx2, "still ghost")
    assert calls2 == []
    assert any("本日已重启过一次" in t for _, t in ctx2.logs)


# ------------------------------- 逐笔成交 (trades.csv, P2 层价口径)

_TRD_HDR = ("localid,date,time,code,action,volume,price,tradeid,orderid")


def _trd(tmp_path, rows) -> str:
    p = os.path.join(str(tmp_path), "trades.csv")
    with open(p, "w", encoding="utf-8") as f:
        f.write(_TRD_HDR + "\n")
        for r in rows:
            f.write(r + "\n")
    return p


def _append_trd(tmp_path, rows):
    """追加成交行 (模拟"登记待回写之后"才成交)"""
    p = os.path.join(str(tmp_path), "trades.csv")
    with open(p, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(r + "\n")


def test_trade_journal_filters_cancel_and_parses(tmp_path):
    p = _trd(tmp_path, [
        "0,20260914,1788245888000,SHFE.rb.2701,CNL,4,3400,  1,  2",
        "1,20260914,1788245889000,SHFE.rb.2701,OL,6,3405,  3,  4",
        "2,20260914,1788245890000,SHFE.rb.2701,OL,4,3407,  5,  6",
        "3,20260914,1788245891000,DCE.i.2701,OL,9,999,  7,  8",
        "坏行",
    ])
    j = TradeJournal(p)
    assert j.available
    # 撤单行(CNL)剔除; 6@3405 + 4@3407 = 3405.8
    assert abs(j.layer_price("SHFE.rb.2701", 0, 10) - 3405.8) < 1e-9
    # 未读满 → None (不猜)
    assert j.layer_price("SHFE.rb.2701", 0, 11) is None
    # 其它合约互不干扰
    assert abs(j.layer_price("DCE.i.2701", 0, 9) - 999.0) < 1e-9
    assert j.layer_price("CZCE.MA.HOT", 0, 1) is None


def test_trade_journal_cursor_and_skip(tmp_path):
    p = _trd(tmp_path, ["1,20260914,1,SHFE.rb.2701,OL,7,3400,1,1"])
    j = TradeJournal(p)
    cur = j.cursor()                       # 当前行数 = 1 (表头+1行)
    assert cur == 2
    # 游标之后的成交才计入
    with open(p, "a", encoding="utf-8") as f:
        f.write("2,20260914,2,SHFE.rb.2701,OL,3,3410,2,2\n")
    assert j.layer_price("SHFE.rb.2701", cur, 3) == 3410.0
    # skip: 跳过前 2 手, 取接下来 2 手 (该窗口只有 3 手 3410)
    assert abs(j.layer_price("SHFE.rb.2701", 0, 2, skip=8) - 3410.0) < 1e-9
    assert abs(j.layer_price("SHFE.rb.2701", 0, 1, skip=9) - 3410.0) < 1e-9
    assert j.layer_price("SHFE.rb.2701", 0, 1, skip=10) is None  # 不足


def test_trade_journal_missing_and_truncated(tmp_path):
    j = TradeJournal(os.path.join(str(tmp_path), "nope.csv"))
    assert not j.available and j.cursor() is None
    assert j.layer_price("SHFE.rb.2701", 0, 1) is None
    # 文件被清空(轮转)后重读不报错
    p = _trd(tmp_path, ["1,20260914,1,SHFE.rb.2701,OL,5,3400,1,1"])
    j2 = TradeJournal(p)
    assert j2.layer_price("SHFE.rb.2701", 0, 5) == 3400.0
    with open(p, "w", encoding="utf-8") as f:
        f.write(_TRD_HDR + "\n")
    assert j2.layer_price("SHFE.rb.2701", 0, 5) is None


def test_layer_price_prefers_trades_journal(tmp_path):
    """有逐笔成交 → 层价取真实成交均价 (而非 avgpx 反推)"""
    _write(tmp_path, data={"positions": [_rt_pos(lots=0.0)]})   # 下单前空仓
    _trd(tmp_path, [])                            # 只有表头 (trader init)
    stra = _mk_stra(os.path.join(str(tmp_path), "rtdata.json"))
    stra._engine.confirm_enter(1, 10, 3400.0, n=30.0)
    ctx = TruthCtx(sig_pos=10, avgpx=3499.0)      # avgpx 故意给错
    stra._mark_fill_pending(ctx, "S1", stra._engine)
    _append_trd(tmp_path, ["1,20260914,1,SHFE.rb.2701,OL,10,3405,1,1"])
    _write(tmp_path, data={"positions": [_rt_pos(lots=10.0)]})  # 成交后
    stra._reanchor_actual_fill(ctx)
    assert abs(stra._engine.layers[-1][1] - 3405.0) < 1e-9
    assert any("(trades)" in t for _, t in ctx.logs)
    assert not stra._fill_pending


def test_multi_pending_layers_get_own_prices(tmp_path):
    """P2: 同批两笔待回写 → 逐笔成交按确认顺序分配, 两层各自精确
    (净均价口径欠定, 旧实现只会修正最后一层)"""
    _write(tmp_path, data={"positions": [_rt_pos(lots=0.0)]})
    _trd(tmp_path, [])
    stra = _mk_stra(os.path.join(str(tmp_path), "rtdata.json"))
    e = stra._engine
    e.confirm_enter(1, 10, 3400.0, n=30.0)          # 记录价 3400 (真实 3405)
    ctx = TruthCtx(sig_pos=10, avgpx=3400.0)
    stra._mark_fill_pending(ctx, "S1", e)
    e.confirm_add(5, 3420.0, n=30.0)                # 记录价 3420 (真实 3414)
    stra._mark_fill_pending(ctx, "S1", e)           # 同槽覆盖: 只追最后一层
    assert len(stra._fill_pending) == 1
    _append_trd(tmp_path, [
        "1,20260914,1,SHFE.rb.2701,OL,10,3405,1,1",
        "2,20260914,2,SHFE.rb.2701,OL,5,3414,2,2",
    ])
    _write(tmp_path, data={"positions": [_rt_pos(lots=15.0)]})   # 成交后
    ctx.pos = 15
    ctx.avgpx = 3408.0
    stra._reanchor_actual_fill(ctx)
    assert abs(e.layers[-1][1] - 3414.0) < 1e-9     # 末层真实价
    assert any("(trades)" in t for _, t in ctx.logs)
    assert not stra._fill_pending


def test_dual_subsystems_allocate_fills_in_confirm_order(tmp_path):
    """双系统同批确认 (两条记录共享游标): 按确认顺序分配逐笔成交,
    两子系统各自拿到真实成交价 (旧单槽实现只会保留最后一条)"""
    _write(tmp_path, data={"positions": [_rt_pos(lots=0.0)]})
    _trd(tmp_path, [])
    stra = _mk_stra(os.path.join(str(tmp_path), "rtdata.json"),
                    system="S1+S2", units=2)
    e1, e2 = stra._subs[0]["engine"], stra._subs[1]["engine"]
    e1.confirm_enter(1, 10, 3400.0, n=30.0)
    e2.confirm_enter(1, 5, 3420.0, n=30.0)
    ctx = TruthCtx(sig_pos=0)
    stra._mark_fill_pending(ctx, "S1", e1)
    stra._mark_fill_pending(ctx, "S2", e2)
    assert set(stra._fill_pending) == {"S1", "S2"}
    _append_trd(tmp_path, [
        "1,20260914,1,SHFE.rb.2701,OL,10,3405,1,1",
        "2,20260914,2,SHFE.rb.2701,OL,5,3414,2,2",
    ])
    _write(tmp_path, data={"positions": [_rt_pos(lots=15.0)]})   # 成交后
    ctx.pos = 15
    ctx.avgpx = 3408.0
    stra._reanchor_actual_fill(ctx)
    assert abs(e1.layers[-1][1] - 3405.0) < 1e-9
    assert abs(e2.layers[-1][1] - 3414.0) < 1e-9
    assert not stra._fill_pending


def test_periodic_truth_check_alerts_and_escalates(tmp_path, monkeypatch):
    """周期真值对账: 引擎已平/券商仍有仓 → 立即告警, 持续 10 分钟后升级"""
    p = _write(tmp_path, data={"positions": [_rt_pos(lots=3.0)]})
    stra = _mk_stra(p)
    stra._notify = _Notify()
    ctx = TruthCtx(sig_pos=0)                 # 引擎(框架)已平, 券商仍持 3 手
    monkeypatch.setattr(time, "time", lambda: 1000.0)
    stra._periodic_truth_check(ctx)
    assert any("truth mismatch" in t for _, t in ctx.logs)
    # 未到间隔 → 静默
    n = len(ctx.logs)
    monkeypatch.setattr(time, "time", lambda: 1100.0)
    stra._periodic_truth_check(ctx)
    assert len(ctx.logs) == n
    # 差额持续 > 10 分钟 → 升级告警 (含"持续"字样)
    monkeypatch.setattr(time, "time", lambda: 1800.0)
    stra._periodic_truth_check(ctx)
    assert any("truth_mismatch_persist" in k for k, _ in stra._notify.sent)
    # 恢复一致 → 计数器复位
    ctx.pos = 3
    monkeypatch.setattr(time, "time", lambda: 2400.0)
    stra._periodic_truth_check(ctx)
    assert stra._truth_mismatch_since is None


def test_alerts_persisted_to_local_file(tmp_path, monkeypatch):
    """告警无条件落盘 (notifier 未配置时也不静默) — 事故教训"""
    _write(tmp_path, data={"positions": []})
    stra = _mk_stra(os.path.join(str(tmp_path), "rtdata.json"))
    logs = tmp_path / "Logs"
    import StraTurtle as stmod
    monkeypatch.setattr(stmod, "_GROUP_DIR", str(tmp_path))
    stra._alert("unit_test", "hello")
    body = (logs / "alerts.log").read_text(encoding="utf-8")
    assert "unit_test" in body and "hello" in body
