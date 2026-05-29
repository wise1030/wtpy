"""Acceptance gates — 每个 gate 输出 GateResult。

GateResult 统一形态：
  status: "pass" | "warn" | "fail" | "skip"
  summary: 单行人类可读
  details: 结构化细节（写入 acceptance_report.json）

设计原则：
  - gate 之间互相独立、可分别运行
  - 任何 IO 失败 → status="fail" + 错误进 details，不抛
  - 不读取/打印任何凭据；不写入 manifest 之外的源数据
"""
from __future__ import annotations

import csv
import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 软错误阈值（warn）；硬错误任一非零即 fail
_HARD_QUALITY_KEYS = (
    "duplicates", "ohlc_errors", "negative_fields",
    "parse_errors", "out_of_order",
)
_SOFT_QUALITY_KEYS = (
    "time_gaps", "price_anomalies", "open_interest_jumps",
    "day_minute_date_mismatches",
)

# ops-2 [P1]: quality 白名单 — 价差合约 + 复权 + 合成合约的"异常"是规则误伤
# 详见 README §11 acceptance + 实盘 gate 章节
_WHITELIST_PREFIXES = (
    "CZCE.SPD ",  # 郑商所跨期价差
    "CZCE.IPS ",  # 郑商所跨品种价差
    "GFEX.SP ",   # 广期所价差
    "SHFE.SP ",   # 上期所价差 (预防)
    "DCE.SP ",    # 大商所价差 (预防)
    "INE.SP ",    # 能源中心价差 (预防)
    "CFFEX.SP ",  # 中金所价差 (预防)
)
# 文件名段含这些片段也算白名单 (复权/合成)
_WHITELIST_SUFFIXES = (
    ".HOT_",      # 复权后主力
    "_main_",     # 纯 OI 启发合成主力
    "_index_",    # 指数合成
)
# 期权衍生品 (策略组合等) — CZCE.SR605MSP5400 / CZCE.CF607CSP12000 etc
# 形如 {product}{月号3位}[CPM]SP{strike}_<period>.csv
import re as _re
_WHITELIST_OPTION_REGEX = _re.compile(
    r"^[A-Z]+\.[a-zA-Z]+\d{3,4}[CPM]SP\d+_[dm]\d*\.csv$"
)


def _is_whitelisted_file(filename: str) -> bool:
    """文件名是否在 quality 白名单 (价差合约 + 复权 + 合成 + 期权策略组合).

    豁免理由:
      - 价差: OHLC 可负、可极端跳变 (是价差本身)
      - .HOT 复权: 除权日 close/prev_close 巨变是正常的
      - _main/_index 合成: 换月点 OI/价格天然跳变
      - 期权策略组合 (MSP/CSP/PSP): 行权价跨度大 + 时间价值衰减导致
        价格序列天然不规则
    """
    if any(filename.startswith(p) for p in _WHITELIST_PREFIXES):
        return True
    if any(s in filename for s in _WHITELIST_SUFFIXES):
        return True
    if _WHITELIST_OPTION_REGEX.match(filename):
        return True
    return False


@dataclass
class GateResult:
    name: str
    status: str  # pass / warn / fail / skip
    summary: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ----------------------------------------------------------------------------
# 1. ManifestGate
# ----------------------------------------------------------------------------

