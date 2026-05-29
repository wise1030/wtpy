"""
acceptance.s3_matrix — 跨格式 (CSV/LMDB/DSB) 一致性核对 + S3 全格式端到端 gate.

设计目标：
  1. 真实生产数据仓中，同一 (code, period, bartime) 在 CSV / LMDB / DSB 三处
     的 OHLC/volume 必须严格一致；任一不一致 → fail，因为意味着写入侧某一
     路径有 bug。
  2. 支持 "skip_s3=True" 的纯本地核对（CI / 离线复现），以及 "skip_s3=False"
     的真实 S3 端到端（拉小窗口的一个品种，跑 main.py 三格式输出，再核对）。

不打印任何凭据；所有日志只写 endpoint/access key 的 *存在性*。
"""
from __future__ import annotations

import csv as csv_mod
import io
import json
import logging
import os
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .gates import GateResult  # 复用 dataclass

logger = logging.getLogger(__name__)

# WTSBarStruct: 88 bytes, see dsb_writer / lmdb_writer
_WTS_BAR_SIZE = 88
_WTS_BAR_FMT = "<II Q dddddddd d"  # date(u32) reserve(u32) time(u64) o h l c settle money vol hold add
# 12 个字段：date, reserve, time, open, high, low, close, settle, money, vol, hold, add
_BLK_FLAG = b"&^%$#@!\x00"  # 与 config.BLK_FLAG 对齐

_EXCHG_PAD = 16
_CODE_PAD = 32
_LMDB_KEY_LEN = _EXCHG_PAD + _CODE_PAD + 4  # exchg + code + bartime(big-endian u32)


# ---------------------------------------------------------------------------
# BarTriple — 三格式统一对齐 schema
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BarTriple:
    """规范化 bar，用于跨格式比对。bartime 是主键。"""
    date: int        # YYYYMMDD
    bartime: int     # 日线=date；分钟线=(date-19900000)*10000+time
    time: int        # HHMM (日线=0)
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    open_interest: float


# ---------------------------------------------------------------------------
# 凭据加载（不打印值）
# ---------------------------------------------------------------------------

