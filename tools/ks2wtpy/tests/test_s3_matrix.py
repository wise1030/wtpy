"""
Tests for acceptance.s3_matrix — 跨格式 (CSV/LMDB/DSB) 一致性核对 + S3 端到端 gate.

测试分两类：
  1. 纯本地单元测试：构造已知 (date,time,OHLC) 三处写入，调用 compare_formats 核对。
  2. S3 端到端：默认跳过，仅在 KS2WTPY_S3_ACCESS_KEY 等 env 齐全且 RUN_S3_MATRIX_E2E=1
     时才真实跑 subprocess。
"""
from __future__ import annotations

import json
import os
import struct
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
KS2WTPY = HERE.parent
sys.path.insert(0, str(KS2WTPY))

from acceptance.s3_matrix import (  # noqa: E402
    S3MatrixGate,
    BarTriple,
    compare_triples,
    extract_csv_bars,
    extract_lmdb_bars,
    extract_dsb_bars,
    build_default_codes,
    _resolve_s3_credentials,
)
from dsb_writer import (  # noqa: E402
    BT_HIS_DAY, BT_HIS_MINUTE5, encode_bar_struct, write_dsb_file,
)
from ks_reader import KSBarRecord  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers — 构造一致 / 不一致的三格式产物
# ---------------------------------------------------------------------------

def _make_bar(date: int, time_hm: int, close: float) -> KSBarRecord:
    return KSBarRecord(
        date=date, time=time_hm,
        open=close - 1.0, high=close + 1.5, low=close - 1.5, close=close,
        volume=100.0, amount=1000.0, openinterest=50.0,
    )


