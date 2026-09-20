"""HOT 日线每日续接脚本测试 (P0 数据管道修复)

背景: 实盘策略读 XXX_HOT 日线, 而数据底盘只写真实合约日线 → HOT 文件
自 2026-09-01 冻结, 策略用陈旧窗口算 N/20/55 日通道。
"""
import json
import os
import sys

from conftest import *  # noqa: F401,F403

sys.path.insert(0, os.path.join(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))), "scripts"))
import update_hot_daily as uhd  # noqa: E402


def test_parse_contracts_takes_last_rule():
    hots = {"SHFE": {"rb": [{"date": 20260901, "to": "rb2701"},
                            {"date": 20261001, "to": "rb2705"}],
                     "cu": []},
            "INE": {"sc": [{"to": "sc2610"}]}}
    m = uhd.parse_contracts(hots, ["SHFE.rb.HOT", "SHFE.cu.HOT",
                                   "INE.sc.HOT", "CZCE.MA.HOT", "bad"])
    assert m["SHFE.rb.HOT"] == "rb2705"      # 取最后一条规则
    assert m["SHFE.cu.HOT"] is None          # 空规则 → 跳过
    assert m["INE.sc.HOT"] == "sc2610"
    assert m["CZCE.MA.HOT"] is None          # 键缺失
    assert m["bad"] is None


def test_dedup_by_date_keeps_last_and_sorts():
    """数据底盘重启会重复追加同一天 → 必须去重 (否则窗口双计)"""
    bars = [{"date": 20260910, "close": 1.0},
            {"date": 20260908, "close": 2.0},
            {"date": 20260910, "close": 3.0},     # 重复: 保留后者
            {"date": 0, "close": 9.0},            # 非法 → 丢弃
            {"close": 9.0}]
    out = uhd.dedup_by_date(bars)
    assert [b["date"] for b in out] == [20260908, 20260910]
    assert out[-1]["close"] == 3.0


def test_bars_to_rows_field_mapping():
    rows = uhd.bars_to_rows([{"date": 20260916, "open": 1, "high": 2,
                              "low": 0.5, "close": 1.5, "volume": 10,
                              "turnover": 123.0, "open_interest": 77}])
    assert rows[0] == {"date": 20260916, "time": 1500, "open": 1.0,
                       "high": 2.0, "low": 0.5, "close": 1.5,
                       "volume": 10.0, "turnover": 123.0,
                       "open_interest": 77.0}


def test_update_appends_delta_and_rebuilds(tmp_path):
    """端到端 (注入式 reader): 增量写入 csv (幂等) + 触发 DSB 重建"""
    common = tmp_path / "common"
    storage = tmp_path / "storage"
    (common).mkdir()
    (storage / "csv").mkdir(parents=True)
    (common / "hots.json").write_text(json.dumps(
        {"SHFE": {"rb": [{"to": "rb2701"}]}}), encoding="utf-8")
    # 真实合约 DSB 已有数据 (含重复日期)
    bars = [{"date": 20260901, "open": 3100, "high": 3120, "low": 3090,
             "close": 3110, "volume": 100, "turnover": 1.0,
             "open_interest": 5},
            {"date": 20260902, "open": 3110, "high": 3130, "low": 3100,
             "close": 3120, "volume": 110, "turnover": 2.0,
             "open_interest": 6},
            {"date": 20260902, "open": 3110, "high": 3131, "low": 3101,
             "close": 3121, "volume": 111, "turnover": 3.0,
             "open_interest": 7}]
    calls = []

    def fake_read(path):
        return bars

    def fake_rebuild(csv_dir, storage_dir):
        calls.append((csv_dir, storage_dir))
        return 1

    csv_path = storage / "csv" / "SHFE.rb.HOT_d.csv"
    csv_path.write_text(
        "date,open,high,low,close,volume,turnover,open_interest\n"
        "2026/09/01,3100,3120,3090,3110,100,0,5\n", encoding="utf-8")
    # 主力合约文件存在 (内容由注入的 reader 提供)
    d = storage / "his" / "day" / "SHFE"
    d.mkdir(parents=True)
    (d / "rb2701.dsb").write_bytes(b"x")
    got = uhd.update(["SHFE.rb.HOT"], str(common), str(storage),
                     read_bars=fake_read, rebuild=fake_rebuild)
    assert got["SHFE.rb.HOT"] == 1              # 只追加 09-02 (去重后)
    assert calls and calls[0][1] == str(storage)
    lines = csv_path.read_text().strip().splitlines()
    assert lines[-1].startswith("2026/09/02")   # 保持原文件日期风格
    assert "3121.0" in lines[-1]                # 重复日期取后者
    # 幂等: 再跑一次不再追加
    assert uhd.update(["SHFE.rb.HOT"], str(common), str(storage),
                      read_bars=fake_read,
                      rebuild=lambda *a: 0)["SHFE.rb.HOT"] == 0


def test_update_skips_missing_rule_or_file(tmp_path):
    """缺 hots 规则/缺主力合约文件 → 跳过并返回 0, 不抛异常"""
    common = tmp_path / "common"
    storage = tmp_path / "storage"
    common.mkdir()
    (storage / "csv").mkdir(parents=True)
    (common / "hots.json").write_text(json.dumps(
        {"SHFE": {"rb": [{"to": "rb2701"}], "cu": []}}), encoding="utf-8")
    got = uhd.update(["SHFE.rb.HOT", "SHFE.cu.HOT", "X.Y.HOT"],
                     str(common), str(storage),
                     read_bars=lambda p: [],
                     rebuild=lambda *a: 0)
    assert got == {"SHFE.rb.HOT": 0, "SHFE.cu.HOT": 0, "X.Y.HOT": 0}
    # hots.json 缺失 → 返回空 dict
    assert uhd.update(["SHFE.rb.HOT"], str(tmp_path / "none"), str(storage),
                      read_bars=lambda p: [], rebuild=lambda *a: 0) == {}


def test_update_survives_reader_exception(tmp_path):
    """任一品种读取异常不得影响其它品种/整体流程"""
    common = tmp_path / "common"
    storage = tmp_path / "storage"
    common.mkdir()
    (storage / "csv").mkdir(parents=True)
    (common / "hots.json").write_text(json.dumps(
        {"SHFE": {"rb": [{"to": "rb2701"}]}}), encoding="utf-8")
    (storage / "his" / "day" / "SHFE").mkdir(parents=True)
    (storage / "his" / "day" / "SHFE" / "rb2701.dsb").write_bytes(b"x")

    def boom(path):
        raise RuntimeError("decode error")

    got = uhd.update(["SHFE.rb.HOT"], str(common), str(storage),
                     read_bars=boom, rebuild=lambda *a: 0)
    assert got == {"SHFE.rb.HOT": 0}
