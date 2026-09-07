"""fetch_data_tqsdk.py 纯逻辑测试 (P2 TqSdk 适配器)

集成路径 (真实 tqsdk/wtpy 网络获取) 不在单测范围; 覆盖:
- 代码映射 std_code_to_tq / tq_to_wt_contract
- dmpBars 缓冲 → merge_rows 行转换 (分钟线 time 口径 G2)
- hots 条目生成与合并 (空键写入/已有表保护/强制覆盖)
- BarJournal.merge_rows 直传行合并 (适配器落地点)
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "helpers"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import fetch_data_tqsdk as fq  # noqa: E402
from helpers.BarJournal import BarJournal  # noqa: E402


# ------------------------------------------------------------------
# 代码映射
# ------------------------------------------------------------------
def test_std_code_to_tq():
    assert fq.std_code_to_tq("SHFE.rb.HOT") == "KQ.m@SHFE.rb"
    assert fq.std_code_to_tq("SHFE.rb.2601") == "SHFE.rb2601"
    assert fq.std_code_to_tq("DCE.l.HOT") == "KQ.m@DCE.l"
    assert fq.std_code_to_tq("SSE.600000") == "SSE.600000"  # 非期货透传


def test_tq_to_wt_contract():
    assert fq.tq_to_wt_contract("SHFE.rb2601") == "SHFE.rb.2601"
    assert fq.tq_to_wt_contract("DCE.l2605") == "DCE.l.2605"
    assert fq.tq_to_wt_contract("CFFEX.IF2509") == "CFFEX.IF.2509"


# ------------------------------------------------------------------
# dmpBars 缓冲转换
# ------------------------------------------------------------------
def _mk_buffer(rows):
    """rows: [(date, time_int, o,h,l,c, vol, hold)]"""
    from wtpy.WtCoreDefs import WTSBarStruct
    BUF = WTSBarStruct * len(rows)
    buf = BUF()
    for i, (d, t, o, h, l, c, v, hd) in enumerate(rows):
        b = buf[i]
        b.date, b.time = d, t
        b.open, b.high, b.low, b.close = o, h, l, c
        b.vol, b.hold = v, hd
    return buf, len(rows)


def test_rows_from_buffer_day():
    buf, n = _mk_buffer([(20260828, 0, 3400, 3420, 3390, 3410, 1000, 5000)])
    rows = fq.rows_from_buffer(buf, n, "day")
    assert rows == [{"date": 20260828, "time": 0, "open": 3400.0,
                     "high": 3420.0, "low": 3390.0, "close": 3410.0,
                     "volume": 1000.0, "open_interest": 5000.0}]


def test_rows_from_buffer_min5_time_hhmmss():
    # dmpBars 口径: time = (date-19900000)*10000 + HHMM
    t = (20260828 - 19900000) * 10000 + 905
    buf, n = _mk_buffer([(20260828, t, 3410, 3415, 3408, 3412, 100, 5000)])
    rows = fq.rows_from_buffer(buf, n, "min5")
    assert rows[0]["time"] == 90500          # HHMMSS 键口径
    assert rows[0]["date"] == 20260828


# ------------------------------------------------------------------
# hots 条目与合并
# ------------------------------------------------------------------
def test_hots_entries_from_cont():
    entries = fq.hots_entries_from_cont(
        {"SHFE.rb.HOT": "SHFE.rb2601", "DCE.l.HOT": "DCE.l2605"},
        tdate=20260901)
    assert entries["SHFE"]["rb"][0]["to"] == "2601"
    assert entries["SHFE"]["rb"][0]["date"] == 20260901
    assert entries["DCE"]["l"][0]["from"] == ""


def test_merge_hots_only_empty(tmp_path):
    p = str(tmp_path / "hots.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"SHFE": {"rb": [{"date": 20200101, "to": "2010"}]},
                   "DCE": {"l": []}}, f)
    entries = {"SHFE": {"rb": [{"date": 20260901, "to": "2601"}]},
               "DCE": {"l": [{"date": 20260901, "to": "2605"}]}}
    changed = fq.merge_hots(p, entries, only_empty=True)
    assert changed == ["DCE.l"]                    # 已有表的 rb 不动
    data = json.load(open(p, encoding="utf-8"))
    assert data["SHFE"]["rb"][0]["to"] == "2010"   # 历史表保留
    assert data["DCE"]["l"][0]["to"] == "2605"     # 空键已写


def test_merge_hots_force_overwrite(tmp_path):
    p = str(tmp_path / "hots.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"SHFE": {"rb": [{"date": 20200101, "to": "2010"}]}}, f)
    entries = {"SHFE": {"rb": [{"date": 20260901, "to": "2601"}]}}
    changed = fq.merge_hots(p, entries, only_empty=False)
    assert changed == ["SHFE.rb"]
    data = json.load(open(p, encoding="utf-8"))
    assert data["SHFE"]["rb"][0]["to"] == "2601"


def test_merge_hots_creates_file(tmp_path):
    p = str(tmp_path / "hots.json")
    changed = fq.merge_hots(p, {"SHFE": {"hc": [{"date": 20260901,
                                                 "to": "2601"}]}})
    assert changed == ["SHFE.hc"]
    assert os.path.exists(p)


# ------------------------------------------------------------------
# BarJournal.merge_rows 直传行合并 (适配器落地点)
# ------------------------------------------------------------------
def test_merge_rows_direct_m5_layout(tmp_path):
    j = BarJournal(str(tmp_path / "j"))
    csv_path = str(tmp_path / "SHFE.rb.HOT_m5.csv")
    rows = [
        {"date": 20260828, "time": 90500, "open": 3410, "high": 3415,
         "low": 3408, "close": 3412, "volume": 100, "open_interest": 5000},
        {"date": 20260828, "time": 91000, "open": 3412, "high": 3418,
         "low": 3410, "close": 3416, "volume": 120, "open_interest": 5010},
    ]
    n = j.merge_rows(rows, "min5", csv_path)
    assert n == 2
    content = open(csv_path, encoding="utf-8").read()
    assert content.startswith("date,time,open")   # 分钟版式带 time 列
    assert "09:05:00" in content
    # 幂等: 重复合并不追加
    assert j.merge_rows(rows, "min5", csv_path) == 0


def test_merge_rows_respects_existing_max_key(tmp_path):
    j = BarJournal(str(tmp_path / "j"))
    csv_path = str(tmp_path / "SHFE.rb.HOT_d.csv")
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write("date,open,high,low,close,volume,turnover,open_interest\n"
                "20260827,3400,3420,3390,3410,1000,0,5000\n")
    rows = [
        {"date": 20260827, "time": 0, "open": 1, "high": 1, "low": 1,
         "close": 1},                                  # 已有 → 跳过
        {"date": 20260828, "time": 0, "open": 3410, "high": 3430,
         "low": 3400, "close": 3425, "volume": 1100},  # 新增
    ]
    assert j.merge_rows(rows, "day", csv_path) == 1
    lines = open(csv_path, encoding="utf-8").read().strip().splitlines()
    assert len(lines) == 3                             # 表头 + 2 行
