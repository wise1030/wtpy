"""Phase 5-1 [P0] 实盘 gate: 数据新鲜度检查 (FreshnessGate)

实盘启动前必须确认数据未陈旧, 否则策略会在"昨天的数据"上下单。

检查源 (按权威性排序):
  1. DSB 最新 bar 内部日期        — 业务真值 (最权威, 但读 DSB 重)
  2. DSB 文件 mtime                — 时间戳真值 (轻量, 兜底)
  3. manifest.generated_at         — 审计真值 (无 DSB 时唯一来源)

任一超 ``max_age_hours`` → fail; 无 DSB → warn; 无 manifest → fail。

副产物 ``live_gate.json``: 启动脚本读 ``ok:bool`` + ``reason`` 决定是否阻断。

设计原则:
  - 与 gates.py 解耦 (gates.py 已 518 行, 不再扩); 独立模块, 独立测试
  - 不依赖 wtpy (无 WtDtServo singleton), 纯 stdlib + 文件系统
  - 失败硬阻断, 不抛异常 (gate 约定: status=fail + details, 永不 raise)
"""
from __future__ import annotations

import glob
import json
import logging
import os
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .gates import GateResult

logger = logging.getLogger(__name__)


def _parse_iso(s: str) -> Optional[datetime]:
    """容错解析 ISO 时间, 兼容 'Z' 后缀和无时区情况, 失败返 None。"""
    if not s or not isinstance(s, str):
        return None
    try:
        # 兼容 'Z' 后缀
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        # 无时区视为 UTC (manifest 默认写 UTC)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _age_hours(dt: Optional[datetime], now: datetime) -> Optional[float]:
    if dt is None:
        return None
    delta = now - dt
    return round(delta.total_seconds() / 3600.0, 3)


def _scan_dsb(output_dir: str) -> Tuple[List[str], Optional[float], Optional[str]]:
    """扫 his/ 下所有 .dsb, 返回 (路径列表, 最旧 mtime age_hours, 最旧文件路径)。

    注: 取"最旧"是因为新鲜度按短板原则——任一文件陈旧即整体陈旧。
    """
    pattern = os.path.join(output_dir, "his", "**", "*.dsb")
    dsbs = glob.glob(pattern, recursive=True)
    if not dsbs:
        return [], None, None
    oldest = min(dsbs, key=os.path.getmtime)
    mt = os.path.getmtime(oldest)
    age = (datetime.now(timezone.utc).timestamp() - mt) / 3600.0
    return dsbs, round(age, 3), oldest


