"""fetch_data_sina 测试 — 临时 journal 目录 finally 清理 (P2-1)"""
import os
import sys

from conftest import *  # noqa: F401,F403
import scripts.fetch_data_sina as sina


def _rows(n=3):
    return [{
        "date": 20200101 + i, "time": 0,
        "open": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i,
        "close": 100.5 + i, "volume": 1000.0, "open_interest": 500.0,
    } for i in range(n)]


def test_journal_tmpdir_cleaned(tmp_path, monkeypatch):
    """main() 退出 (含 sys.exit) 后 mkdtemp 临时目录必须被清理 (P2-1)"""
    made = []
    real_mkdtemp = __import__("tempfile").mkdtemp

    def spy_mkdtemp(*a, **k):
        k.setdefault("dir", str(tmp_path))
        d = real_mkdtemp(*a, **k)
        made.append(d)
        return d

    monkeypatch.setattr("tempfile.mkdtemp", spy_mkdtemp)
    monkeypatch.setattr(sina, "fetch_daily",
                        lambda symbol, start, end: _rows())
    monkeypatch.setattr(sina.time, "sleep", lambda *_a, **_k: None)
    storage = tmp_path / "storage"
    monkeypatch.setattr(sys, "argv",
                        ["x", "--codes", "SHFE.rb.HOT",
                         "--storage", str(storage)])
    try:
        sina.main()
    except SystemExit as e:
        assert e.code == 0          # 单品种成功 → exit 0
    else:
        raise AssertionError("main 应以 sys.exit 结束")
    assert made, "mkdtemp 应被调用"
    assert all(not os.path.exists(d) for d in made), \
        "journal 临时目录泄漏 (P2-1 回归)"
    # 数据本身落盘在 storage/csv
    assert (storage / "csv" / "SHFE.rb.HOT_d.csv").exists()