def _resolve_s3_credentials(credentials_path: Optional[str] = None
                            ) -> Optional[Dict[str, Any]]:
    """优先 env，缺失再读 credentials.json。endpoint 必须是 host:port，无 scheme/path。

    返回 dict 或 None；不输出任何凭据。
    """
    endpoint = os.environ.get("KS2WTPY_S3_ENDPOINT", "").strip()
    access = os.environ.get("KS2WTPY_S3_ACCESS_KEY", "").strip()
    secret = os.environ.get("KS2WTPY_S3_SECRET_KEY", "").strip()
    bucket = os.environ.get("KS2WTPY_S3_BUCKET", "").strip() or "ks-data"

    if endpoint and access and secret:
        return {"endpoint": endpoint, "access_key": access,
                "secret_key": secret, "bucket": bucket, "source": "env"}

    if credentials_path and os.path.isfile(credentials_path):
        try:
            with open(credentials_path, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception as e:  # noqa: BLE001
            logger.warning("credentials.json 读取失败: %s", e)
            return None
        url = (d.get("url") or "").strip()
        access = (d.get("accessKey") or "").strip()
        secret = (d.get("secretKey") or "").strip()
        if url and access and secret:
            # 解析 host，固定使用 S3 API 端口 6900
            try:
                parsed = urlparse(url if "://" in url else f"http://{url}")
                host = parsed.hostname
                if not host:
                    return None
                endpoint = f"{host}:6900"
            except Exception as e:  # noqa: BLE001
                logger.debug("S3 endpoint URL 解析失败: %s", e)
                return None
            return {"endpoint": endpoint, "access_key": access,
                    "secret_key": secret, "bucket": bucket,
                    "source": "credentials.json"}
    return None


# ---------------------------------------------------------------------------
# 提取器：CSV
# ---------------------------------------------------------------------------

def _parse_csv_date_to_int(raw: str) -> int:
    s = raw.strip().replace("/", "").replace("-", "")
    return int(s) if s.isdigit() and len(s) == 8 else 0


def _parse_csv_time_to_int(raw: str) -> int:
    s = raw.strip().replace(":", "")
    return int(s) if s and s.isdigit() else 0


def _bartime_for(date: int, time_hhmm: int, is_day: bool) -> int:
    if is_day:
        return date
    return (date - 19900000) * 10000 + time_hhmm


def extract_csv_bars(csv_path: str, is_day: bool = True) -> List[BarTriple]:
    """读 ks2wtpy 输出的 CSV → BarTriple 列表。BOM + UTF-8。"""
    if not os.path.isfile(csv_path):
        return []
    out: List[BarTriple] = []
    try:
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv_mod.DictReader(f)
            for row in reader:
                date = _parse_csv_date_to_int(row.get("date") or "")
                if date <= 0:
                    continue
                time_val = 0 if is_day else _parse_csv_time_to_int(row.get("time") or "0")
                try:
                    o = float(row.get("open") or 0)
                    h = float(row.get("high") or 0)
                    lo = float(row.get("low") or 0)
                    cl = float(row.get("close") or 0)
                    vol = float(row.get("volume") or 0)
                    amt = float(row.get("turnover") or row.get("amount") or 0)
                    oi = float(row.get("open_interest") or row.get("openinterest") or 0)
                except ValueError:
                    continue
                out.append(BarTriple(
                    date=date,
                    bartime=_bartime_for(date, time_val, is_day),
                    time=time_val,
                    open=o, high=h, low=lo, close=cl,
                    volume=vol, amount=amt, open_interest=oi,
                ))
    except Exception as e:  # noqa: BLE001
        logger.debug("extract_csv_bars %s failed: %s", csv_path, e)
        return []
    return out


# ---------------------------------------------------------------------------
# 提取器：DSB
# ---------------------------------------------------------------------------

def _decode_wts_bar(buf: bytes) -> Optional[BarTriple]:
    if len(buf) != _WTS_BAR_SIZE:
        return None
    try:
        (date, _reserve, time_val,
         o, h, lo, cl, _settle, money, vol, hold, _add) = struct.unpack(_WTS_BAR_FMT, buf)
    except struct.error:
        return None
    # time_val 在日线为 date，在分钟线为 (date-19900000)*10000+HHMM
    is_day = (time_val == date)
    if is_day:
        time_hhmm = 0
        bartime = date
    else:
        time_hhmm = time_val % 10000
        bartime = time_val
    return BarTriple(
        date=int(date), bartime=int(bartime), time=int(time_hhmm),
        open=float(o), high=float(h), low=float(lo), close=float(cl),
        volume=float(vol), amount=float(money), open_interest=float(hold),
    )


def extract_dsb_bars(dsb_path: str) -> List[BarTriple]:
    """读 DSB 文件 → BarTriple 列表。zstd 解压后按 88B 切。"""
    if not os.path.isfile(dsb_path):
        return []
    try:
        with open(dsb_path, "rb") as f:
            data = f.read()
        if len(data) < 20 or data[:8] != _BLK_FLAG:
            return []
        version = struct.unpack("<H", data[10:12])[0]
        comp_size = struct.unpack("<Q", data[12:20])[0]
        payload = data[20:]
        if version in (0x02, 0x04) and len(payload) == comp_size:
            try:
                import zstandard as zstd
                dctx = zstd.ZstdDecompressor()
                raw = dctx.decompress(payload)
            except Exception as e:  # noqa: BLE001
                logger.debug("zstd 解压失败, 使用原始数据: %s", e)
                raw = payload
        else:
            raw = payload
        out: List[BarTriple] = []
        for i in range(0, len(raw), _WTS_BAR_SIZE):
            chunk = raw[i:i + _WTS_BAR_SIZE]
            if len(chunk) != _WTS_BAR_SIZE:
                break
            bar = _decode_wts_bar(chunk)
            if bar is not None:
                out.append(bar)
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("extract_dsb_bars %s failed: %s", dsb_path, e)
        return []


# ---------------------------------------------------------------------------
# 提取器：LMDB
# ---------------------------------------------------------------------------

def extract_lmdb_bars(lmdb_dir: str, code_filter: Optional[str] = None
                      ) -> List[BarTriple]:
    """扫一个 LMDB env（{lmdb}/{period}/{EXCHG}/），过滤 code，返回 BarTriple 列表。

    Key 结构：exchg(16) + code(32) + bartime(big-endian u32) = 52 bytes
    Value 结构：88 bytes WTSBarStruct
    """
    if not os.path.isdir(lmdb_dir):
        return []
    try:
        import lmdb
    except ImportError:
        return []
    code_bytes_target = None
    if code_filter:
        code_bytes_target = code_filter.encode("utf-8").ljust(_CODE_PAD, b"\x00")

    try:
        env = lmdb.open(lmdb_dir, readonly=True, lock=False, subdir=True)
    except Exception as e:  # noqa: BLE001
        logger.debug("lmdb.open(%s) failed: %s", lmdb_dir, e)
        return []
    out: List[BarTriple] = []
    try:
        with env.begin() as txn:
            cur = txn.cursor()
            for key, value in cur:
                if len(key) != _LMDB_KEY_LEN:
                    continue
                if code_bytes_target is not None and key[_EXCHG_PAD:_EXCHG_PAD + _CODE_PAD] != code_bytes_target:
                    continue
                if len(value) != _WTS_BAR_SIZE:
                    continue
                bar = _decode_wts_bar(value)
                if bar is not None:
                    out.append(bar)
    finally:
        env.close()
    return out


# ---------------------------------------------------------------------------
# 跨格式核对
# ---------------------------------------------------------------------------

_COMPARE_FIELDS = ("open", "high", "low", "close", "volume", "amount", "open_interest")
_PRICE_TOL = 1e-6  # 三处都来自同一 KSBarRecord struct，要求严格一致


def compare_triples(csv: List[BarTriple], lmdb: List[BarTriple],
                    dsb: List[BarTriple], price_tol: float = _PRICE_TOL,
                    max_mismatch_samples: int = 10) -> Dict[str, Any]:
    """核对三格式的 (bartime → bar) 字典。

    判定：
      - 三方都为空 → skip
      - 仅一方有数据 → skip（无法核对）
      - 两方非空，第三方空 → warn（数据仓 DSB/LMDB 偏序）
      - 三方都有 → pass/fail 取决于交集字段是否一致
    """
    csv_map = {b.bartime: b for b in csv}
    lmdb_map = {b.bartime: b for b in lmdb}
    dsb_map = {b.bartime: b for b in dsb}

    non_empty = sum(1 for m in (csv_map, lmdb_map, dsb_map) if m)
    result: Dict[str, Any] = {
        "bartime_count": {"csv": len(csv_map), "lmdb": len(lmdb_map), "dsb": len(dsb_map)},
        "mismatched_bartimes": 0,
        "only_in": {},
        "sample_mismatches": [],
    }
    if non_empty == 0:
        result["status"] = "skip"
        result["reason"] = "三格式均为空"
        return result
    if non_empty == 1:
        result["status"] = "skip"
        result["reason"] = "仅一种格式有数据，无法核对"
        return result

    all_bt = set(csv_map) | set(lmdb_map) | set(dsb_map)
    only_csv = sorted(set(csv_map) - set(lmdb_map) - set(dsb_map))[:20]
    only_lmdb = sorted(set(lmdb_map) - set(csv_map) - set(dsb_map))[:20]
    only_dsb = sorted(set(dsb_map) - set(csv_map) - set(lmdb_map))[:20]
    csv_lmdb_not_dsb = sorted((set(csv_map) & set(lmdb_map)) - set(dsb_map))[:20]
    csv_dsb_not_lmdb = sorted((set(csv_map) & set(dsb_map)) - set(lmdb_map))[:20]
    lmdb_dsb_not_csv = sorted((set(lmdb_map) & set(dsb_map)) - set(csv_map))[:20]
    result["only_in"] = {
        "only_csv": only_csv, "only_lmdb": only_lmdb, "only_dsb": only_dsb,
        "csv_lmdb_not_dsb": csv_lmdb_not_dsb,
        "csv_dsb_not_lmdb": csv_dsb_not_lmdb,
        "lmdb_dsb_not_csv": lmdb_dsb_not_csv,
    }

    # 字段一致性：取所有非空格式间的两两交集
    diffs: List[Dict[str, Any]] = []
    bt_with_diff: set = set()
    pairs = []
    if csv_map and lmdb_map:
        pairs.append(("csv", "lmdb", csv_map, lmdb_map))
    if csv_map and dsb_map:
        pairs.append(("csv", "dsb", csv_map, dsb_map))
    if lmdb_map and dsb_map:
        pairs.append(("lmdb", "dsb", lmdb_map, dsb_map))

    for name_a, name_b, ma, mb in pairs:
        common = set(ma) & set(mb)
        for bt in sorted(common):
            ba, bb = ma[bt], mb[bt]
            for fld in _COMPARE_FIELDS:
                va, vb = getattr(ba, fld), getattr(bb, fld)
                if abs(va - vb) > price_tol:
                    bt_with_diff.add(bt)
                    if len(diffs) < max_mismatch_samples:
                        diffs.append({
                            "bartime": bt, "pair": f"{name_a} vs {name_b}",
                            "field": fld, name_a: va, name_b: vb,
                        })

    result["mismatched_bartimes"] = len(bt_with_diff)
    result["sample_mismatches"] = diffs
    result["bartime_union"] = len(all_bt)

    has_only = bool(only_csv or only_lmdb or only_dsb
                    or csv_lmdb_not_dsb or csv_dsb_not_lmdb or lmdb_dsb_not_csv)

    if bt_with_diff:
        result["status"] = "fail"
        result["reason"] = f"{len(bt_with_diff)} 个 bartime 在跨格式间字段不一致"
    elif non_empty < 3:
        result["status"] = "warn"
        result["reason"] = "仅两种格式可对，第三方为空"
    elif has_only:
        result["status"] = "fail"
        result["reason"] = "三格式 bartime 集合不一致（写入端漏数）"
    else:
        result["status"] = "pass"
        result["reason"] = "三格式严格一致"
    return result


# ---------------------------------------------------------------------------
# 数据仓自动发现
# ---------------------------------------------------------------------------

def _scan_csv_specific_codes(csv_dir: str, max_codes: int = 8) -> List[Dict[str, Any]]:
    """扫 csv/ 目录，挑出具体合约（非 HOT / IDX）的日线 CSV，给出 stdCode 与各格式路径。"""
    if not os.path.isdir(csv_dir):
        return []
    out: List[Dict[str, Any]] = []
    for fname in sorted(os.listdir(csv_dir)):
        if not fname.endswith("_d.csv"):
            continue
        stem = fname[:-len("_d.csv")]  # SHFE.rb.2510 / SHFE.rb.HOT
        parts = stem.split(".")
        if len(parts) != 3:
            continue
        exchg, product, contract = parts
        if contract in ("HOT", "IDX") or not contract:
            continue
        # 必须是具体月份（数字）
        if not any(ch.isdigit() for ch in contract):
            continue
        out.append({
            "stdCode": stem,
            "exchg": exchg,
            "product": product,
            "contract": contract,
            "lmdb_code": f"{product}{contract}",
        })
        if len(out) >= max_codes:
            break
    return out


def build_default_codes(output_dir: str, max_codes: int = 8) -> List[Dict[str, Any]]:
    """从数据仓 csv/ 自动发现要核对的具体合约。"""
    csv_dir = os.path.join(output_dir, "csv")
    return _scan_csv_specific_codes(csv_dir, max_codes=max_codes)


# ---------------------------------------------------------------------------
# S3 端到端：可选触发新一轮 ks2wtpy 三格式输出
# ---------------------------------------------------------------------------

def _run_s3_conversion(creds: Dict[str, Any], product: str, period: str,
                       output_dir: str, ks2wtpy_dir: str,
                       timeout: int = 600) -> Dict[str, Any]:
    """subprocess 跑 main.py --s3 --full --products X --periods Y --output Z（三格式）。

    凭据通过 env 注入，不出现在命令行。返回 {ok, rc, stderr_tail}。
    """
    env = os.environ.copy()
    env["KS2WTPY_S3_ENDPOINT"] = creds["endpoint"]
    env["KS2WTPY_S3_ACCESS_KEY"] = creds["access_key"]
    env["KS2WTPY_S3_SECRET_KEY"] = creds["secret_key"]
    env["KS2WTPY_S3_BUCKET"] = creds.get("bucket", "ks-data")

    cmd = [
        sys.executable, os.path.join(ks2wtpy_dir, "main.py"),
        "--full", "--s3",
        "--products", product,
        "--periods", period,
        "--output", output_dir,
        "--log-level", "WARNING",
    ]
    try:
        proc = subprocess.run(cmd, cwd=ks2wtpy_dir, env=env,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "rc": -1, "stderr_tail": "TIMEOUT"}
    return {
        "ok": proc.returncode == 0,
        "rc": proc.returncode,
        "stderr_tail": (proc.stderr or "")[-800:],
    }


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

class S3MatrixGate:
    name = "s3_matrix"

    def __init__(self, skip_s3: bool = True, max_codes: int = 4,
                 credentials_path: Optional[str] = None,
                 run_s3_e2e: bool = False, e2e_product: str = "SHFE.rb",
                 e2e_period: str = "d", e2e_output_dir: Optional[str] = None,
                 e2e_timeout: int = 600):
        """
        @skip_s3            True = 仅本地核对（不联网）
        @max_codes          最多核对 N 个具体合约
        @credentials_path   credentials.json 路径（不打印值）
        @run_s3_e2e         True = 先用 S3 跑一轮 ks2wtpy 输出三格式再核对
        @e2e_product        端到端使用的品种（EXCHG.product）
        @e2e_period         端到端周期（d / m5 / m1）
        @e2e_output_dir     端到端输出目录；None = tempdir
        @e2e_timeout        端到端超时秒
        """
        self.skip_s3 = skip_s3
        self.max_codes = max_codes
        self.credentials_path = credentials_path
        self.run_s3_e2e = run_s3_e2e
        self.e2e_product = e2e_product
        self.e2e_period = e2e_period
        self.e2e_output_dir = e2e_output_dir
        self.e2e_timeout = e2e_timeout

    def _compare_for_code(self, output_dir: str, code_info: Dict[str, Any],
                          period: str = "day") -> Dict[str, Any]:
        exchg = code_info["exchg"]
        product = code_info["product"]
        contract = code_info["contract"]
        std = code_info["stdCode"]
        lmdb_code = code_info["lmdb_code"]
        is_day = (period == "day")

        csv_name = f"{std}_d.csv" if is_day else f"{std}_m5.csv"
        csv_path = os.path.join(output_dir, "csv", csv_name)
        dsb_path = os.path.join(output_dir, "his", period, exchg,
                                f"{exchg}.{lmdb_code}.dsb")
        lmdb_dir = os.path.join(output_dir, "lmdb", period, exchg)

        csv_bars = extract_csv_bars(csv_path, is_day=is_day)
        dsb_bars = extract_dsb_bars(dsb_path)
        lmdb_bars = extract_lmdb_bars(lmdb_dir, code_filter=lmdb_code)

        cmp = compare_triples(csv=csv_bars, lmdb=lmdb_bars, dsb=dsb_bars)
        cmp["stdCode"] = std
        cmp["period"] = period
        cmp["paths"] = {"csv": csv_path, "dsb": dsb_path, "lmdb": lmdb_dir}
        return cmp

    def run(self, output_dir: str) -> GateResult:
        details: Dict[str, Any] = {"output_dir": output_dir,
                                   "skip_s3": self.skip_s3,
                                   "run_s3_e2e": self.run_s3_e2e}

        # 端到端：先用 S3 跑一轮 ks2wtpy
        e2e_dir = None
        if self.run_s3_e2e and not self.skip_s3:
            creds = _resolve_s3_credentials(self.credentials_path)
            if not creds:
                return GateResult(self.name, "skip",
                                  "S3 e2e 已开但凭据缺失（env / credentials.json）",
                                  details)
            details["s3_credentials_source"] = creds["source"]
            details["s3_endpoint_present"] = bool(creds["endpoint"])
            e2e_dir = self.e2e_output_dir or tempfile.mkdtemp(prefix="ks2wtpy_s3_matrix_")
            ks2wtpy_dir = str(Path(__file__).resolve().parent.parent)
            run_info = _run_s3_conversion(
                creds, product=self.e2e_product, period=self.e2e_period,
                output_dir=e2e_dir, ks2wtpy_dir=ks2wtpy_dir,
                timeout=self.e2e_timeout,
            )
            details["s3_e2e_run"] = run_info
            details["s3_e2e_output_dir"] = e2e_dir
            if not run_info["ok"]:
                return GateResult(self.name, "fail",
                                  f"S3 e2e 转换失败 rc={run_info['rc']}",
                                  details)
            # 用 e2e 产物核对（覆盖入参 output_dir）
            target_dir = e2e_dir
        else:
            target_dir = output_dir

        # 自动发现要核对的合约
        codes = build_default_codes(target_dir, max_codes=self.max_codes)
        if not codes:
            return GateResult(self.name, "skip",
                              f"未在 {target_dir}/csv 找到可核对的具体合约",
                              {**details, "target_dir": target_dir})

        # 周期：当前仅核日线（DSB/LMDB 在数据仓中 min5 通常按合约缺失较多）
        per_code_results: List[Dict[str, Any]] = []
        any_fail = False
        any_warn = False
        pass_count = 0
        for c in codes:
            r = self._compare_for_code(target_dir, c, period="day")
            per_code_results.append(r)
            if r["status"] == "fail":
                any_fail = True
            elif r["status"] == "warn":
                any_warn = True
            elif r["status"] == "pass":
                pass_count += 1

        details["target_dir"] = target_dir
        details["pairs_checked"] = len(per_code_results)
        details["per_code"] = per_code_results
        details["summary_counts"] = {
            "pass": pass_count,
            "warn": sum(1 for r in per_code_results if r["status"] == "warn"),
            "fail": sum(1 for r in per_code_results if r["status"] == "fail"),
            "skip": sum(1 for r in per_code_results if r["status"] == "skip"),
        }

        if any_fail:
            return GateResult(self.name, "fail",
                              f"跨格式核对失败 {details['summary_counts']['fail']}/{len(per_code_results)}",
                              details)
        if any_warn:
            return GateResult(self.name, "warn",
                              f"跨格式核对告警 {details['summary_counts']['warn']}/{len(per_code_results)}",
                              details)
        if pass_count == 0:
            return GateResult(self.name, "skip",
                              "无任何合约可完成三方核对", details)
        return GateResult(self.name, "pass",
                          f"{pass_count}/{len(per_code_results)} 合约三格式严格一致",
                          details)
