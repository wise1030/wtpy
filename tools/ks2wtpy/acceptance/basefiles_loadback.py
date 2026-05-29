"""O2 [P0] acceptance gate: basefiles_loadback

用 wtpy 原生 ContractMgr/SessionMgr/ProductMgr 真正 load 一遍输出仓的 common/
配置三件套, 并交叉校验关键引用完整性。这是 WT 启动 (WtBtEngine/WtDtServo init)
前的最后一道闸门: ks2wtpy 生成的 basefiles 如果不能被 wtpy 加载, 任何上游
gate 通过都没意义。

校验项 (硬阻断):
  1) common/ 目录存在
  2) commodities.json / sessions.json 文件存在 (contracts/fees 可空)
  3) wtpy.ProductMgr.load(commodities.json) 不抛异常 + 至少 1 个品种
  4) wtpy.SessionMgr.load(sessions.json) 不抛异常 + 至少 1 个 session
  5) wtpy.ContractMgr.load(contracts.json) 不抛 (允许空 contracts)
  6) commodities[*].session 全部存在于 sessions.json (dangling_sessions = [])
  7) commodities[*].holiday 全部存在于 holidays.json 顶层 key
     (默认 "CHINA"; 缺 holidays.json 即 fail)
  8) contracts[*].product 全部存在于 commodities.json (dangling_products = [])

非阻断 (info-only, 仅记 details):
  - sessions.json 里未被 commodities 引用的 session ID (orphan_sessions)
  - fees.json 里指向不存在 product/contract 的 key (orphan_fees)

设计:
  - 与现有 gate 风格一致 (GateResult 单返回)
  - 不抛, 任何异常 -> fail
  - 不读 / 不打印任何凭据 (basefiles 不含敏感数据, 但 details 仍只列 key 名)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .gates import GateResult

logger = logging.getLogger(__name__)


def _ensure_wtpy_on_path() -> None:
    """ks2wtpy 在 wtpy/tools/ 下, wtpy 包在父父父级; 通常已可 import,
    保险起见做一次软注入。
    """
    try:
        import wtpy  # noqa: F401
        return
    except ImportError:
        pass
    # acceptance/ -> ks2wtpy/ -> tools/ -> wtpy_repo/
    here = Path(__file__).resolve()
    wtpy_repo = here.parents[3]
    if (wtpy_repo / "wtpy" / "__init__.py").is_file():
        if str(wtpy_repo) not in sys.path:
            sys.path.insert(0, str(wtpy_repo))


def _load_json(path: Path) -> Tuple[Optional[Any], Optional[str]]:
    if not path.is_file():
        return None, f"file_missing:{path.name}"
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f), None
    except UnicodeDecodeError:
        # ks2wtpy 实际写 utf-8, 但 wtpy 原生 loader 用 chardet; 兼容
        try:
            import chardet
            raw = path.read_bytes()
            enc = chardet.detect(raw[:500]).get("encoding") or "utf-8"
            return json.loads(raw.decode(enc, errors="replace")), None
        except Exception as e:  # noqa: BLE001
            logger.warning("JSON 解码失败 %s: %s", path.name, e)
            return None, f"decode_error:{path.name}:{e}"
    except Exception as e:  # noqa: BLE001
        logger.warning("JSON 解析失败 %s: %s", path.name, e)
        return None, f"parse_error:{path.name}:{e}"


def _collect_commodity_refs(commodities: Dict[str, Any]) -> Tuple[
    Set[str], Set[str], List[Tuple[str, str]]
]:
    """从 commodities 抽 (引用的 session ID 集, 引用的 holiday key 集,
    [(EXCHG.product, session), ...])。"""
    sessions: Set[str] = set()
    holidays: Set[str] = set()
    pairs: List[Tuple[str, str]] = []
    for exch, products in (commodities or {}).items():
        if not isinstance(products, dict):
            continue
        for pid, pobj in products.items():
            if not isinstance(pobj, dict):
                continue
            sid = pobj.get("session")
            if sid:
                sessions.add(str(sid))
                pairs.append((f"{exch}.{pid}", str(sid)))
            hkey = pobj.get("holiday")
            if hkey:
                holidays.add(str(hkey))
    return sessions, holidays, pairs


def _collect_contract_products(contracts: Dict[str, Any]) -> Set[str]:
    """从 contracts 抽出所有 product 字段。"""
    out: Set[str] = set()
    for exch, codes in (contracts or {}).items():
        if not isinstance(codes, dict):
            continue
        for code, cobj in codes.items():
            if not isinstance(cobj, dict):
                continue
            prod = cobj.get("product")
            if prod:
                out.add(f"{exch}.{prod}")
    return out


class BasefilesLoadbackGate:
    """用 wtpy 原生 loader 验证 ks2wtpy 输出的 common/ 三件套可用性。"""

    name = "basefiles_loadback"

    def __init__(
        self,
        *,
        default_holiday_key: str = "CHINA",
        require_contracts: bool = False,
    ):
        self.default_holiday_key = default_holiday_key
        self.require_contracts = require_contracts

    def run(self, output_dir: str) -> GateResult:
        output_dir = os.path.abspath(output_dir)
        common = Path(output_dir) / "common"
        details: Dict[str, Any] = {
            "output_dir": output_dir,
            "common_dir": str(common),
            "loaded_via": "wtpy.ContractMgr+SessionMgr+ProductMgr",
            "commodities_count": 0,
            "sessions_count": 0,
            "contracts_count": 0,
            "dangling_sessions": [],
            "dangling_holiday_keys": [],
            "dangling_products": [],
            "orphan_sessions": [],
            "orphan_fees": [],
            "errors": [],
        }

        # ── 1) common/ 目录 ──
        if not common.is_dir():
            details["errors"].append(f"common/ 目录不存在: {common}")
            return GateResult(self.name, "fail",
                              f"common/ 目录不存在 (missing): {common}",
                              details)

        # ── 2) 必备文件 ──
        commodities_path = common / "commodities.json"
        sessions_path = common / "sessions.json"
        contracts_path = common / "contracts.json"
        fees_path = common / "fees.json"
        holidays_path = common / "holidays.json"

        if not commodities_path.is_file():
            details["errors"].append("commodities.json 缺失")
            return GateResult(self.name, "fail",
                              "commodities.json 缺失 (WT 无法 init)",
                              details)
        if not sessions_path.is_file():
            details["errors"].append("sessions.json 缺失")
            return GateResult(self.name, "fail",
                              "sessions.json 缺失 (commodities.session 无引用源)",
                              details)

        # ── 3) JSON 解析 (用于交叉校验; loader 调用走 wtpy) ──
        commodities, err = _load_json(commodities_path)
        if err:
            details["errors"].append(err)
            return GateResult(self.name, "fail",
                              f"commodities.json 解析失败: {err}", details)
        sessions_json, err = _load_json(sessions_path)
        if err:
            details["errors"].append(err)
            return GateResult(self.name, "fail",
                              f"sessions.json 解析失败: {err}", details)
        contracts_json: Dict[str, Any] = {}
        if contracts_path.is_file():
            tmp, err = _load_json(contracts_path)
            if err:
                details["errors"].append(err)
                return GateResult(self.name, "fail",
                                  f"contracts.json 解析失败: {err}", details)
            contracts_json = tmp or {}
        elif self.require_contracts:
            details["errors"].append("contracts.json 缺失 (require_contracts=True)")
            return GateResult(self.name, "fail",
                              "contracts.json 缺失", details)
        fees_json: Dict[str, Any] = {}
        if fees_path.is_file():
            tmp, err = _load_json(fees_path)
            if err:
                # fees 解析失败仅记不阻断
                details["errors"].append(f"fees.json 解析失败 (非阻断): {err}")
            else:
                fees_json = tmp or {}

        # ── 4) wtpy 原生 loader 真跑一遍 ──
        _ensure_wtpy_on_path()
        try:
            from wtpy.ProductMgr import ProductMgr
            from wtpy.SessionMgr import SessionMgr
            from wtpy.ContractMgr import ContractMgr
        except Exception as e:  # noqa: BLE001
            logger.warning("import wtpy 失败: %s", e)
            details["errors"].append(f"import wtpy 失败: {e}")
            return GateResult(self.name, "fail",
                              f"无法 import wtpy 原生 loader: {e}", details)

        try:
            pmgr = ProductMgr()
            pmgr.load(str(commodities_path))
            # 注意: __products__ 首尾都是双下划线 (dunder), Python 不做 name
            # mangling, 直接是 instance attr。
            commodities_count = len(getattr(pmgr, "__products__", {}) or {})
            details["commodities_count"] = commodities_count
        except Exception as e:  # noqa: BLE001
            logger.warning("ProductMgr.load 失败: %s", e)
            tb = traceback.format_exc(limit=4)
            details["errors"].append(f"ProductMgr.load 抛异常: {e}")
            details["product_load_traceback"] = tb
            return GateResult(self.name, "fail",
                              f"wtpy.ProductMgr 无法加载 commodities.json: {e}",
                              details)

        try:
            smgr = SessionMgr()
            smgr.load(str(sessions_path))
            sessions_loaded = getattr(smgr, "__sessions__", {}) or {}
            sessions_count = len(sessions_loaded)
            details["sessions_count"] = sessions_count
        except Exception as e:  # noqa: BLE001
            logger.warning("SessionMgr.load 失败: %s", e)
            tb = traceback.format_exc(limit=4)
            details["errors"].append(f"SessionMgr.load 抛异常: {e}")
            details["session_load_traceback"] = tb
            return GateResult(self.name, "fail",
                              f"wtpy.SessionMgr 无法加载 sessions.json: {e}",
                              details)

        contracts_count = 0
        if contracts_path.is_file():
            try:
                cmgr = ContractMgr()
                cmgr.load(str(contracts_path))
                # ContractMgr.getTotalCodes() 返回 stdCode 列表, 不是 int。
                total_codes = cmgr.getTotalCodes() or []
                contracts_count = len(total_codes)
                details["contracts_count"] = contracts_count
            except Exception as e:  # noqa: BLE001
                logger.warning("ContractMgr.load 失败: %s", e)
                tb = traceback.format_exc(limit=4)
                details["errors"].append(f"ContractMgr.load 抛异常: {e}")
                details["contract_load_traceback"] = tb
                return GateResult(self.name, "fail",
                                  f"wtpy.ContractMgr 无法加载 contracts.json: {e}",
                                  details)

        # ── 5) 计数边界 ──
        if commodities_count == 0:
            details["errors"].append("commodities 加载后为 0 (loader 不抛但无产出)")
            return GateResult(self.name, "fail",
                              "commodities.json load 后 0 品种 (空或结构错)",
                              details)
        if sessions_count == 0:
            details["errors"].append("sessions 加载后为 0")
            return GateResult(self.name, "fail",
                              "sessions.json load 后 0 session", details)

        # ── 6) 交叉引用: session ──
        ref_sessions, ref_holidays, prod_session_pairs = _collect_commodity_refs(
            commodities or {})
        sessions_keys = set((sessions_json or {}).keys())
        dangling_sessions = sorted(ref_sessions - sessions_keys)
        details["dangling_sessions"] = dangling_sessions
        # 孤立 session (非阻断)
        details["orphan_sessions"] = sorted(sessions_keys - ref_sessions)

        # ── 7) 交叉引用: holiday key ──
        # holidays.json 缺失但有 ref → fail; 存在则查 top-level keys
        holidays_keys: Set[str] = set()
        if ref_holidays:
            if not holidays_path.is_file():
                details["dangling_holiday_keys"] = sorted(ref_holidays)
                details["errors"].append(
                    f"commodities.holiday 引用了 {sorted(ref_holidays)} "
                    f"但 holidays.json 不存在")
                return GateResult(
                    self.name, "fail",
                    f"holidays.json 缺失但 commodities 引用了 holiday "
                    f"{sorted(ref_holidays)}",
                    details,
                )
            holidays_data, err = _load_json(holidays_path)
            if err:
                details["errors"].append(f"holidays.json 解析失败: {err}")
                return GateResult(self.name, "fail",
                                  f"holidays.json 解析失败: {err}", details)
            if isinstance(holidays_data, dict):
                holidays_keys = set(holidays_data.keys())
            dangling_holiday = sorted(ref_holidays - holidays_keys)
            details["dangling_holiday_keys"] = dangling_holiday
        else:
            dangling_holiday = []

        # ── 8) 交叉引用: contract.product ↔ commodities ──
        commodity_keys: Set[str] = set()
        for exch, prods in (commodities or {}).items():
            if isinstance(prods, dict):
                for pid in prods:
                    commodity_keys.add(f"{exch}.{pid}")
        contract_products = _collect_contract_products(contracts_json)
        dangling_products = sorted(contract_products - commodity_keys)
        details["dangling_products"] = dangling_products

        # 孤立 fees (info-only)
        fees_orphan: List[str] = []
        for k in (fees_json or {}).keys():
            # fees key 形如 "SHFE.ag" 或 "SHFE.ag.2612"
            head = ".".join(str(k).split(".")[:2])
            if head and head not in commodity_keys:
                fees_orphan.append(str(k))
        details["orphan_fees"] = sorted(fees_orphan)[:50]

        # ── 9) 判定 ──
        fail_reasons: List[str] = []
        if dangling_sessions:
            fail_reasons.append(f"dangling_sessions={dangling_sessions}")
        if dangling_holiday:
            fail_reasons.append(f"dangling_holiday_keys={dangling_holiday}")
        if dangling_products:
            sample = dangling_products[:5]
            extra = "" if len(dangling_products) <= 5 else f" (+{len(dangling_products)-5})"
            fail_reasons.append(f"dangling_products={sample}{extra}")

        if fail_reasons:
            return GateResult(
                self.name, "fail",
                "basefiles 交叉校验失败: " + "; ".join(fail_reasons),
                details,
            )

        return GateResult(
            self.name, "pass",
            (f"basefiles loadback OK: "
             f"{commodities_count} 品种 / {sessions_count} sessions / "
             f"{contracts_count} 合约 (wtpy 原生 loader)"),
            details,
        )


__all__ = ["BasefilesLoadbackGate"]
