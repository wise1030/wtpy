"""Phase 5-2 [P0] 实盘 gate: 当日完整性检查 (CompletenessGate)

实盘启动前必须确认: 应有的合成合约 (_main / _index) 实际全部落地。

口径 (用户拍板):
  - 应有清单: manifest.filters.specific_products → 每品种 _main + _index 各一份
    (实盘策略基本只跑 _main, 原始合约不在 preflight 关键路径)
  - 阈值: 缺失率 0%=pass, >0 且 ≤warn_pct=warn, >fail_pct=fail
  - 节假日: holidays.json 顶层 CHINA 数组; 今日在内 → 整体 skip (不阻断)

设计:
  - 与 freshness gate 独立, 互不污染
  - 失败硬阻断, 不抛异常
  - 输出 details.missing: 列表形式 [EXCHG.code_suffix, ...] 便于排障

注: 此 gate 不检查 bar 内部日期完整性 (那是 p2-x quality 范畴), 只检查
"合约文件是否落地"。深层日期/缺口检查由 QualityGate 的 time_gaps 负责。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from .gates import GateResult

logger = logging.getLogger(__name__)


# 默认阈值: 0=pass / ≤5%=warn / >5%=fail
_DEFAULT_WARN_PCT = 5.0
_DEFAULT_FAIL_PCT = 5.0  # > fail_pct → fail


def _load_manifest(output_dir: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    path = os.path.join(output_dir, "conversion_manifest.json")
    if not os.path.isfile(path):
        return None, f"conversion_manifest.json 不存在: {path}"
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except Exception as e:  # noqa: BLE001
        logger.warning("manifest 解析失败: %s", e)
        return None, f"manifest 解析失败: {e}"


def _load_holidays(output_dir: str) -> Set[str]:
    """读 common/holidays.json 顶层 CHINA 数组, 返回 YYYYMMDD set。失败返空。"""
    path = os.path.join(output_dir, "common", "holidays.json")
    if not os.path.isfile(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        china = data.get("CHINA") or []
        return {str(d) for d in china if d}
    except Exception as e:  # noqa: BLE001
        logger.warning("holidays.json 加载失败: %s", e)
        return set()


def _expected_codes(specific_products: Dict[str, List[str]]) -> List[str]:
    """每品种 _main + _index 各一份, 形如 'SHFE.ag_main' / 'DCE.m_index'。"""
    out: List[str] = []
    for exch, products in (specific_products or {}).items():
        for prod in products or []:
            out.append(f"{exch}.{prod}_main")
            out.append(f"{exch}.{prod}_index")
    return sorted(out)


def _found_dsb_codes(output_dir: str) -> Set[str]:
    """扫 his/<period>/<EXCHG>/<code>.dsb, 返回 {EXCHG.code} set (只取 _main/_index)。"""
    found: Set[str] = set()
    his_root = os.path.join(output_dir, "his")
    if not os.path.isdir(his_root):
        return found
    for period_dir in os.listdir(his_root):
        period_path = os.path.join(his_root, period_dir)
        if not os.path.isdir(period_path):
            continue
        for exch in os.listdir(period_path):
            exch_path = os.path.join(period_path, exch)
            if not os.path.isdir(exch_path):
                continue
            for fname in os.listdir(exch_path):
                if not fname.endswith(".dsb"):
                    continue
                code = fname[:-4]  # 去 .dsb
                if code.endswith("_main") or code.endswith("_index"):
                    found.add(f"{exch}.{code}")
    return found


class CompletenessGate:
    """实盘 gate: 当日完整性 (合成合约是否齐)。

    Args:
        warn_pct: 缺失率 ≤ 此值 (但 >0) → warn; 默认 5.0
        fail_pct: 缺失率 > 此值 → fail; 默认 5.0
        today: 仅测试用; None 表示用今天日期
    """

    name = "completeness"

    def __init__(
        self,
        warn_pct: float = _DEFAULT_WARN_PCT,
        fail_pct: float = _DEFAULT_FAIL_PCT,
        *,
        today: Optional[date] = None,
    ):
        self.warn_pct = float(warn_pct)
        self.fail_pct = float(fail_pct)
        self._today = today

    def _check_holiday(self, output_dir: str) -> Optional[str]:
        """今日是假日返 YYYYMMDD, 否则 None。"""
        today = self._today or date.today()
        today_str = today.strftime("%Y%m%d")
        holidays = _load_holidays(output_dir)
        return today_str if today_str in holidays else None

    def run(self, output_dir: str) -> GateResult:
        output_dir = os.path.abspath(output_dir)
        manifest, err = _load_manifest(output_dir)
        if manifest is None:
            return GateResult(self.name, "fail", err or "manifest 加载失败",
                              {"output_dir": output_dir})

        # 1. 假日 → skip
        holiday_today = self._check_holiday(output_dir)
        if holiday_today is not None:
            return GateResult(
                self.name, "skip",
                f"今日为节假日 {holiday_today} (holidays.json), 跳过完整性检查",
                {"output_dir": output_dir, "holiday": holiday_today},
            )

        # 2. 应有清单 (口径 a: specific_products → _main + _index)
        filters = manifest.get("filters") or {}
        sp = filters.get("specific_products") or {}
        if not sp:
            return GateResult(
                self.name, "skip",
                "manifest.filters.specific_products 为空, 无应有清单可对比 (empty)",
                {"output_dir": output_dir, "filters": filters},
            )
        expected = _expected_codes(sp)
        expected_set = set(expected)

        # 3. 实际产出
        found = _found_dsb_codes(output_dir)
        # 只看交集范围 (期望集合)
        found_in_scope = found & expected_set
        missing_list = sorted(expected_set - found)
        extra_list = sorted(found - expected_set)  # 实际多余项 (诊断用)

        expected_count = len(expected_set)
        found_count = len(found_in_scope)
        missing_count = len(missing_list)
        miss_pct = (missing_count / expected_count * 100.0) if expected_count else 0.0
        miss_pct = round(miss_pct, 3)

        details: Dict[str, Any] = {
            "output_dir": output_dir,
            "expected_count": expected_count,
            "found_count": found_count,
            "missing_count": missing_count,
            "missing_pct": miss_pct,
            "warn_pct": self.warn_pct,
            "fail_pct": self.fail_pct,
            "missing": missing_list[:200],  # 截断防爆
            "extra_count": len(extra_list),
            "extra_sample": extra_list[:20],
            "specific_products": sp,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

        # 4. 判定
        if missing_count == 0:
            return GateResult(
                self.name, "pass",
                f"完整: {found_count}/{expected_count} (_main+_index 全到位)",
                details,
            )
        if miss_pct > self.fail_pct:
            return GateResult(
                self.name, "fail",
                (f"缺失 {missing_count}/{expected_count}={miss_pct}% "
                 f"超 fail 阈值 {self.fail_pct}% (硬阻断)"),
                details,
            )
        # 0 < miss_pct ≤ warn_pct
        return GateResult(
            self.name, "warn",
            (f"缺失 {missing_count}/{expected_count}={miss_pct}% "
             f"在 warn 区间 (≤{self.warn_pct}%)"),
            details,
        )


__all__ = ["CompletenessGate"]
