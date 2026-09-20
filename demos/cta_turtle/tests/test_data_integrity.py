"""数据完整性运维测试 (P1): BarJournal / 深度检查 / 策略旁路日志

场景: 初始化暖机深度校验; 运行中K线旁路落盘; 崩溃后
journal→storage/csv 幂等回补 (重启可读到最新历史)。
"""
import json
import os

from conftest import *  # noqa: F401,F403
import pytest

from helpers.BarJournal import BarJournal
from helpers import DataChecker as dc


# ---------- BarJournal 基础 ----------

def test_append_and_read_roundtrip(tmp_path):
    bj = BarJournal(str(tmp_path))
    ok = bj.append("SHFE.rb.HOT", "d1",
                   {"date": 20230103, "time": 1500, "open": 4000,
                    "high": 4010, "low": 3990, "close": 4005,
                    "volume": 100, "open_interest": 5000})
    assert ok is True
    rows = bj.read("SHFE.rb.HOT", "d1")
    assert len(rows) == 1 and rows[0]["close"] == 4005


def test_append_invalid_row_silent_drop(tmp_path):
    bj = BarJournal(str(tmp_path))
    assert bj.append("X.Y.HOT", "d", {"date": "bad"}) is False
    assert bj.append("X.Y.HOT", "d", {"date": 1}) is False   # 缺 OHLC
    assert bj.read("X.Y.HOT", "d") == []


def test_read_tolerates_truncated_line(tmp_path):
    """崩溃瞬间半行 JSON → 跳过不抛异常"""
    bj = BarJournal(str(tmp_path))
    p = tmp_path / "X_Y_HOT_d.jsonl"
    p.write_text('{"date":1,"time":2,"open":3,"high":4,"low":5,'
                 '"close":6}\n{"date":7,"time":8,"open":9\n',
                 encoding="utf-8")
    rows = bj.read("X.Y.HOT", "d")
    assert len(rows) == 1 and rows[0]["date"] == 1


def test_read_dedup_and_sort():
    pass   # 占位对齐命名; 实际断言见下


def test_read_dedups_and_sorts_by_key(tmp_path):
    bj = BarJournal(str(tmp_path))
    for row in ({"date": 20230105, "time": 1500, "open": 1, "high": 1,
                 "low": 1, "close": 1},
                {"date": 20230103, "time": 1500, "open": 2, "high": 2,
                 "low": 2, "close": 2},
                {"date": 20230103, "time": 1500, "open": 9, "high": 9,
                 "low": 9, "close": 9}):     # 同键重复 → 后写覆盖
        bj.append("A.B.HOT", "m5", row)
    rows = bj.read("A.B.HOT", "m5")
    assert [r["date"] for r in rows] == [20230103, 20230105]
    assert rows[0]["close"] == 9.0           # 重复键保留最后一条


# ---------- journal → csv 回补 ----------

def test_journal_keeps_turnover(tmp_path):
    """P2: turnover(成交额/money 列) 一并旁路, 回补后不再恒 0"""
    bj = BarJournal(str(tmp_path / "j"))
    bj.append("SHFE.rb.HOT", "m5",
              {"date": 20230103, "time": 90500, "open": 1, "high": 2,
               "low": 0.5, "close": 1.5, "volume": 10,
               "turnover": 12345.5, "open_interest": 777})
    rows = bj.read("SHFE.rb.HOT", "m5")
    assert rows[0]["turnover"] == 12345.5
    csv_path = tmp_path / "out.csv"
    assert bj.merge_to_csv("SHFE.rb.HOT", "m5", str(csv_path)) == 1
    body = csv_path.read_text().strip().splitlines()[1]
    assert "12345.5" in body and ",777" in body      # turnover 不再写 0


def test_journal_turnover_defaults_zero_for_legacy_rows(tmp_path):
    """旧 journal 行 (无 turnover 字段) → 回补为 0, 不报错"""
    bj = BarJournal(str(tmp_path / "j"))
    bj.append("A.B.HOT", "d", {"date": 20230103, "time": 1500, "open": 1,
                               "high": 1, "low": 1, "close": 1})
    assert bj.read("A.B.HOT", "d")[0]["turnover"] == 0.0


def test_merge_to_csv_creates_and_appends(tmp_path):
    bj = BarJournal(str(tmp_path / "j"))
    for d in (20230103, 20230104, 20230105):
        bj.append("SHFE.rb.HOT", "d",
                  {"date": d, "time": 1500, "open": 1, "high": 2,
                   "low": 0.5, "close": 1.5, "volume": 10})
    csv_path = tmp_path / "storage" / "csv" / "SHFE.rb.HOT_d.csv"
    csv_path.parent.mkdir(parents=True)
    n = bj.merge_to_csv("SHFE.rb.HOT", "d", str(csv_path))
    assert n == 3
    lines = csv_path.read_text().strip().splitlines()
    assert lines[0].startswith("date,open")          # 自动建表头
    assert len(lines) == 4
    # 幂等: 重复执行零追加
    assert bj.merge_to_csv("SHFE.rb.HOT", "d", str(csv_path)) == 0