class ManifestGate:
    name = "manifest"

    def run(self, output_dir: str) -> GateResult:
        path = os.path.join(output_dir, "conversion_manifest.json")
        if not os.path.isfile(path):
            return GateResult(self.name, "fail",
                              f"conversion_manifest.json 不存在: {path}",
                              {"path": path})
        try:
            with open(path, "r", encoding="utf-8") as f:
                m = json.load(f)
        except Exception as e:  # noqa: BLE001
            logger.warning("manifest 解析失败: %s", e)
            return GateResult(self.name, "fail",
                              f"manifest 解析失败: {e}", {"path": path})

        stats = m.get("stats") or {}
        errors = int(stats.get("errors", 0) or 0)
        error_details = stats.get("error_details") or []
        version_id = (m.get("access_contract") or {}).get("version_id")

        details = {
            "path": path,
            "stats": {
                "csv_codes": stats.get("csv_codes"),
                "lmdb_bars": stats.get("lmdb_bars"),
                "errors": errors,
                "error_details": error_details[:20],
            },
            "version_id": version_id,
            "mode": m.get("mode"),
            "enabled_outputs": m.get("enabled_outputs"),
        }
        if errors > 0:
            return GateResult(self.name, "fail",
                              f"manifest.stats.errors={errors} (硬失败)", details)
        return GateResult(self.name, "pass",
                          f"errors=0, csv_codes={stats.get('csv_codes')}", details)


# ----------------------------------------------------------------------------
# 2. QualityGate
# ----------------------------------------------------------------------------

class QualityGate:
    name = "quality"

    def __init__(self,
                 hard_keys=_HARD_QUALITY_KEYS,
                 soft_keys=_SOFT_QUALITY_KEYS,
                 enable_whitelist: bool = True):
        self.hard_keys = tuple(hard_keys)
        self.soft_keys = tuple(soft_keys)
        self.enable_whitelist = enable_whitelist

    def _classify_by_file(self, files: List[Dict[str, Any]]) -> Dict[str, Any]:
        """扫 files 把异常按文件归属拆为 whitelisted/actionable 两桶。"""
        from collections import Counter, defaultdict
        whitelisted_counts: Counter = Counter()
        actionable_counts: Counter = Counter()
        whitelisted_files: Dict[str, List[str]] = defaultdict(list)
        actionable_files: Dict[str, List[str]] = defaultdict(list)
        all_keys = (*self.hard_keys, *self.soft_keys)
        for f in files or []:
            fn = f.get("file", "")
            is_wl = _is_whitelisted_file(fn)
            for k in all_keys:
                c = int(f.get(k, 0) or 0)
                if c <= 0:
                    continue
                if is_wl:
                    whitelisted_counts[k] += c
                    whitelisted_files[k].append(f"{fn}={c}")
                else:
                    actionable_counts[k] += c
                    actionable_files[k].append(f"{fn}={c}")
        return {
            "whitelisted_counts": dict(whitelisted_counts),
            "actionable_counts": dict(actionable_counts),
            "whitelisted_files": {k: v[:50] for k, v in whitelisted_files.items()},
            "actionable_files": {k: v[:50] for k, v in actionable_files.items()},
        }

    def run(self, output_dir: str) -> GateResult:
        path = os.path.join(output_dir, "quality_report.json")
        if not os.path.isfile(path):
            return GateResult(self.name, "skip",
                              "quality_report.json 不存在（可能未启用 verify）",
                              {"path": path})
        try:
            with open(path, "r", encoding="utf-8") as f:
                q = json.load(f)
        except Exception as e:  # noqa: BLE001
            logger.warning("quality_report 解析失败: %s", e)
            return GateResult(self.name, "fail",
                              f"quality_report 解析失败: {e}", {"path": path})

        summary = q.get("summary") or {}
        files = q.get("files") or []

        # ops-2: 启用白名单且 files 列表非空时, 用 actionable_counts 判定
        # files 为空 → 无文件归属信息, 回退到 summary 直读 (旧 fixture 兼容)
        if self.enable_whitelist and files:
            classified = self._classify_by_file(files)
            hard_hits = [(k, classified["actionable_counts"].get(k, 0))
                         for k in self.hard_keys
                         if classified["actionable_counts"].get(k, 0) > 0]
            warnings = [(k, classified["actionable_counts"].get(k, 0))
                        for k in self.soft_keys
                        if classified["actionable_counts"].get(k, 0) > 0]
        else:
            classified = {"whitelisted_counts": {}, "actionable_counts": {},
                          "whitelisted_files": {}, "actionable_files": {}}
            hard_hits = [(k, int(summary.get(k, 0) or 0))
                         for k in self.hard_keys
                         if int(summary.get(k, 0) or 0) > 0]
            warnings = [(k, int(summary.get(k, 0) or 0))
                        for k in self.soft_keys
                        if int(summary.get(k, 0) or 0) > 0]

        details = {
            "path": path,
            "summary": {k: summary.get(k) for k in (
                "csv_files", "csv_rows", *self.hard_keys, *self.soft_keys,
                "lmdb_keys", "dsb_files", "dsb_bars",
            )},
            "hard_failures": [{"key": k, "count": c} for k, c in hard_hits],
            "warnings": [{"key": k, "count": c} for k, c in warnings],
            "whitelist_enabled": self.enable_whitelist,
            "whitelisted_counts": classified["whitelisted_counts"],
            "actionable_counts": classified["actionable_counts"],
            "whitelisted_files": classified["whitelisted_files"],
            "actionable_files": classified["actionable_files"],
        }
        if hard_hits:
            joined = ", ".join(f"{k}={c}" for k, c in hard_hits)
            return GateResult(self.name, "fail",
                              f"质量硬错误(actionable): {joined}", details)
        if warnings:
            joined = ", ".join(f"{k}={c}" for k, c in warnings)
            return GateResult(self.name, "warn",
                              f"质量软异常(actionable): {joined}", details)
        wl_total = sum(classified["whitelisted_counts"].values())
        pass_msg = (f"csv_files={summary.get('csv_files')}, "
                    f"rows={summary.get('csv_rows')}")
        if wl_total > 0:
            pass_msg += f", whitelisted={wl_total} (价差/复权/合成)"
        return GateResult(self.name, "pass", pass_msg, details)


