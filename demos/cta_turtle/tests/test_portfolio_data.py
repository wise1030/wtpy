"""组合权益合成与数据预检测试"""
import json
import os

import numpy as np
import pytest

from conftest import *  # noqa: F401,F403
from helpers import portfolio as pf
from helpers import DataChecker as dc


# ---------- funds 读取 ----------

def test_read_funds_curve(tmp_path):
    p = tmp_path / "funds.csv"
    p.write_text("date,closeprofit,positionprofit,dynbalance,fee\n"
                 "20230103,0,0,0.00,0\n"
                 "20230104,-100,50,-1200.5,12\n", encoding="utf-8")
    curve = pf.read_funds_curve(str(p))
    assert curve == {20230103: 0.0, 20230104: -1200.5}


def test_read_funds_missing_returns_empty(tmp_path):
    assert pf.read_funds_curve(str(tmp_path / "nope.csv")) == {}


# ---------- 曲线合成 ----------

def test_merge_equal_weight_with_ffill():
    a = {1: 100.0, 3: 300.0}          # 缺 2 → ffill=100
    b = {1: -50.0, 2: -80.0, 3: -90.0}
    dates, pnl, aligned = pf.merge_curves({"A": a, "B": b})
    assert dates == [1, 2, 3]
    # 等权 0.5: [50-25, 50-40, 150-45]
    assert np.allclose(pnl, [25.0, 10.0, 105.0])
    assert np.allclose(aligned["A"], [50, 50, 150])
    assert np.allclose(aligned["B"], [-25, -40, -45])


def test_merge_late_start_code_zero_filled():
    a = {1: 10.0, 2: 20.0}
    b = {2: 100.0}                     # 第2天才上市
    dates, pnl, _ = pf.merge_curves({"A": a, "B": b})
    assert np.allclose(pnl, [5.0, 60.0])


def test_merge_custom_weights_normalized():
    a = {1: 100.0}
    b = {1: -50.0}
    _, pnl, _ = pf.merge_curves(
        {"A": a, "B": b}, weights={"A": 3.0, "B": 1.0})
    assert np.allclose(pnl, [62.5])    # 0.75*100+0.25*(-50)


# ---------- 组合指标 ----------

def test_metrics_linear_growth():
    capital = 1_000_000.0
    pnl = np.linspace(0, 200_000, 241)   # 一年稳定赚 20%
    m = pf.portfolio_metrics(list(range(241)), pnl, capital)
    assert abs(m["total_return"] - 0.20) < 1e-6
    assert abs(m["max_drawdown"]) < 1e-9   # 单调 → 零回撤
    assert m["sharpe_ratio"] > 5           # 无波动高夏普
    assert m["days"] == 241


def test_metrics_drawdown():
    capital = 100.0
    # dynbalance 口径为累计盈亏 → equity = 100,150,120,130
    pnl = np.array([0, 50, 20, 30], dtype=float)
    m = pf.portfolio_metrics([1, 2, 3, 4], pnl, capital)
    assert abs(m["max_drawdown"] - 30.0 / 150.0) < 1e-6


def test_contribution_shares():
    a = {1: 30.0}
    b = {1: 10.0}
    dates, pnl, aligned = pf.merge_curves({"A": a, "B": b})
    c = pf.contribution(aligned, pnl)
    assert abs(c["A"] - 0.75) < 1e-6 and abs(c["B"] - 0.25) < 1e-6


def test_contribution_uses_final_not_sum():
    """贡献按期末累计盈亏比, 而非全序列求和 (回归: 曾误用 np.sum)"""
    a = {1: 0.0, 2: 100.0}
    b = {1: 0.0, 2: -60.0}
    dates, pnl, aligned = pf.merge_curves({"A": a, "B": b},
                                          weights={"A": 1.0, "B": 1.0})
    c = pf.contribution(aligned, pnl)
    assert abs(sum(c.values()) - 1.0) < 1e-6
    assert c["A"] > 1.0 and c["B"] < 0.0   # B 亏损为负贡献


# ---------- DataChecker ----------

def _mk_cfg(tmp, text):
    cfgp = tmp / "cfg.yaml"
    cfgp.write_text(text, encoding="utf-8")
    from helpers.ConfigLoader import ConfigLoader
    return ConfigLoader.load(str(cfgp))


CFG = """
instruments:
  - code: SHFE.rb.HOT
    period: d1
"""


def test_check_hots_and_fix(tmp_path):
    common = tmp_path / "common"
    common.mkdir()
    (common / "hots.json").write_text('{"SHFE": {}}', encoding="utf-8")
    ok, path, n = dc.check_hots("SHFE.rb.HOT", str(common))
    assert ok is False and n == -1
    assert dc.fix_hots("SHFE.rb.HOT", str(common)) is True
    hots = json.loads((common / "hots.json").read_text())
    assert hots["SHFE"]["rb"] == []
    assert dc.fix_hots("SHFE.rb.HOT", str(common)) is False   # 幂等


