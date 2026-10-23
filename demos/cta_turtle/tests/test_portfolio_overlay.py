"""组合约束回放纯函数测试 (2026-10-20)"""
from run_portfolio import build_episodes, replay, _pnl_between


def test_build_episodes_long_add_close():
    trades = [(1, 10.0), (2, 5.0), (3, -15.0)]
    eps = build_episodes("A", "g", trades, last_date=5)
    assert len(eps) == 1
    e = eps[0]
    assert e["start"] == 1 and e["end"] == 3 and e["dir"] == 1


def test_build_episodes_flip_creates_two():
    trades = [(1, 10.0), (2, -10.0), (2, -5.0), (3, 5.0)]
    eps = build_episodes("A", "g", trades, last_date=5)
    # day2: 平多 + 开空 → 旧 episode 结束于 2, 新 episode 从 2 开始
    assert [(e["start"], e["end"], e["dir"]) for e in eps] == [
        (1, 2, 1), (2, 3, -1)]


def test_pnl_between_excludes_entry_includes_exit():
    pnl = {1: 99.0, 2: 10.0, 3: 5.0, 4: 7.0}
    assert _pnl_between(pnl, 1, 3) == 15.0


def test_replay_total_units_limit():
    episodes = {
        "A": [{"code": "A", "group": "g", "start": 1, "end": 3, "dir": 1}],
        "B": [{"code": "B", "group": "g", "start": 2, "end": 4, "dir": 1}],
    }
    pnl = {"A": {2: 10.0, 3: 5.0}, "B": {3: 7.0, 4: 3.0}}
    daily, equity, allowed, stats = replay(
        episodes, pnl, [1, 2, 3, 4], max_units=1, max_group=0)
    assert [e["code"] for e in allowed] == ["A"]
    assert daily == [0.0, 10.0, 5.0, 0.0]
    assert equity[-1] == 15.0
    assert stats["rejected_by_total_units"] == 1
    assert stats["max_active"] == 1


def test_replay_group_limit():
    episodes = {
        "A": [{"code": "A", "group": "cheme", "start": 1, "end": 3,
               "dir": 1}],
        "B": [{"code": "B", "group": "cheme", "start": 1, "end": 3,
               "dir": 1}],
        "C": [{"code": "C", "group": "metal", "start": 1, "end": 3,
               "dir": 1}],
    }
    pnl = {c: {2: 10.0} for c in "ABC"}
    daily, equity, allowed, stats = replay(
        episodes, pnl, [1, 2, 3], max_units=0, max_group=1)
    # A 先到先得; B 同组拒绝; C 异组放行
    assert [e["code"] for e in allowed] == ["A", "C"]
    assert daily == [0.0, 20.0, 0.0]
    assert stats["rejected_by_group_units"] == 1