# ----------------------------------------------------------------------------
# 3. ReadbackGate（CSV/LMDB/DSB）
# ----------------------------------------------------------------------------

_CSV_REQUIRED_COMMON = {"date", "open", "high", "low", "close",
                        "volume", "open_interest"}
# 分钟周期额外要求 time
_MINUTE_SUFFIXES = ("_m1.csv", "_m5.csv", "_m15.csv", "_m30.csv",
                    "_m60.csv", "_min1.csv", "_min5.csv")


def _expected_csv_cols(filename: str) -> set:
    f = filename.lower()
    cols = set(_CSV_REQUIRED_COMMON)
    if any(f.endswith(sfx) for sfx in _MINUTE_SUFFIXES):
        cols.add("time")
    return cols


def _parse_csv_date(v: str) -> int:
    """支持 YYYYMMDD 整数与 YYYY/MM/DD / YYYY-MM-DD 字符串。返回 YYYYMMDD int。"""
    s = (v or "").strip()
    if not s:
        raise ValueError("空日期")
    if s.isdigit() and len(s) == 8:
        return int(s)
    sep = None
    for c in ("/", "-"):
        if c in s:
            sep = c; break
    if sep is None:
        raise ValueError(f"无法识别日期格式: {s!r}")
    parts = s.split(sep)
    if len(parts) != 3:
        raise ValueError(f"日期分段错误: {s!r}")
    y, m, d = (int(p) for p in parts)
    return y * 10000 + m * 100 + d


def _parse_csv_time(v: str) -> int:
    """支持 HHMM / HHMMSS 整数与 HH:MM[:SS] 字符串。返回 HHMMSS int。"""
    s = (v or "").strip()
    if not s:
        raise ValueError("空时间")
    if s.isdigit():
        n = int(s)
        if len(s) <= 4:
            return n * 100  # HHMM → HHMM00
        return n
    parts = s.split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"时间分段错误: {s!r}")
    h = int(parts[0]); mi = int(parts[1]); se = int(parts[2]) if len(parts) == 3 else 0
    return h * 10000 + mi * 100 + se


