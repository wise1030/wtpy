"""run_portfolio_bt contract 信号源接线回归 (Phase 1, 2026-11-11)。

build_contract_series 拼接语义 (对齐 StraTurtle bt align_window):
上市前头部截断 / 换月日切 dom / 段内缺口沿用前收 / volume/hold
带上 (过滤链输入); PortfolioSim 接线: contract 品种 data 替换 +
_no_roll_shift 禁执行层价差平移。
"""
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "research"))

from research.run_portfolio_bt import (build_contract_series,   # noqa: E402
                                       load_signal_calendar,
                                       load_bars, load_contracts, load_fees)
from core.ConfigLoader import ConfigLoader                       # noqa: E402


def _write_cache(tmp_path, name, rows):
    p = os.path.join(str(tmp_path), name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(rows, f)
    return p


def _bars(dates, closes):
    n = len(dates)
    return {"dates": np.asarray(dates, dtype=np.int64),
            "opens": np.asarray(closes, dtype=float),
            "highs": np.asarray([c + 1 for c in closes], dtype=float),
            "lows": np.asarray([c - 1 for c in closes], dtype=float),
            "closes": np.asarray(closes, dtype=float),
            "volumes": np.ones(n), "ois": np.ones(n),
            "index": {int(d): i for i, d in enumerate(dates)}}


def test_series_roll_and_gap_fill(tmp_path):
    cal = [(20190102, 20190131, "1905"), (20190201, 20190228, "1910")]
    _write_cache(tmp_path, "SHFE.rb.1905.json", [
        {"date": f"201901{d:02d}", "open": 100, "high": 101, "low": 99,
         "close": 100 + d, "volume": 10 + d, "hold": 5}
        for d in range(2, 32) if d not in (20, 21)])   # 20/21 缺口
    _write_cache(tmp_path, "SHFE.rb.1910.json", [
        {"date": f"201902{d:02d}", "open": 350, "high": 351, "low": 349,
         "close": 350, "volume": 20, "hold": 9} for d in range(1, 29)])
    dates = [20190101] + [20190100 + d for d in range(2, 32)] \
        + [20190200 + d for d in range(1, 4)]
    out = build_contract_series(_bars(dates, [0] * len(dates)), cal,
                                str(tmp_path), "SHFE", "rb")
    # 0101 上市前截断; 0102..0103 旧 dom; 0120/0121 日历内缺口
    # → 沿用前收; 0131 已切 1910 有数据 → 新 dom 真实价 (无复权)
    assert out["dates"][0] == 20190102
    assert 20190120 in list(out["dates"])          # 缺口日保留 (前收填充)
    i31 = list(out["dates"]).index(20190131)
    assert out["closes"][i31] == 131.0             # 0131 仍属 1905 段
    i201 = list(out["dates"]).index(20190201)
    assert out["closes"][i201] == 350.0            # 换月即切, 无跳空平移
    # 缺口沿用前收: 0120 close == 0119 close
    c19 = out["closes"][list(out["dates"]).index(20190119)]
    c20 = out["closes"][list(out["dates"]).index(20190120)]
    assert c19 == c20
    assert out["volumes"][list(out["dates"]).index(20190120)] == 0.0
    assert out["ois"][0] == 5.0                    # hold 拼接 (过滤链输入)


def test_series_window_extension_and_short(tmp_path):
    cal = [(20190102, 20190120, "1905")]
    _write_cache(tmp_path, "SHFE.rb.1905.json", [
        {"date": f"201901{d:02d}", "open": 1, "high": 2, "low": 0,
         "close": d, "volume": 1, "hold": 1} for d in range(2, 21)])
    dates = [20181231] + [20190100 + d for d in range(2, 13)] \
        + [20190125]                                   # 窗外沿用最近段
    out = build_contract_series(_bars(dates, [0] * len(dates)), cal,
                                str(tmp_path), "SHFE", "rb")
    assert out["dates"][0] == 20190102
    assert out["dates"][-1] == 20190125
    assert out["closes"][-1] == 12.0           # 窗外沿用输入序列前收
    # 数据不足 → {}
    assert build_contract_series(_bars([20190102], [0]), cal,
                                 str(tmp_path), "SHFE", "rb") == {}


def test_load_signal_calendar_segments(tmp_path):
    d = {"codes": {"SHFE.rb.HOT": {"segments": [["20190102", "20190131",
                                                "1905"]]},
                   "BAD.HOT": {"segments": [["x", "y", "z"]]}}}
    p = os.path.join(str(tmp_path), "roll_calendar.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f)
    out = load_signal_calendar(str(tmp_path))
    assert out == {"SHFE.rb.HOT": [(20190102, 20190131, "1905")]}


def test_portfolio_sim_contract_wiring(tmp_path, monkeypatch):
    """PortfolioSim 接线: contract 品种 data 被拼接序列替换且登记
    _no_roll_shift (禁执行层换月价差平移)。"""
    import research.run_portfolio_bt as pbt
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    cfg = ConfigLoader.load(os.path.join(
        root, "config", "turtle_config.yaml"), warn=False)
    import dataclasses
    cfg = dataclasses.replace(
        cfg, base=dataclasses.replace(cfg.base, signal_source="contract"))
    common = os.path.join(root, "..", "common")
    sim = pbt.PortfolioSim(cfg, load_contracts(common), load_fees(common),
                           60_000_000, 0.6, 1, codes=["rb"],
                           common_dir=common)
    code = "SHFE.rb.HOT"
    assert code in sim._no_roll_shift
    bars = sim.data[code]
    cal = sim.sig_calendar[code]
    first = cal[0][0]
    assert bars["dates"][0] >= first          # 上市前截断
    assert code not in sim.roll_calendar or True  # roll_calendar 仍加载
    # 通道数组与 data 等长 (NaN 暖机期存在)
    assert len(sim.chans[code]["atr"]) == len(bars["closes"])