def test_merge_only_appends_tail_after_csv_max(tmp_path):
    """csv 已有更晚数据(外部补数) → journal 旧行不回灌"""
    bj = BarJournal(str(tmp_path / "j"))
    for d in (20230101, 20230102):
        bj.append("A.B.HOT", "d", {"date": d, "time": 1500, "open": 1,
                                   "high": 1, "low": 1, "close": 1})
    csv_path = tmp_path / "a.csv"
    csv_path.write_text(
        "date,open,high,low,close,volume,turnover,open_interest\n"
        "2023/01/05,1,1,1,1,1,0,1\n", encoding="utf-8")
    assert bj.merge_to_csv("A.B.HOT", "d", str(csv_path)) == 0


def test_merge_respects_existing_keys(tmp_path):
    """journal 与 csv 尾部重叠(宕机前已落盘的最后一根) → 不重复"""
    bj = BarJournal(str(tmp_path / "j"))
    bj.append("A.B.HOT", "d", {"date": 20230104, "time": 1500,
                               "open": 1, "high": 1, "low": 1,
                               "close": 1})
    bj.append("A.B.HOT", "d", {"date": 20230105, "time": 1500,
                               "open": 2, "high": 2, "low": 2,
                               "close": 2})
    csv_path = tmp_path / "a.csv"
    csv_path.write_text(
        "date,open,high,low,close,volume,turnover,open_interest\n"
        "20230104,1,1,1,1,1,0,1\n", encoding="utf-8")
    n = bj.merge_to_csv("A.B.HOT", "d", str(csv_path))
    assert n == 1                                    # 只补 0105


def test_merge_matches_real_daily_layout(tmp_path):
    """真实 storage 日线版式: 斜杠日期 + 无 time 列 (取自 ../storage/csv)"""
    bj = BarJournal(str(tmp_path / "j"))
    for d in (20230104, 20230105):
        bj.append("SHFE.rb.HOT", "d",
                  {"date": d, "time": 1500, "open": 4523, "high": 4610,
                   "low": 4523, "close": 4579, "volume": 1809996})
    csv_path = tmp_path / "rb_d.csv"
    csv_path.write_text(
        "date,open,high,low,close,volume,turnover,open_interest\n"
        "2010/01/04,4523.0,4610.0,4523.0,4579.0,1809996.0,"
        "82828166020.0,965458.0\n"
        "2023/01/04,4523.0,4610.0,4523.0,4579.0,1.0,0.0,9.0\n",
        encoding="utf-8")
    n = bj.merge_to_csv("SHFE.rb.HOT", "d", str(csv_path))
    assert n == 1                                    # 仅补 0105
    tail = csv_path.read_text().strip().splitlines()[-1]
    assert tail.startswith("2023/01/05,")            # 沿用斜杠风格


def test_merge_matches_real_m5_layout(tmp_path):
    """真实 storage 分钟版式: BOM + date,time(HH:MM:SS) 列"""
    bj = BarJournal(str(tmp_path / "j"))
    # 框架分钟 bar.time 口径为 HHMMSS 整数
    for t in (90500, 91000):
        bj.append("SHFE.rb.HOT", "m5",
                  {"date": 20230104, "time": t, "open": 4523,
                   "high": 4540, "low": 4523, "close": 4535,
                   "volume": 72564})
    csv_path = tmp_path / "rb_m5.csv"
    csv_path.write_text(
        "﻿date,time,open,high,low,close,volume,turnover,"
        "open_interest\n"
        "2010/01/04,09:00:00,4523.0,4540.0,4523.0,4535.0,72564.0,"
        "3289037040.0,918970.0\n", encoding="utf-8")
    n = bj.merge_to_csv("SHFE.rb.HOT", "m5", str(csv_path))
    assert n == 2
    lines = csv_path.read_text(encoding="utf-8").strip().splitlines()
    # 沿用既有文件的斜杠日期风格 (版式一致性优先)
    assert lines[-1].startswith("2023/01/04,09:10:00,")


def test_journal_time_roundtrip_format():
    assert BarJournal._fmt_time(90500) == "09:05:00"
    assert BarJournal._fmt_time(150000) == "15:00:00"
    assert BarJournal._parse_time("09:05:00") == 90500
    assert BarJournal._parse_time("4523.0") == 0      # 价格列不误判
    assert BarJournal._parse_time("1") == 0


# ---------- 深度检查 ----------

def _mk_cfg(tmp, text):
    cfgp = tmp / "cfg.yaml"
    cfgp.write_text(text, encoding="utf-8")
    from helpers.ConfigLoader import ConfigLoader
    return ConfigLoader.load(str(cfgp))