def _read_csv_sample(path: str, max_rows: int = 5) -> Dict[str, Any]:
    """读 CSV 前 N 行做最小校验。返回 {rows, header, sample, error?}

    日线 CSV (`*_d.csv`) 没有 time 列；分钟线 (`*_m*.csv`) 必须含 time。
    date/time 同时接受 WT loader 支持的整数与分隔串两种形态。
    """
    info: Dict[str, Any] = {"path": path}
    fname = os.path.basename(path)
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            header = reader.fieldnames or []
            sample: List[Dict[str, Any]] = []
            count = 0
            for row in reader:
                count += 1
                if len(sample) < max_rows:
                    sample.append(row)
                if count > 100_000:  # 防御性短路
                    break
            info.update({"rows_seen": count, "header": header, "sample": sample})
    except Exception as e:  # noqa: BLE001
        logger.warning("CSV 读取失败: %s", e)
        info["error"] = f"read fail: {e}"
        return info

    if not info.get("header"):
        info["error"] = "缺少 header"
        return info
    expected = _expected_csv_cols(fname)
    missing = expected - set(info["header"])
    if missing:
        info["error"] = f"缺列: {sorted(missing)}"
        return info
    if info["rows_seen"] == 0:
        info["error"] = "空文件"
        return info

    has_time = "time" in info["header"]
    bad_rows = []
    for r in info["sample"]:
        try:
            o = float(r["open"]); h = float(r["high"])
            l = float(r["low"]);  c = float(r["close"])
            _parse_csv_date(r["date"])
            if has_time:
                _parse_csv_time(r["time"])
            if h < max(o, c) or l > min(o, c):
                bad_rows.append({"row": r, "why": "OHLC inconsistent"})
        except Exception as e:  # noqa: BLE001
            logger.debug("CSV 行解析异常: %s", e)
            bad_rows.append({"row": r, "why": f"parse: {e}"})
    if bad_rows:
        info["error"] = f"前 {len(info['sample'])} 行存在 {len(bad_rows)} 个解析/OHLC 错误"
        info["bad_rows"] = bad_rows
    return info


