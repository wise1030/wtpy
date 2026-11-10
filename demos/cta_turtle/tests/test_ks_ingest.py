"""KS 摄取适配回归 (Phase 1, 2026-11-10)。

ks2wtpy 转换产物 (CZCE.<PROD><yyyy4>_d.csv) -> contracts 缓存:
- 4 位年月转 WT CZCE 3 位 (年尾数+月), SPD/IPS/HOT/main 全部跳过;
- open_interest 列即 hold (日历 max-hold 判定的输入);
- 窗口过滤 + 幂等 (内容一致不重写)。
"""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.build_contract_signals import ingest_ks_csv  # noqa: E402


def _write(tmp_path, name, rows):
    p = os.path.join(str(tmp_path), name)
    with open(p, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close", "volume",
                    "turnover", "open_interest"])
        w.writerows(rows)
    return p


def test_ingest_ks_basic_and_skip_derived(tmp_path):
    _write(tmp_path, "CZCE.TA1601_d.csv", [
        ["2016/01/04", "4500", "4600", "4400", "4550", "100", "0", "5000"],
        ["2016/01/05", "4550", "4560", "4500", "4520", "90", "0", "5100"]])
    _write(tmp_path, "CZCE.TA2601_d.csv", [
        ["2025/12/22", "5000", "5100", "4900", "5050", "80", "0", "6000"]])
    _write(tmp_path, "CZCE.SPD TA601&TA605_d.csv", [
        ["2016/01/04", "1", "2", "0", "1", "5", "0", "10"]])
    _write(tmp_path, "CZCE.TA.HOT_d.csv", [
        ["2016/01/04", "4500", "4600", "4400", "4550", "100", "0", "5000"]])
    cache = os.path.join(str(tmp_path), "cache")
    n = ingest_ks_csv("CZCE", "TA", str(tmp_path), cache)
    assert n == 1, f"只有 601 一个缓存 (两个十年同码合并), 实际 {n}"
    rows = json.load(open(os.path.join(cache, "CZCE.TA.601.json")))
    assert len(rows) == 3                    # 2016 + 2025 行并存 (按日期 disjoint)
    assert rows[0]["date"] == "20160104"
    assert rows[0]["hold"] == 5000.0
    assert rows[-1]["date"] == "20251222"
    # 幂等: 重跑不重写
    assert ingest_ks_csv("CZCE", "TA", str(tmp_path), cache) == 0


def test_ingest_ks_window_and_other_products(tmp_path):
    _write(tmp_path, "CZCE.MA1605_d.csv", [
        ["2015/12/31", "1", "1", "1", "1", "1", "0", "1"],   # 窗外
        ["2016/01/04", "2200", "2300", "2100", "2250", "50", "0", "800"]])
    cache = os.path.join(str(tmp_path), "cache")
    n = ingest_ks_csv("CZCE", "MA", str(tmp_path), cache)
    assert n == 1
    rows = json.load(open(os.path.join(cache, "CZCE.MA.605.json")))
    assert len(rows) == 1 and rows[0]["date"] == "20160104"


def test_ingest_ks_shfe_4digit(tmp_path):
    """SHFE 4 位文件名 (rb1905) 也兼容 (若未来整体切换 KS 源)。"""
    _write(tmp_path, "SHFE.rb1905_d.csv", [
        ["2018/05/16", "3500", "3600", "3400", "3550", "10", "0", "100"]])
    cache = os.path.join(str(tmp_path), "cache")
    n = ingest_ks_csv("SHFE", "rb", str(tmp_path), cache)
    assert n == 1
    rows = json.load(open(os.path.join(cache, "SHFE.rb.1905.json")))
    assert rows[0]["date"] == "20180516"