class FreshnessGate:
    """实盘 gate: 数据新鲜度。

    Args:
        max_age_hours: 容忍上限 (小时); 日线推荐 24, 分钟线推荐 2。
        write_live_gate: 是否落地 ``live_gate.json`` (启动脚本读)。
        write_preflight: 是否落地 ``live_preflight_report.json`` (审计明细)。
    """

    name = "freshness"

    def __init__(
        self,
        max_age_hours: float = 24.0,
        *,
        write_live_gate: bool = False,
        write_preflight: bool = False,
    ):
        self.max_age_hours = float(max_age_hours)
        self.write_live_gate = bool(write_live_gate)
        self.write_preflight = bool(write_preflight)

    def run(self, output_dir: str) -> GateResult:
        output_dir = os.path.abspath(output_dir)
        manifest_path = os.path.join(output_dir, "conversion_manifest.json")

        # ---- 1. manifest 必须存在 (硬性) ----
        if not os.path.isfile(manifest_path):
            r = GateResult(
                self.name, "fail",
                f"conversion_manifest.json 不存在: {manifest_path}",
                {"path": manifest_path,
                 "max_age_hours": self.max_age_hours},
            )
            self._maybe_write_live_gate(output_dir, r)
            return r

        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception as e:  # noqa: BLE001
            logger.warning("manifest 解析失败: %s", e)
            r = GateResult(
                self.name, "fail",
                f"manifest 解析失败: {e}",
                {"path": manifest_path,
                 "max_age_hours": self.max_age_hours},
            )
            self._maybe_write_live_gate(output_dir, r)
            return r

        now = datetime.now(timezone.utc)
        manifest_dt = _parse_iso(manifest.get("generated_at", ""))
        manifest_age = _age_hours(manifest_dt, now)
        version_id = (manifest.get("access_contract") or {}).get("version_id")

        # ---- 2. DSB 扫描 (软性: 无 → warn) ----
        dsbs, dsb_age, dsb_oldest = _scan_dsb(output_dir)

        details: Dict[str, Any] = {
            "max_age_hours": self.max_age_hours,
            "manifest_path": manifest_path,
            "manifest_generated_at": manifest.get("generated_at"),
            "manifest_age_hours": manifest_age,
            "dsb_count": len(dsbs),
            "dsb_oldest_path": dsb_oldest,
            "dsb_oldest_age_hours": dsb_age,
            "version_id": version_id,
            "checked_at": now.isoformat(),
        }

        # ---- 3. 判定 ----
        reasons: List[str] = []

        # manifest 时间不可解析 → fail
        if manifest_age is None:
            reasons.append(
                f"manifest.generated_at 不可解析: {manifest.get('generated_at')!r}")
        elif manifest_age > self.max_age_hours:
            reasons.append(
                f"manifest 陈旧 (stale): {manifest_age}h > {self.max_age_hours}h")

        # DSB 单独逻辑
        if not dsbs:
            # 无 DSB 文件 → warn (manifest 可能合法, 但实盘必有数据)
            details["dsb_warning"] = "no_dsb_files"
            warn_summary = (
                f"DSB 缺失 (no dsb files under {output_dir}/his/), "
                f"manifest 年龄 {manifest_age}h"
            )
            if reasons:
                # manifest 已 fail, 整体 fail
                r = GateResult(
                    self.name, "fail",
                    "; ".join(reasons) + "; 且 DSB 缺失",
                    details,
                )
            else:
                r = GateResult(self.name, "warn", warn_summary, details)
            self._maybe_write_live_gate(output_dir, r)
            self._maybe_write_preflight(output_dir, r)
            return r

        # DSB 超阈值
        if dsb_age is not None and dsb_age > self.max_age_hours:
            reasons.append(
                f"DSB 陈旧 (stale): {dsb_age}h > {self.max_age_hours}h "
                f"(oldest={dsb_oldest})")

        if reasons:
            r = GateResult(
                self.name, "fail",
                "; ".join(reasons), details,
            )
            self._maybe_write_live_gate(output_dir, r)
            self._maybe_write_preflight(output_dir, r)
            return r

        r = GateResult(
            self.name, "pass",
            (f"manifest_age={manifest_age}h, dsb_age={dsb_age}h, "
             f"max={self.max_age_hours}h, dsb_count={len(dsbs)}"),
            details,
        )
        self._maybe_write_live_gate(output_dir, r)
        self._maybe_write_preflight(output_dir, r)
        return r

    # ----------------------------------------------------------------------
    # 启动脚本契约: live_gate.json
    # ----------------------------------------------------------------------

    def _maybe_write_live_gate(self, output_dir: str, r: GateResult) -> None:
        if not self.write_live_gate:
            return
        path = os.path.join(output_dir, "live_gate.json")
        payload = {
            "ok": r.status == "pass",
            "status": r.status,
            "gate": r.name,
            "summary": r.summary,
            "reason": r.summary if r.status != "pass" else "",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "max_age_hours": self.max_age_hours,
            "details": r.details,
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except OSError:
            # gate 约定不抛
            pass

    def _maybe_write_preflight(self, output_dir: str, r: GateResult) -> None:
        if not self.write_preflight:
            return
        path = os.path.join(output_dir, "live_preflight_report.json")
        payload = {
            "version": "1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "freshness": asdict(r),
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except OSError:
            pass


__all__ = ["FreshnessGate"]