def _write_csv_day(path: Path, bars, code: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        f.write("date,open,high,low,close,volume,turnover,open_interest\n")
        for b in bars:
            d = f"{b.date // 10000}/{(b.date // 100) % 100:02d}/{b.date % 100:02d}"
            f.write(f"{d},{b.open},{b.high},{b.low},{b.close},{b.volume},{b.amount},{b.openinterest}\n")


def _write_dsb_day(path: Path, bars):
    raw = b"".join(encode_bar_struct(b, is_day=True) for b in bars)
    write_dsb_file(str(path), BT_HIS_DAY, raw, use_compression=True)


def _write_lmdb_day(path: Path, exchg: str, code: str, bars):
    import lmdb
    path.mkdir(parents=True, exist_ok=True)
    env = lmdb.open(str(path), map_size=10 * 1024 * 1024)
    exchg_bytes = exchg.encode("utf-8").ljust(16, b"\x00")
    code_bytes = code.encode("utf-8").ljust(32, b"\x00")
    with env.begin(write=True) as txn:
        for b in bars:
            bartime = b.date  # 日线 bartime = date
            key = exchg_bytes + code_bytes + struct.pack(">I", bartime)
            value = encode_bar_struct(b, is_day=True)
            txn.put(key, value)
    env.close()


# ---------------------------------------------------------------------------
# Section 1: Bar 提取器
# ---------------------------------------------------------------------------

class TestExtractCsvBars:
    def test_day_csv_basic(self, tmp_path):
        csv = tmp_path / "csv" / "SHFE.rb.2510_d.csv"
        bars = [_make_bar(20240115, 0, 3800.0), _make_bar(20240116, 0, 3820.0)]
        _write_csv_day(csv, bars, "SHFE.rb.2510")

        out = extract_csv_bars(str(csv), is_day=True)
        assert len(out) == 2
        assert out[0].date == 20240115 and out[0].close == 3800.0
        # bartime 日线 == date
        assert out[0].bartime == 20240115
        assert out[1].bartime == 20240116

    def test_missing_file_returns_empty(self, tmp_path):
        out = extract_csv_bars(str(tmp_path / "nope.csv"), is_day=True)
        assert out == []


class TestExtractDsbBars:
    def test_day_dsb_roundtrip(self, tmp_path):
        dsb = tmp_path / "his" / "day" / "SHFE" / "SHFE.rb2510.dsb"
        bars = [_make_bar(20240115, 0, 3800.0), _make_bar(20240116, 0, 3820.0)]
        _write_dsb_day(dsb, bars)

        out = extract_dsb_bars(str(dsb))
        assert len(out) == 2
        assert out[0].date == 20240115
        assert out[0].close == 3800.0
        assert out[0].bartime == 20240115

    def test_missing_dsb_returns_empty(self, tmp_path):
        out = extract_dsb_bars(str(tmp_path / "nope.dsb"))
        assert out == []


class TestExtractLmdbBars:
    def test_day_lmdb_roundtrip(self, tmp_path):
        lmdb_dir = tmp_path / "lmdb" / "day" / "SHFE"
        bars = [_make_bar(20240115, 0, 3800.0), _make_bar(20240116, 0, 3820.0)]
        _write_lmdb_day(lmdb_dir, "SHFE", "rb2510", bars)

        out = extract_lmdb_bars(str(lmdb_dir), code_filter="rb2510")
        assert len(out) == 2
        assert out[0].date in (20240115, 20240116)
        assert all(b.close in (3800.0, 3820.0) for b in out)

    def test_code_filter(self, tmp_path):
        lmdb_dir = tmp_path / "lmdb" / "day" / "SHFE"
        _write_lmdb_day(lmdb_dir, "SHFE", "rb2510",
                        [_make_bar(20240115, 0, 3800.0)])
        _write_lmdb_day(lmdb_dir, "SHFE", "hc2510",
                        [_make_bar(20240115, 0, 3500.0)])

        out_rb = extract_lmdb_bars(str(lmdb_dir), code_filter="rb2510")
        assert len(out_rb) == 1 and out_rb[0].close == 3800.0
        out_hc = extract_lmdb_bars(str(lmdb_dir), code_filter="hc2510")
        assert len(out_hc) == 1 and out_hc[0].close == 3500.0


# ---------------------------------------------------------------------------
# Section 2: 跨格式 compare_triples
# ---------------------------------------------------------------------------

class TestCompareTriples:
    def _mk(self, bartime, close):
        return BarTriple(date=bartime, bartime=bartime, time=0,
                         open=close - 1, high=close + 1, low=close - 1,
                         close=close, volume=100, amount=1000,
                         open_interest=50)

    def test_all_consistent(self):
        bars = [self._mk(20240115, 3800.0), self._mk(20240116, 3820.0)]
        r = compare_triples(csv=bars, lmdb=bars, dsb=bars)
        assert r["status"] == "pass"
        assert r["bartime_count"]["csv"] == 2
        assert r["bartime_count"]["lmdb"] == 2
        assert r["bartime_count"]["dsb"] == 2
        assert r["mismatched_bartimes"] == 0

    def test_missing_in_dsb(self):
        csv_lmdb = [self._mk(20240115, 3800.0), self._mk(20240116, 3820.0)]
        dsb_only = [self._mk(20240115, 3800.0)]
        r = compare_triples(csv=csv_lmdb, lmdb=csv_lmdb, dsb=dsb_only)
        assert r["status"] == "fail"
        assert 20240116 in r["only_in"]["csv_lmdb_not_dsb"]

    def test_close_mismatch(self):
        a = [self._mk(20240115, 3800.0)]
        b = [self._mk(20240115, 3801.0)]  # 1.0 差
        r = compare_triples(csv=a, lmdb=a, dsb=b)
        assert r["status"] == "fail"
        assert r["mismatched_bartimes"] >= 1
        # 应该报告 close 字段
        diffs = r["sample_mismatches"]
        assert any("close" in str(d) for d in diffs)

    def test_skip_when_two_formats_empty(self):
        bars = [self._mk(20240115, 3800.0)]
        # csv 有数据，lmdb/dsb 全 0 → 无法核对，应 skip 或 warn
        r = compare_triples(csv=bars, lmdb=[], dsb=[])
        assert r["status"] in ("skip", "warn")

    def test_warn_on_zero_bars_all(self):
        r = compare_triples(csv=[], lmdb=[], dsb=[])
        assert r["status"] == "skip"


# ---------------------------------------------------------------------------
# Section 3: 数据仓自动发现
# ---------------------------------------------------------------------------

class TestBuildDefaultCodes:
    def test_pick_specific_codes_from_csv(self, tmp_path):
        # 模拟 ks2wtpy 输出布局
        csv_dir = tmp_path / "csv"
        csv_dir.mkdir()
        (csv_dir / "SHFE.rb.2510_d.csv").write_text("date,open\n")
        (csv_dir / "SHFE.hc.2510_d.csv").write_text("date,open\n")
        (csv_dir / "SHFE.rb.HOT_d.csv").write_text("date,open\n")

        codes = build_default_codes(str(tmp_path), max_codes=8)
        # HOT 不参与跨格式核对（DSB 通常只输出具体月份）
        std_codes = [c["stdCode"] for c in codes]
        assert "SHFE.rb.2510" in std_codes
        assert "SHFE.hc.2510" in std_codes
        # HOT 不进
        assert not any(".HOT" in s for s in std_codes)

    def test_empty_dir(self, tmp_path):
        codes = build_default_codes(str(tmp_path), max_codes=8)
        assert codes == []


# ---------------------------------------------------------------------------
# Section 4: Gate 集成
# ---------------------------------------------------------------------------

class TestS3MatrixGate:
    def _setup_consistent_storage(self, tmp_path: Path, code: str = "rb2510"):
        bars = [_make_bar(20240115, 0, 3800.0), _make_bar(20240116, 0, 3820.0)]
        # CSV
        _write_csv_day(tmp_path / "csv" / f"SHFE.rb.2510_d.csv", bars, code)
        # DSB
        _write_dsb_day(tmp_path / "his" / "day" / "SHFE" / f"SHFE.{code}.dsb", bars)
        # LMDB
        _write_lmdb_day(tmp_path / "lmdb" / "day" / "SHFE", "SHFE", code, bars)
        return bars

    def test_gate_pass_on_consistent(self, tmp_path):
        self._setup_consistent_storage(tmp_path)
        gate = S3MatrixGate(skip_s3=True)  # 仅本地核对
        r = gate.run(str(tmp_path))
        assert r.status == "pass", r.summary
        assert r.details["pairs_checked"] >= 1

    def test_gate_skip_when_no_codes(self, tmp_path):
        gate = S3MatrixGate(skip_s3=True)
        r = gate.run(str(tmp_path))
        assert r.status == "skip"

    def test_gate_fail_when_close_mismatch(self, tmp_path):
        bars_csv = [_make_bar(20240115, 0, 3800.0)]
        bars_lmdb = [_make_bar(20240115, 0, 3800.0)]
        bars_dsb = [_make_bar(20240115, 0, 3801.0)]  # 不一致
        _write_csv_day(tmp_path / "csv" / "SHFE.rb.2510_d.csv", bars_csv, "rb2510")
        _write_dsb_day(tmp_path / "his" / "day" / "SHFE" / "SHFE.rb2510.dsb", bars_dsb)
        _write_lmdb_day(tmp_path / "lmdb" / "day" / "SHFE", "SHFE", "rb2510", bars_lmdb)

        gate = S3MatrixGate(skip_s3=True)
        r = gate.run(str(tmp_path))
        assert r.status == "fail"


# ---------------------------------------------------------------------------
# Section 5: 凭据加载（不打印值）
# ---------------------------------------------------------------------------

class TestResolveCredentials:
    def test_from_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KS2WTPY_S3_ENDPOINT", "host.example:6900")
        monkeypatch.setenv("KS2WTPY_S3_ACCESS_KEY", "AK")
        monkeypatch.setenv("KS2WTPY_S3_SECRET_KEY", "SK")
        monkeypatch.setenv("KS2WTPY_S3_BUCKET", "ks-data")
        c = _resolve_s3_credentials(credentials_path=str(tmp_path / "nope.json"))
        assert c["endpoint"] == "host.example:6900"
        assert c["access_key"] == "AK"
        assert c["bucket"] == "ks-data"

    def test_from_credentials_json(self, monkeypatch, tmp_path):
        for k in ("KS2WTPY_S3_ENDPOINT", "KS2WTPY_S3_ACCESS_KEY",
                  "KS2WTPY_S3_SECRET_KEY", "KS2WTPY_S3_BUCKET"):
            monkeypatch.delenv(k, raising=False)
        p = tmp_path / "credentials.json"
        p.write_text(json.dumps({
            "url": "http://host.example:6901/console",
            "accessKey": "AK",
            "secretKey": "SK",
            "api": "auto", "path": "auto",
        }))
        c = _resolve_s3_credentials(credentials_path=str(p))
        # endpoint 必须是 host:6900，无 scheme/path
        assert c["endpoint"] == "host.example:6900"
        assert c["access_key"] == "AK"
        assert c["bucket"] == "ks-data"

    def test_missing_returns_none(self, monkeypatch, tmp_path):
        for k in ("KS2WTPY_S3_ENDPOINT", "KS2WTPY_S3_ACCESS_KEY",
                  "KS2WTPY_S3_SECRET_KEY", "KS2WTPY_S3_BUCKET"):
            monkeypatch.delenv(k, raising=False)
        c = _resolve_s3_credentials(credentials_path=str(tmp_path / "nope.json"))
        assert c is None