def test_check_hots_empty_rules_fatal_in_live(tmp_path):
    """空规则: 回测可用 (直读HOT.dsb), 实盘致死 (cacheIntegratedBars
    空容器 segfault, 2026-09-01 远端实测)"""
    common = tmp_path / "common"
    common.mkdir()
    (common / "hots.json").write_text('{"SHFE": {"rb": []}}',
                                      encoding="utf-8")
    ok, _, n = dc.check_hots("SHFE.rb.HOT", str(common))
    assert ok is True and n == 0                    # 回测口径放行
    ok_live, _, _ = dc.check_hots("SHFE.rb.HOT", str(common), live=True)
    assert ok_live is False                         # 实盘口径拦截
    # 写入规则后两口径均放行
    (common / "hots.json").write_text(
        '{"SHFE": {"rb": [{"date": 20260901, "to": "rb2701"}]}}',
        encoding="utf-8")
    assert dc.check_hots("SHFE.rb.HOT", str(common), live=True)[0] is True


def test_check_bars_csv_health_and_missing(tmp_path):
    storage = tmp_path / "storage"
    csv_dir = storage / "csv"
    csv_dir.mkdir(parents=True)
    f = csv_dir / "SHFE.rb.HOT_d.csv"
    f.write_text("date,open,high,low,close\n"
                 "2023/01/03,4000,4010,3990,4005\n"
                 "2023/01/04,4005,4020,4000,4015\n", encoding="utf-8")
    issues = dc.check_bars("SHFE.rb.HOT", "d1", str(storage))
    assert any(i.level == "info" and "rows=2" in i.msg for i in issues)

    issues = dc.check_bars("DCE.i.HOT", "d1", str(storage))
    assert any(i.level == "error" and "缺少" in i.msg for i in issues)


def test_dsb_unreadable_degrades_to_warn_not_error(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    dsb_dir = storage / "his" / "day" / "SHFE"
    dsb_dir.mkdir(parents=True)
    (dsb_dir / "SHFE.rb_HOT.dsb").write_bytes(b"\x00" * 32)
    monkeypatch.setattr(dc, "_read_dsb_health", lambda p: None)
    issues = dc.check_bars("SHFE.rb.HOT", "d1", str(storage))
    assert len(issues) == 1 and issues[0].level == "warn"


def test_check_config_flags_intrabar_subperiod(tmp_path):
    storage = tmp_path / "storage"
    csv_dir = storage / "csv"
    csv_dir.mkdir(parents=True)
    (csv_dir / "SHFE.rb.HOT_d.csv").write_text(
        "date,open\n2023/01/03,1\n", encoding="utf-8")   # 仅日线
    common = tmp_path / "common"
    common.mkdir()
    (common / "hots.json").write_text('{"SHFE":{"rb":[]}}',
                                      encoding="utf-8")
    cfg = _mk_cfg(tmp_path, CFG.replace(
        "period: d1", "period: d1\n    intrabar_stop: true"))
    issues = dc.check_config(cfg, str(common), str(storage))
    assert any(i.level == "error" and "m5" in i.msg for i in issues)


def test_prepare_data_cli_exit_codes(tmp_path):
    """CLI: 全齐(含暖机深度)→0; 缺数据→1"""
    import subprocess
    import sys
    root = os.path.dirname(os.path.abspath(__file__)) + "/.."
    common = tmp_path / "common"
    common.mkdir()
    (common / "hots.json").write_text('{"SHFE":{"rb":[]}}',
                                      encoding="utf-8")
    storage = tmp_path / "storage"
    csv_dir = storage / "csv"
    csv_dir.mkdir(parents=True)
    # 130 根日线 (>= 默认 bar_cnt=120 暖机需求)
    lines = ["date,open,high,low,close"]
    for i in range(130):
        lines.append(f"2023/{1 + i // 28:02d}/{1 + i % 28:02d},4,4,4,4")
    (csv_dir / "SHFE.rb.HOT_d.csv").write_text("\n".join(lines) + "\n",
                                               encoding="utf-8")
    cfgp = tmp_path / "cfg.yaml"
    cfgp.write_text(CFG, encoding="utf-8")

    r_ok = subprocess.run(
        [sys.executable, os.path.join(root, "scripts", "prepare_data.py"),
         "--config", str(cfgp), "--common", str(common),
         "--storage", str(storage)], capture_output=True, text=True)
    assert r_ok.returncode == 0, r_ok.stdout

    bad_storage = tmp_path / "empty_storage"
    bad_storage.mkdir()
    r_bad = subprocess.run(
        [sys.executable, os.path.join(root, "scripts", "prepare_data.py"),
         "--config", str(cfgp), "--common", str(common),
         "--storage", str(bad_storage)], capture_output=True, text=True)
    assert r_bad.returncode == 1
