"""O2 [P0] acceptance gate: basefiles_loadback

用 wtpy 原生 ContractMgr/SessionMgr/ProductMgr 真正 load 一遍输出仓的 common/
配置三件套, 并交叉校验:
  - commodities.json 每个品种的 .session 必须存在于 sessions.json
  - commodities.json 每个品种的 .holiday 字符串 (默认"CHINA") 必须是 holidays.json
    顶层的 key
  - contracts.json 每个合约的 .product 必须存在于 commodities.json
  - fees.json 每个 key 必须能匹配到 commodities 或 contracts

仅 ks2wtpy 转换器生成的 basefiles 才能被 WtBtEngine/WtDtServo 正常 init,
这是 WT 启动前的最后一道闸门。

设计:
  - 与现有 manifest/quality/completeness gate 独立
  - 任一 IO/load 异常 → fail (不抛)
  - 缺关键字段 → fail; 仅引用越界 → 视严重度 fail
  - 完全无 common/ → fail (上游必出 manifest, basefiles 是配套交付)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

KS2WTPY_DIR = Path(__file__).resolve().parents[1]
if str(KS2WTPY_DIR) not in sys.path:
    sys.path.insert(0, str(KS2WTPY_DIR))


# ----------------------------------------------------------------------------
# 工具: 构造一份"最小但完整"的 common/ 三件套 + holidays + 输出仓骨架
# ----------------------------------------------------------------------------

# 8 个 WT 官方 session ID (与 ks2wtpy config_generator 对齐)
_MIN_SESSIONS = {
    "FD0900": {
        "name": "期货白盘0900",
        "offset": 0,
        "auction": {"from": 855, "to": 900},
        "sections": [
            {"from": 900, "to": 1015},
            {"from": 1030, "to": 1130},
            {"from": 1330, "to": 1500},
        ],
    },
    "FD0915": {
        "name": "期货白盘0915",
        "offset": 0,
        "auction": {"from": 910, "to": 915},
        "sections": [
            {"from": 915, "to": 1130},
            {"from": 1300, "to": 1515},
        ],
    },
    "SD0930": {
        "name": "股票0930",
        "offset": 0,
        "auction": {"from": 925, "to": 930},
        "sections": [
            {"from": 930, "to": 1130},
            {"from": 1300, "to": 1500},
        ],
    },
    "FN0100": {
        "name": "期货夜盘0100",
        "offset": 300,
        "auction": {"from": 855, "to": 900},
        "sections": [
            {"from": 2100, "to": 100},
            {"from": 900, "to": 1015},
            {"from": 1030, "to": 1130},
            {"from": 1330, "to": 1500},
        ],
    },
}


def _write_min_basefiles(root: Path, *, with_holidays: bool = True,
                         commodities_overrides: dict | None = None,
                         sessions_overrides: dict | None = None,
                         holidays_overrides: dict | None = None) -> None:
    """在 root/common/ 下落地最小三件套 + holidays.json。"""
    common = root / "common"
    common.mkdir(parents=True, exist_ok=True)

    # commodities.json — 一品种引 FD0900, 假日表 CHINA
    commodities = {
        "SHFE": {
            "ag": {
                "name": "白银",
                "category": 1,
                "precision": 0,
                "pricetick": 1,
                "volscale": 15,
                "session": "FD0900",
                "holiday": "CHINA",
            },
        },
    }
    if commodities_overrides is not None:
        commodities = commodities_overrides
    (common / "commodities.json").write_text(
        json.dumps(commodities, ensure_ascii=False, indent=2), encoding="utf-8")

    # sessions.json — 8 ID 子集
    sessions = dict(_MIN_SESSIONS)
    if sessions_overrides is not None:
        sessions = sessions_overrides
    (common / "sessions.json").write_text(
        json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8")

    # contracts.json — 一合约引 ag
    contracts = {
        "SHFE": {
            "ag2612": {
                "name": "白银2612",
                "product": "ag",
            },
        },
    }
    (common / "contracts.json").write_text(
        json.dumps(contracts, ensure_ascii=False, indent=2), encoding="utf-8")

    # fees.json (可空, 但要存在)
    fees = {
        "SHFE.ag": {
            "open": 0.00005,
            "close": 0.00005,
            "closetoday": 0.0,
            "byvolume": False,
        }
    }
    (common / "fees.json").write_text(
        json.dumps(fees, ensure_ascii=False, indent=2), encoding="utf-8")

    # holidays.json — CHINA 顶层数组
    if with_holidays:
        holidays = {"CHINA": ["20260101", "20260102"]}
        if holidays_overrides is not None:
            holidays = holidays_overrides
        (common / "holidays.json").write_text(
            json.dumps(holidays, ensure_ascii=False, indent=2), encoding="utf-8")


# ----------------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------------


def test_basefiles_loadback_passes_when_all_consistent(tmp_path):
    """RED: 三件套+holidays 全 OK → pass。"""
    from acceptance.basefiles_loadback import BasefilesLoadbackGate
    _write_min_basefiles(tmp_path)
    r = BasefilesLoadbackGate().run(str(tmp_path))
    assert r.status == "pass", f"全 OK 应 pass, got {r.status}: {r.summary} | {r.details}"
    # 关键 details 字段
    assert r.details["commodities_count"] >= 1
    assert r.details["sessions_count"] >= 4
    assert r.details["contracts_count"] >= 1
    assert r.details["dangling_sessions"] == []
    assert r.details["dangling_products"] == []
    assert r.details["dangling_holiday_keys"] == []


def test_basefiles_loadback_fails_when_common_dir_missing(tmp_path):
    """RED: 完全无 common/ → fail。"""
    from acceptance.basefiles_loadback import BasefilesLoadbackGate
    r = BasefilesLoadbackGate().run(str(tmp_path))
    assert r.status == "fail", f"无 common/ 应 fail, got {r.status}: {r.summary}"
    assert "common" in r.summary.lower() or "missing" in r.summary.lower()


def test_basefiles_loadback_fails_when_commodities_missing(tmp_path):
    """RED: common/ 存在但 commodities.json 缺失 → fail。"""
    from acceptance.basefiles_loadback import BasefilesLoadbackGate
    _write_min_basefiles(tmp_path)
    (tmp_path / "common" / "commodities.json").unlink()
    r = BasefilesLoadbackGate().run(str(tmp_path))
    assert r.status == "fail", f"缺 commodities.json 应 fail, got {r.status}"
    assert "commodities" in r.summary.lower()


def test_basefiles_loadback_fails_on_dangling_session(tmp_path):
    """RED: commodities 引用了 sessions.json 没有的 session ID → fail。"""
    from acceptance.basefiles_loadback import BasefilesLoadbackGate
    bad_commodities = {
        "SHFE": {
            "ag": {
                "name": "白银",
                "category": 1,
                "precision": 0,
                "pricetick": 1,
                "volscale": 15,
                "session": "ZZ9999",  # 不存在
                "holiday": "CHINA",
            },
        },
    }
    _write_min_basefiles(tmp_path, commodities_overrides=bad_commodities)
    r = BasefilesLoadbackGate().run(str(tmp_path))
    assert r.status == "fail", f"悬挂 session 应 fail, got {r.status}: {r.summary}"
    assert "ZZ9999" in str(r.details.get("dangling_sessions", []))


def test_basefiles_loadback_fails_on_dangling_holiday(tmp_path):
    """RED: commodities.holiday='CHINA' 但 holidays.json 没 CHINA key → fail。"""
    from acceptance.basefiles_loadback import BasefilesLoadbackGate
    _write_min_basefiles(tmp_path, holidays_overrides={"USA": ["20260704"]})
    r = BasefilesLoadbackGate().run(str(tmp_path))
    assert r.status == "fail", f"holiday key 悬挂应 fail, got {r.status}: {r.summary}"
    assert "CHINA" in str(r.details.get("dangling_holiday_keys", []))


def test_basefiles_loadback_fails_when_holidays_missing(tmp_path):
    """RED: commodities 声明用 CHINA 但 holidays.json 文件根本不存在 → fail。"""
    from acceptance.basefiles_loadback import BasefilesLoadbackGate
    _write_min_basefiles(tmp_path, with_holidays=False)
    r = BasefilesLoadbackGate().run(str(tmp_path))
    assert r.status == "fail", f"holidays.json 不存在应 fail, got {r.status}: {r.summary}"


def test_basefiles_loadback_warns_on_orphan_session(tmp_path):
    """RED: sessions.json 含未被 commodities 引用的 session → 信息归 details, 整体 pass。

    (孤立 session 不破坏 WT 启动, 仅冗余, 不阻断)
    """
    from acceptance.basefiles_loadback import BasefilesLoadbackGate
    _write_min_basefiles(tmp_path)  # 默认 4 session 但只用 FD0900
    r = BasefilesLoadbackGate().run(str(tmp_path))
    assert r.status == "pass", f"孤立 session 不应阻断, got {r.status}: {r.summary}"
    # 孤立 session 应在 details 里显示
    orphans = r.details.get("orphan_sessions", [])
    assert "FD0915" in orphans and "SD0930" in orphans and "FN0100" in orphans


def test_basefiles_loadback_fails_on_unparsable_json(tmp_path):
    """RED: commodities.json 损坏 → fail (load 抛 → gate 捕获)。"""
    from acceptance.basefiles_loadback import BasefilesLoadbackGate
    _write_min_basefiles(tmp_path)
    (tmp_path / "common" / "commodities.json").write_text(
        "{not valid json", encoding="utf-8")
    r = BasefilesLoadbackGate().run(str(tmp_path))
    assert r.status == "fail", f"损坏 JSON 应 fail, got {r.status}"


def test_basefiles_loadback_reports_dangling_product_in_contracts(tmp_path):
    """RED: contracts 里某合约 product 不在 commodities → fail。"""
    from acceptance.basefiles_loadback import BasefilesLoadbackGate
    _write_min_basefiles(tmp_path)
    contracts = {
        "SHFE": {
            "ag2612": {"name": "白银2612", "product": "ag"},  # 合法
            "xx2612": {"name": "未知2612", "product": "xx"},  # 悬挂
        },
    }
    (tmp_path / "common" / "contracts.json").write_text(
        json.dumps(contracts, ensure_ascii=False), encoding="utf-8")
    r = BasefilesLoadbackGate().run(str(tmp_path))
    assert r.status == "fail", f"contracts 悬挂 product 应 fail, got {r.status}"
    assert "xx" in str(r.details.get("dangling_products", []))


def test_basefiles_loadback_uses_wtpy_native_loaders(tmp_path):
    """RED: gate 必须真正用 wtpy 的 ContractMgr/SessionMgr/ProductMgr load,
    不能只做 JSON 校验 (这是 O2 的关键价值)。
    """
    from acceptance.basefiles_loadback import BasefilesLoadbackGate
    _write_min_basefiles(tmp_path)
    r = BasefilesLoadbackGate().run(str(tmp_path))
    # 通过 details 暴露 wtpy 真实计数 (而不是 json 行数)
    # ContractMgr.getTotalCodes 风格的统计字段必须存在
    assert "loaded_via" in r.details
    assert "wtpy" in r.details["loaded_via"].lower(), \
        f"必须声明用 wtpy 原生 loader, got: {r.details.get('loaded_via')}"
