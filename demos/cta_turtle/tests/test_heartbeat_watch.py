"""heartbeat_watch 纯函数回归 (Phase 0, 2026-11-09)。

watchdog 是 2026-09-28 商品行情全天中断事故的外部防线, 其时段判定与
告警去重必须是纯函数可测的: 跨零点节段/午休/节假日/状态转换提醒。
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import heartbeat_watch as hw  # noqa: E402


def _tpl(*secs):
    return {"sections": [{"from": f, "to": t} for f, t in secs]}


# ------------------------------------------------------------------
# section_active / market_open
# ------------------------------------------------------------------
def test_section_active_normal():
    tpl = _tpl((2100, 2300), (900, 1015))
    assert hw.section_active(2100, tpl["sections"])
    assert hw.section_active(2259, tpl["sections"])
    assert not hw.section_active(2300, tpl["sections"])   # 右开区间
    assert hw.section_active(900, tpl["sections"])
    assert not hw.section_active(830, tpl["sections"])


def test_section_active_midnight_wrap():
    tpl = _tpl((2100, 100))            # cu/al 21:00-01:00
    assert hw.section_active(2100, tpl["sections"])
    assert hw.section_active(0, tpl["sections"])
    assert hw.section_active(59, tpl["sections"])
    assert not hw.section_active(100, tpl["sections"])
    assert not hw.section_active(1200, tpl["sections"])
    tpl230 = _tpl((2100, 230))         # sc 21:00-02:30
    assert hw.section_active(229, tpl230["sections"])
    assert not hw.section_active(230, tpl230["sections"])


def test_section_active_degenerate_ignored():
    assert not hw.section_active(1000, [{"from": 1000, "to": 1000}])
    assert not hw.section_active(1000, [{"from": "x", "to": None}])


def test_market_open_respects_day_and_sections():
    holidays = {"20261001"}
    # FN2300 形状: 夜盘 + 白盘两节段 (午休 11:30-13:30 无覆盖)
    tpl = _tpl((2100, 2300), (900, 1015), (1030, 1130), (1330, 1500))
    d = datetime.datetime(2026, 11, 9, 10, 0)     # 周一盘中
    assert hw.market_open(d, tpl, holidays)
    d = datetime.datetime(2026, 11, 9, 12, 0)     # 午休
    assert not hw.market_open(d, tpl, holidays)
    d = datetime.datetime(2026, 11, 7, 10, 0)     # 周六
    assert not hw.market_open(d, tpl, holidays)
    d = datetime.datetime(2026, 10, 1, 10, 0)     # 国庆
    assert not hw.market_open(d, tpl, holidays)
    d = datetime.datetime(2026, 11, 9, 22, 0)     # 夜盘
    assert hw.market_open(d, tpl, holidays)


def test_is_watch_day():
    assert hw.is_watch_day(datetime.date(2026, 11, 9), set())      # 周一
    assert not hw.is_watch_day(datetime.date(2026, 11, 7), set())  # 周六
    assert not hw.is_watch_day(datetime.date(2026, 11, 9),
                               {"20261109"})


# ------------------------------------------------------------------
# classify
# ------------------------------------------------------------------
NOW = 1_760_000_000.0


def test_classify_closed():
    assert hw.classify(None, 0, NOW, 360, 180, False) == ("closed", "")


def test_classify_no_heartbeat():
    assert hw.classify(None, 0, NOW, 360, 180, True)[0] == "no_heartbeat"


def test_classify_process_silent():
    st, detail = hw.classify({"last_tick_ts": NOW - 5}, NOW - 500,
                             NOW, 360, 180, True)
    assert st == "process_silent" and "stale" in detail


def test_classify_tick_silent():
    st, _ = hw.classify({"last_tick_ts": NOW - 400}, NOW - 5,
                        NOW, 360, 180, True)
    assert st == "tick_silent"
    st, _ = hw.classify({"last_tick_ts": 0.0}, NOW - 5, NOW, 360, 180, True)
    assert st == "tick_silent"                 # 从未有 tick


def test_classify_degraded_and_ok():
    st, _ = hw.classify({"last_tick_ts": NOW - 5, "degraded": True},
                        NOW - 5, NOW, 360, 180, True)
    assert st == "degraded"
    assert hw.classify({"last_tick_ts": NOW - 5}, NOW - 5,
                       NOW, 360, 180, True)[0] == "ok"


# ------------------------------------------------------------------
# should_alert (状态转换 + 周期提醒)
# ------------------------------------------------------------------
def test_should_alert_transition_and_remind():
    # 新 bad 状态 → 告警
    assert hw.should_alert("ok", "tick_silent", 0, NOW, 1800)
    # bad → 同 bad 未到期 → 不告警
    assert not hw.should_alert("tick_silent", "tick_silent",
                               NOW - 100, NOW, 1800)
    # bad → 同 bad 超过 remind → 重复提醒
    assert hw.should_alert("tick_silent", "tick_silent",
                           NOW - 1900, NOW, 1800)
    # bad → ok → 恢复通知
    assert hw.should_alert("tick_silent", "ok", NOW - 100, NOW, 1800)
    # ok → ok / closed 不告警
    assert not hw.should_alert("ok", "ok", 0, NOW, 1800)
    assert not hw.should_alert("ok", "closed", 0, NOW, 1800)
    assert not hw.should_alert("closed", "closed", 0, NOW, 1800)


# ------------------------------------------------------------------
# load_sessions 编码鲁棒 (GBK/UTF-8/损坏)
# ------------------------------------------------------------------
def test_load_sessions_encodings(tmp_path):
    p = tmp_path / "s.json"
    p.write_bytes(json.dumps(
        {"FN2300": {"sections": [{"from": 2100, "to": 2300}]}}
    ).encode("gbk"))
    assert "FN2300" in hw.load_sessions(str(p))
    p.write_bytes(b"\xff\xfe garbage")
    assert hw.load_sessions(str(p)) == {}
    assert hw.load_sessions(str(tmp_path / "missing.json")) == {}
