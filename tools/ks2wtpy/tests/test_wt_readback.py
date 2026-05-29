"""WT readback gate 单元测试。

不依赖真实 wtpy/C++：通过 monkey-patch 替换 subprocess 调用，验证：
  - storage 自动发现两种布局
  - autodiscovery 区分 HOT/specific/malformed
  - expect=pass/fail 与实际结果的 verdict 判定矩阵
  - C++ segfault (rc != 0 + empty stdout) 被显式上报
  - skip：basefiles/storage 缺失/wtpy 不可用
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from acceptance.wt_readback import (
    WtReadbackGate, WtReadbackTask, _autodiscover_codes,
    _is_wt_std_code, _scan_storage, build_default_tasks,
)


# ---------- 纯函数 ----------

class TestIsWtStdCode:
    def test_hot(self):
        assert _is_wt_std_code("SHFE.rb.HOT")
        assert _is_wt_std_code("CZCE.AP.HOT")
    def test_specific(self):
        assert _is_wt_std_code("SHFE.rb.2510")
    def test_malformed_no_product_sep(self):
        assert not _is_wt_std_code("SHFE.rb2510")
        assert not _is_wt_std_code("SHFE.2510")
    def test_too_few_segs(self):
        assert not _is_wt_std_code("SHFE.rb")
        assert not _is_wt_std_code("rb")


class TestScanStorage:
    def test_subdir_layout(self, tmp_path: Path):
        (tmp_path / "csv").mkdir()
        (tmp_path / "dsb").mkdir()
        out = _scan_storage(str(tmp_path))
        assert out["csv"] == str(tmp_path / "csv")
        assert out["dsb"] == str(tmp_path / "dsb")
        assert out["lmdb"] is None

    def test_wt_standard_layout(self, tmp_path: Path):
        (tmp_path / "csv").mkdir()
        (tmp_path / "his" / "day" / "SHFE").mkdir(parents=True)
        out = _scan_storage(str(tmp_path))
        # WT 标准布局：dsb 指向 output_dir 根，让 WtDtServo 自行解析 his/
        assert out["dsb"] == str(tmp_path)

    def test_nothing(self, tmp_path: Path):
        out = _scan_storage(str(tmp_path))
        assert out == {"csv": None, "lmdb": None, "dsb": None}


class TestAutodiscover:
    """新 _autodiscover_codes 走 DSB + commodities.json 反推三段 stdCode。

    旧 CSV-扫描 + malformed 桶分支已删（修订方案 A, fix-dup）：
      - csv alias 已删，DSB 文件名不带点本身合规，无法再产出 malformed 候选
      - .IDX 反例已足够覆盖 "不应被解析为 HOT"
    """

    def _make_dsb(self, base: Path, files):
        """在 base/his/day/{EXCHG}/{code}.dsb 创建空 dsb。

        files: [(exchg, code), ...]
        """
        for exchg, code in files:
            d = base / "his" / "day" / exchg
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{code}.dsb").write_bytes(b"\0")

    def _make_commodities(self, base: Path, products):
        """在 base/common/commodities.json 写 {EXCHG:{product:{...}}}。"""
        common = base / "common"
        common.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        for exchg, prod in products:
            data.setdefault(exchg, {})[prod] = {"type": "futures"}
        (common / "commodities.json").write_text(json.dumps(data), encoding="utf-8")

    def test_classifies_hot_and_specific(self, tmp_path: Path):
        """HOT (rb.HOT) 直识，具体合约 (rb2510) 反推成 SHFE.rb.2510。"""
        self._make_dsb(tmp_path, [
            ("SHFE", "rb.HOT"),
            ("SHFE", "rb2510"),
        ])
        self._make_commodities(tmp_path, [("SHFE", "rb")])
        d = _autodiscover_codes(str(tmp_path))
        assert d["hot"] == ["SHFE.rb.HOT"]
        assert d["specific"] == ["SHFE.rb.2510"]
        assert "malformed" not in d  # 新接口不再返回 malformed 桶

    def test_czce_three_digit_month(self, tmp_path: Path):
        """CZCE 合约号 3 位月 (AP610) 必须反推成 'CZCE.AP.610'。"""
        self._make_dsb(tmp_path, [
            ("CZCE", "AP.HOT"),
            ("CZCE", "AP610"),
        ])
        self._make_commodities(tmp_path, [("CZCE", "AP")])
        d = _autodiscover_codes(str(tmp_path))
        assert d["hot"] == ["CZCE.AP.HOT"]
        assert d["specific"] == ["CZCE.AP.610"]

    def test_skips_unknown_products(self, tmp_path: Path):
        """commodities.json 缺该 product → 跳过该 DSB（不硬猜）。"""
        self._make_dsb(tmp_path, [("CFFEX", "XX1505")])
        self._make_commodities(tmp_path, [("CFFEX", "IC")])
        d = _autodiscover_codes(str(tmp_path))
        assert d["specific"] == []

    def test_empty_dir(self, tmp_path: Path):
        """缺 his/day → 返回空两桶字典。"""
        d = _autodiscover_codes(str(tmp_path))
        assert d == {"hot": [], "specific": []}

    def test_no_commodities_json(self, tmp_path: Path):
        """有 dsb 但缺 commodities.json → product_dict 空 → specific 空。"""
        self._make_dsb(tmp_path, [("SHFE", "rb.HOT"), ("SHFE", "rb2510")])
        d = _autodiscover_codes(str(tmp_path))
        assert d["hot"] == ["SHFE.rb.HOT"]
        # 没有 product 字典反推不出三段
        assert d["specific"] == []


class TestBuildDefaultTasks:
    def test_emits_hot_specific_and_idx_counterexample(self, tmp_path: Path):
        """新接口产出 HOT + specific + counterexample (.IDX) 三类任务，无 malformed。"""
        # 仿 WT 标准布局
        his_day = tmp_path / "his" / "day" / "SHFE"
        his_day.mkdir(parents=True)
        (his_day / "rb.HOT.dsb").write_bytes(b"\0")
        (his_day / "rb2510.dsb").write_bytes(b"\0")
        # commodities.json 供 product 反推
        common = tmp_path / "common"
        common.mkdir()
        (common / "commodities.json").write_text(
            json.dumps({"SHFE": {"rb": {"type": "futures"}}}), encoding="utf-8"
        )
        # csv 子目录可选（period 探测会落到 'd'）
        (tmp_path / "csv").mkdir()

        tasks = build_default_tasks(str(tmp_path))
        labels = [t.label for t in tasks]
        expects = {t.label: t.expect for t in tasks}

        assert any(l.startswith("HOT/") for l in labels), f"no HOT task in {labels}"
        assert any(l.startswith("specific/") for l in labels), f"no specific in {labels}"
        assert any(l.startswith("counterexample/") for l in labels), \
            f"no counterexample in {labels}"
        # 不应再有 malformed 任务
        assert not any(l.startswith("malformed/") for l in labels), \
            f"malformed 反例分支应已删除，但出现: {labels}"
        # counterexample 期望 fail
        for l in labels:
            if l.startswith("counterexample/"):
                assert expects[l] == "fail"


# ---------- Gate 行为（subprocess mock） ----------

@pytest.fixture
def fake_storage(tmp_path: Path) -> Path:
    """构造一个合法布局的数据仓 fixture（WT 标准布局 + commodities + dsb）。"""
    # basefiles
    common = tmp_path / "common"
    common.mkdir()
    (common / "commodities.json").write_text(
        json.dumps({"SHFE": {"rb": {"type": "futures"}}}), encoding="utf-8"
    )
    # csv 子目录（可选，用于 period 探测；这里只放 HOT 让 period='d'）
    csv = tmp_path / "csv"
    csv.mkdir()
    # dsb 文件触发 _autodiscover_codes 找到 HOT + specific
    his_day = tmp_path / "his" / "day" / "SHFE"
    his_day.mkdir(parents=True)
    (his_day / "rb.HOT.dsb").write_bytes(b"\0")
    (his_day / "rb2510.dsb").write_bytes(b"\0")
    return tmp_path


def _mk_proc(stdout: str = "", stderr: str = "", rc: int = 0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=rc)


def test_gate_pass_when_worker_returns_ok(monkeypatch, fake_storage):
    """所有 expect=pass 用例 worker 返回 ok=True；fail/反例返回 ok=False → 整体 pass。"""
    # mock wtpy importable
    import sys, types
    fake = types.ModuleType("wtpy")
    setattr(fake, "WtDtServo", object)
    monkeypatch.setitem(sys.modules, "wtpy", fake)

    def fake_run(cmd, input=None, capture_output=True, text=True, timeout=None):
        payload = json.loads(input)
        std = payload["stdCode"]
        # expect=pass → ok; expect=fail (counterexample/malformed) → not ok
        if ".HOT" in std or std.endswith(".2510"):
            return _mk_proc(json.dumps({"ok": True, "rows": 100,
                                         "first_bar": {"date": 20240101},
                                         "last_bar": {"date": 20240601}}), rc=0)
        # 反例 .IDX 与 malformed → empty bars / crash 都算 fail（符合 expect）
        return _mk_proc(json.dumps({"ok": False, "rows": 0, "error": "no bars"}), rc=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    res = WtReadbackGate().run(str(fake_storage))
    assert res.status == "pass", f"detail: {res.summary}"
    assert res.details["verdicts_summary"]["fail"] == 0
    assert res.details["verdicts_summary"]["pass"] >= 3


def test_gate_fail_when_hot_unreadable(monkeypatch, fake_storage):
    """HOT 用例返回 ok=False → 整体 fail。"""
    import sys, types
    fake = types.ModuleType("wtpy")
    setattr(fake, "WtDtServo", object)
    monkeypatch.setitem(sys.modules, "wtpy", fake)

    def fake_run(cmd, input=None, **_):
        payload = json.loads(input)
        std = payload["stdCode"]
        if ".HOT" in std:
            return _mk_proc(json.dumps({"ok": False, "rows": 0,
                                         "error": "WtDtServo returned None"}), rc=0)
        if std.endswith(".2510"):
            return _mk_proc(json.dumps({"ok": True, "rows": 50,
                                         "first_bar": {}, "last_bar": {}}), rc=0)
        return _mk_proc(json.dumps({"ok": False, "rows": 0, "error": "n/a"}), rc=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = WtReadbackGate().run(str(fake_storage))
    assert res.status == "fail"
    assert res.details["verdicts_summary"]["fail"] >= 1


def test_gate_reports_cpp_crash_explicitly(monkeypatch, fake_storage):
    """worker rc!=0 且 stdout 为空 → 显式上报 segfault。"""
    import sys, types
    fake = types.ModuleType("wtpy")
    setattr(fake, "WtDtServo", object)
    monkeypatch.setitem(sys.modules, "wtpy", fake)

    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _mk_proc("", "", rc=11))
    res = WtReadbackGate().run(str(fake_storage))
    # 任何 expect=pass 用例都会 fail；expect=fail 反例会被 verdict=pass
    found_crash = False
    for r in res.details["results"]:
        err = (r.get("error") or "")
        if "rc=11" in err and "stdout" in err:
            found_crash = True
            break
    assert found_crash, "未将 C++ crash 显式上报到 error 字段"


def test_gate_skip_when_no_common(tmp_path: Path):
    (tmp_path / "csv").mkdir()
    res = WtReadbackGate().run(str(tmp_path))
    assert res.status == "skip"
    assert "common" in res.summary


def test_gate_skip_when_wtpy_unavailable(monkeypatch, fake_storage):
    import sys
    monkeypatch.setitem(sys.modules, "wtpy", None)  # 触发 ImportError
    # 但 fake_storage 有 common —— 走 import 分支
    # monkeypatch import 不稳；用 monkeypatch.setattr 上下文的另一种方式
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__
    def fake_imp(name, *a, **k):
        if name == "wtpy":
            raise ImportError("no wtpy in test env")
        return real_import(name, *a, **k)
    monkeypatch.setattr("builtins.__import__", fake_imp)
    res = WtReadbackGate().run(str(fake_storage))
    assert res.status == "skip"
    assert "wtpy" in res.summary