def _try_read_lmdb_keys(path: str, max_keys: int = 5) -> Dict[str, Any]:
    """尝试用 lmdb 库扫几把 key。lmdb 不可用 → skip。"""
    info: Dict[str, Any] = {"path": path}
    try:
        import lmdb  # type: ignore
    except Exception as e:  # noqa: BLE001
        logger.debug("lmdb 未安装: %s", e)
        info["status"] = "skip"
        info["reason"] = f"lmdb 未安装: {e}"
        return info
    try:
        env = lmdb.open(path, readonly=True, lock=False, max_dbs=8, subdir=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("lmdb 打开失败: %s", e)
        info["status"] = "fail"
        info["error"] = f"open lmdb fail: {e}"
        return info
    try:
        keys: List[str] = []
        total = 0
        with env.begin() as txn:
            cursor = txn.cursor()
            for k, _ in cursor:
                total += 1
                if len(keys) < max_keys:
                    try:
                        keys.append(k.decode("utf-8", errors="replace"))
                    except Exception as e:  # noqa: BLE001
                        logger.debug("lmdb key decode fallback: %s", e)
                        keys.append(repr(k))
                if total >= 1_000_000:
                    break
        info["status"] = "pass" if total > 0 else "fail"
        info["total_keys_seen"] = total
        info["sample_keys"] = keys
        if total == 0:
            info["error"] = "lmdb 为空"
    except Exception as e:  # noqa: BLE001
        logger.warning("lmdb 扫描失败: %s", e)
        info["status"] = "fail"
        info["error"] = f"scan lmdb fail: {e}"
    finally:
        env.close()
    return info


def _try_read_dsb_file(path: str) -> Dict[str, Any]:
    """读单个 .dsb 文件头若干字节，确认非空且非全零；不解析全部数据。"""
    info: Dict[str, Any] = {"path": path}
    try:
        size = os.path.getsize(path)
        info["size"] = size
        if size == 0:
            info["status"] = "fail"; info["error"] = "空文件"; return info
        with open(path, "rb") as f:
            head = f.read(min(64, size))
        info["head_hex"] = head.hex()
        if all(b == 0 for b in head):
            info["status"] = "fail"; info["error"] = "全零头部"
        else:
            info["status"] = "pass"
    except Exception as e:  # noqa: BLE001
        logger.warning("DSB 读取失败: %s", e)
        info["status"] = "fail"; info["error"] = f"read fail: {e}"
    return info


class ReadbackGate:
    name = "readback"

    def __init__(self, formats=("csv", "lmdb", "dsb"),
                 max_codes: int = 8, max_rows: int = 5):
        self.formats = tuple(formats)
        self.max_codes = max_codes
        self.max_rows = max_rows

    def run(self, output_dir: str) -> GateResult:
        details: Dict[str, Any] = {"output_dir": output_dir}
        any_fail = False
        any_pass = False

        if "csv" in self.formats:
            csv_dir = os.path.join(output_dir, "csv")
            if not os.path.isdir(csv_dir):
                details["csv"] = {"status": "skip", "reason": "csv/ 不存在"}
            else:
                files = sorted(p for p in os.listdir(csv_dir)
                               if p.endswith(".csv"))
                # 取样：HOT + 具体合约 + 全部前 N
                picked: List[str] = []
                for p in files:
                    if ".HOT_" in p and p not in picked:
                        picked.append(p)
                for p in files:
                    if p not in picked:
                        picked.append(p)
                    if len(picked) >= self.max_codes:
                        break
                checked = []
                failures = []
                for p in picked:
                    full = os.path.join(csv_dir, p)
                    res = _read_csv_sample(full, max_rows=self.max_rows)
                    entry = {"file": p, **res}
                    checked.append(entry)
                    if "error" in res:
                        failures.append(entry)
                csv_status = "fail" if failures else ("pass" if checked else "fail")
                details["csv"] = {
                    "status": csv_status,
                    "files_total": len(files),
                    "files_checked": len(checked),
                    "checked": checked,
                    "failures": failures,
                }
                if csv_status == "pass":
                    any_pass = True
                else:
                    any_fail = True

        if "lmdb" in self.formats:
            lmdb_root = os.path.join(output_dir, "lmdb")
            if not os.path.isdir(lmdb_root):
                details["lmdb"] = {"status": "skip", "reason": "lmdb/ 不存在"}
            else:
                # WtDataKit 约定 lmdb/his/min5/SHFE/* 子目录里有 data.mdb
                envs = []
                for root, dirs, files in os.walk(lmdb_root):
                    if "data.mdb" in files:
                        envs.append(root)
                        if len(envs) >= self.max_codes:
                            break
                if not envs:
                    details["lmdb"] = {"status": "skip",
                                       "reason": f"{lmdb_root} 下未找到 data.mdb"}
                else:
                    sub = [_try_read_lmdb_keys(e, max_keys=self.max_rows)
                           for e in envs]
                    statuses = {s.get("status", "fail") for s in sub}
                    lmdb_status = ("fail" if "fail" in statuses
                                   else ("pass" if "pass" in statuses else "skip"))
                    details["lmdb"] = {"status": lmdb_status, "envs": sub}
                    if lmdb_status == "pass":
                        any_pass = True
                    elif lmdb_status == "fail":
                        any_fail = True

        if "dsb" in self.formats:
            dsb_root = os.path.join(output_dir, "his")
            if not os.path.isdir(dsb_root):
                # 也可能直接放在 output_dir/dsb/
                alt = os.path.join(output_dir, "dsb")
                dsb_root = alt if os.path.isdir(alt) else dsb_root
            if not os.path.isdir(dsb_root):
                details["dsb"] = {"status": "skip",
                                  "reason": "his/ 与 dsb/ 都不存在"}
            else:
                dsb_files = []
                for root, _dirs, files in os.walk(dsb_root):
                    for f in files:
                        if f.endswith(".dsb"):
                            dsb_files.append(os.path.join(root, f))
                            if len(dsb_files) >= self.max_codes:
                                break
                    if len(dsb_files) >= self.max_codes:
                        break
                if not dsb_files:
                    details["dsb"] = {"status": "skip",
                                      "reason": f"{dsb_root} 下未找到 .dsb"}
                else:
                    sub = [_try_read_dsb_file(p) for p in dsb_files]
                    statuses = {s.get("status", "fail") for s in sub}
                    dsb_status = ("fail" if "fail" in statuses
                                  else ("pass" if "pass" in statuses else "skip"))
                    details["dsb"] = {"status": dsb_status, "files": sub}
                    if dsb_status == "pass":
                        any_pass = True
                    elif dsb_status == "fail":
                        any_fail = True

        if any_fail:
            return GateResult(self.name, "fail",
                              "存在读回失败，见 details.failures", details)
        if not any_pass:
            return GateResult(self.name, "skip",
                              "无可读回格式", details)
        return GateResult(self.name, "pass",
                          "全部已检格式读回成功", details)


# ----------------------------------------------------------------------------
# 4. PytestGate / PyCompileGate
# ----------------------------------------------------------------------------

class PytestGate:
    name = "regression"

    def __init__(self, test_path: Optional[str] = None,
                 extra_args: Optional[List[str]] = None,
                 timeout: int = 600):
        self.test_path = test_path
        self.extra_args = list(extra_args or [])
        self.timeout = timeout

    def run(self, ks2wtpy_dir: str) -> GateResult:
        cmd = [sys.executable, "-m", "pytest", "-q"]
        if self.test_path:
            cmd.append(self.test_path)
        else:
            cmd.append("tests/")
        cmd.extend(self.extra_args)
        try:
            proc = subprocess.run(cmd, cwd=ks2wtpy_dir,
                                  capture_output=True, text=True,
                                  timeout=self.timeout)
        except subprocess.TimeoutExpired as e:  # noqa: BLE001
            logger.warning("pytest 超时: %s", e)
            return GateResult(self.name, "fail",
                              f"pytest 超时 ({self.timeout}s)",
                              {"cmd": cmd, "stdout": (e.stdout or "")[-4000:]})
        tail_out = (proc.stdout or "")[-4000:]
        tail_err = (proc.stderr or "")[-2000:]
        details = {
            "cmd": cmd,
            "returncode": proc.returncode,
            "stdout_tail": tail_out,
            "stderr_tail": tail_err,
        }
        if proc.returncode == 0:
            return GateResult(self.name, "pass",
                              tail_out.strip().splitlines()[-1] if tail_out.strip() else "pytest pass",
                              details)
        return GateResult(self.name, "fail",
                          f"pytest 非零退出: rc={proc.returncode}", details)


class PyCompileGate:
    name = "pycompile"

    def __init__(self, files: Optional[List[str]] = None, timeout: int = 120):
        self.files = files
        self.timeout = timeout

    def run(self, ks2wtpy_dir: str) -> GateResult:
        if self.files:
            targets = self.files
        else:
            targets = sorted(
                f for f in os.listdir(ks2wtpy_dir)
                if f.endswith(".py") and os.path.isfile(os.path.join(ks2wtpy_dir, f))
            )
        cmd = [sys.executable, "-m", "py_compile", *targets]
        try:
            proc = subprocess.run(cmd, cwd=ks2wtpy_dir,
                                  capture_output=True, text=True,
                                  timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return GateResult(self.name, "fail",
                              f"py_compile 超时 ({self.timeout}s)",
                              {"cmd": cmd})
        details = {"cmd": cmd, "returncode": proc.returncode,
                   "stderr_tail": (proc.stderr or "")[-2000:]}
        if proc.returncode == 0:
            return GateResult(self.name, "pass",
                              f"{len(targets)} 个文件编译通过", details)
        return GateResult(self.name, "fail",
                          f"py_compile 失败 rc={proc.returncode}", details)