def test_required_depth_matches_subscription():
    from schemas import TurtleBaseConfig
    base = TurtleBaseConfig()
    assert dc.required_depth(base, "d1") >= base.bar_cnt
    assert dc.required_depth(base, "m5") >= base.intrabar_cnt


def test_check_depth_flags_insufficient_history(tmp_path):
    storage = tmp_path / "storage"
    csv_dir = storage / "csv"
    csv_dir.mkdir(parents=True)
    (csv_dir / "SHFE.rb.HOT_d.csv").write_text(
        "date,open,high,low,close\n2023/01/03,4,4,4,4\n",
        encoding="utf-8")
    from schemas import TurtleBaseConfig
    issues = dc.check_depth(TurtleBaseConfig(), "SHFE.rb.HOT", "d1",
                            str(storage))
    assert any(i.level == "error" and "深度不足" in i.msg for i in issues)


def test_check_depth_ok_when_enough_rows(tmp_path):
    storage = tmp_path / "storage"
    csv_dir = storage / "csv"
    csv_dir.mkdir(parents=True)
    lines = ["date,open,high,low,close"]
    for i in range(130):
        lines.append(f"2023/{1 + i // 28:02d}/{1 + i % 28:02d},4,4,4,4")
    (csv_dir / "SHFE.rb.HOT_d.csv").write_text("\n".join(lines),
                                               encoding="utf-8")
    from schemas import TurtleBaseConfig
    issues = dc.check_depth(TurtleBaseConfig(), "SHFE.rb.HOT", "d1",
                            str(storage))
    assert any(i.level == "info" and "深度 OK" in i.msg for i in issues)


# ---------- 策略旁路日志链路 ----------

def test_strategy_journals_daily_bars(tmp_path):
    """日线模式: 新完成 bar 幂等写入 journal; 首call建基线不写历史"""
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
    from test_intrabar import FakePeriodBars, MultiCtx
    from StraTurtle import StraTurtle
    from schemas import InstrumentConfig, TurtleBaseConfig

    jdir = tmp_path / "jr"
    inst = InstrumentConfig(code="SHFE.rb.HOT", period="d1", system="S1",
                            units=1)
    base = TurtleBaseConfig(journal_dir=str(jdir))
    stra = StraTurtle("tj", inst, base)
    ctx = MultiCtx()
    d1 = ctx.add_store("d1")
    for i in range(90):
        c = 3400 + 8 * (i % 7 - 3) * 0.5
        d1.append(20230000 + i, 1500, c, c + 2, c - 2, c)
    ctx.add_store("m5").append(20230089, 2110, 3400, 3401, 3399, 3400)
    stra.on_init(ctx)
    stra.on_calculate(ctx)                    # 基线 call
    jfile = jdir / "SHFE_rb_HOT_d.jsonl"
    assert not jfile.exists(), "基线 call 不应写历史"
    # 新日线 → 写入一行
    d1.append(20230090, 1500, 3410, 3412, 3408, 3411)
    ctx.klines["m5"].append(20230090, 2110, 3411, 3412, 3410, 3411)
    stra.on_calculate(ctx)
    assert jfile.exists()
    rows = [json.loads(x) for x in
            jfile.read_text(encoding="utf-8").strip().splitlines()]
    assert len(rows) == 1 and rows[0]["date"] == 20230090
    # 同日重复 calculate → 不重复写
    stra.on_calculate(ctx)
    rows = [json.loads(x) for x in
            jfile.read_text(encoding="utf-8").strip().splitlines()]
    assert len(rows) == 1


def test_repair_end_to_end_via_cli(tmp_path):
    """CLI --repair-journal: journal 尾部合并回 storage/csv"""
    import subprocess
    import sys
    root = os.path.dirname(os.path.abspath(__file__)) + "/.."

    common = tmp_path / "common"
    common.mkdir()
    (common / "hots.json").write_text('{"SHFE":{"rb":[]}}',
                                      encoding="utf-8")
    storage = tmp_path / "storage"
    (storage / "csv").mkdir(parents=True)
    jdir = tmp_path / "jr"
    bj = BarJournal(str(jdir))
    for d in (20230104, 20230105):
        bj.append("SHFE.rb.HOT", "d",
                  {"date": d, "time": 1500, "open": 4, "high": 4,
                   "low": 4, "close": 4})
    cfgp = tmp_path / "cfg.yaml"
    cfgp.write_text(
        "base:\n  journal_dir: %s\ninstruments:\n  - code: SHFE.rb.HOT\n"
        % json.dumps(str(jdir)), encoding="utf-8")

    r = subprocess.run(
        [sys.executable, os.path.join(root, "scripts", "prepare_data.py"),
         "--config", str(cfgp), "--common", str(common),
         "--storage", str(storage), "--repair-journal", str(jdir)],
        capture_output=True, text=True)
    out = r.stdout
    assert "+2 bars" in out or "+2" in out
    csvf = storage / "csv" / "SHFE.rb.HOT_d.csv"
    assert csvf.exists() and len(csvf.read_text().splitlines()) == 3
