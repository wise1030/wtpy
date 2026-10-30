# -*- coding: utf-8 -*-
"""S1/S2/speeds normalized profiles、配置与快照安全回归。"""
import numpy as np
import pytest

from schemas import (InstrumentConfig, SpeedConfig, TurtleBaseConfig,
                     TurtleConfig)
from core.profiles import (ProfileSpec, resolve_profiles, required_bars,
                           snapshot_meta, validate_snapshot)
from core.ConfigLoader import ConfigLoader
from core.TurtleEngine import TurtleEngine
from run_portfolio_bt import PortfolioSim, build_channels, channel_at


def test_single_system_uses_actual_base_windows():
    base = TurtleBaseConfig(s1_entry=21, s1_exit=11,
                            s2_entry=56, s2_exit=22)
    p1 = resolve_profiles(InstrumentConfig(code="X", system="S1"), base)[0]
    p2 = resolve_profiles(InstrumentConfig(code="X", system="S2"), base)[0]
    assert (p1.system, p1.entry, p1.exit, p1.fs) == ("S1", 21, 11, 56)
    assert (p2.system, p2.entry, p2.exit, p2.fs) == ("S2", 56, 22, 56)
    assert p1.units == p2.units == 1.0
    assert p1.share == p2.share == 1.0


def test_dual_legacy_units_and_shares_are_not_forced_half():
    base = TurtleBaseConfig()
    p3 = resolve_profiles(InstrumentConfig(code="X", system="S1+S2",
                                           units=3), base)
    assert [p.units for p in p3] == [2.0, 1.0]
    assert [p.share for p in p3] == pytest.approx([2 / 3, 1 / 3])
    p1 = resolve_profiles(InstrumentConfig(code="X", system="S1+S2",
                                           units=1), base)
    assert [p.units for p in p1] == [1.0, 0.0]
    assert [p.share for p in p1] == [1.0, 0.0]


def test_speed_resolution_preserves_name_system_and_custom_windows():
    speeds = [SpeedConfig(name="fast", entry=20, exit=10, fs=55,
                          share=0.25, system="S1"),
              SpeedConfig(name="slow", entry=55, exit=20, fs=55,
                          share=0.75, system="S2")]
    ps = resolve_profiles(InstrumentConfig(code="X", units=4,
                                           speeds=speeds), TurtleBaseConfig())
    assert [p.key for p in ps] == ["fast", "slow"]
    assert [p.system for p in ps] == ["S1", "S2"]
    assert [p.units for p in ps] == [1.0, 3.0]
    assert [p.share for p in ps] == [0.25, 0.75]
    assert ps[1].entry == 55 and ps[1].exit == 20


def test_direct_dataclass_profile_invariants_and_bad_snapshot():
    # 绕过 ConfigLoader 直接构造时，解析器自身也必须守住唯一性/份额。
    dup = [SpeedConfig(name="x", entry=20, exit=10, share=0.5),
           SpeedConfig(name="x", entry=55, exit=20, share=0.5)]
    with pytest.raises(ValueError, match="名称重复"):
        resolve_profiles(InstrumentConfig(code="X", speeds=dup),
                         TurtleBaseConfig())
    bad_share = [SpeedConfig(name="x", entry=20, exit=10, share=0.4),
                 SpeedConfig(name="y", entry=55, exit=20, share=0.4)]
    with pytest.raises(ValueError, match="合计为 1"):
        resolve_profiles(InstrumentConfig(code="X", speeds=bad_share),
                         TurtleBaseConfig())
    with pytest.raises(ValueError, match="system 非法"):
        resolve_profiles(InstrumentConfig(code="X", system="S3"),
                         TurtleBaseConfig())

    profiles = resolve_profiles(InstrumentConfig(code="X"), TurtleBaseConfig())
    with pytest.raises(ValueError, match="subs 段格式非法"):
        validate_snapshot({"subs": []}, profiles)
    bad_meta = {"subs": {"S1": {"system": "S1"}},
                "profiles": snapshot_meta(profiles)}
    bad_meta["profiles"]["S1"]["units"] = None
    with pytest.raises(ValueError, match="S1.units"):
        validate_snapshot(bad_meta, profiles)


def test_required_bars_includes_speed_windows():
    base = TurtleBaseConfig(bar_cnt=120)
    inst = InstrumentConfig(code="X", speeds=[
        SpeedConfig(name="slow", entry=250, exit=125, fs=55,
                    share=1.0, system="S1")])
    assert required_bars(inst, base) == 252


def test_snapshot_identity_and_semantic_guard():
    profiles = resolve_profiles(
        InstrumentConfig(code="X", system="S1+S2", units=2),
        TurtleBaseConfig())
    data = {"subs": {p.key: {"system": p.system} for p in profiles},
            "profiles": snapshot_meta(profiles)}
    validate_snapshot(data, profiles)       # 完整快照允许恢复

    missing = {"subs": {"S1": {"system": "S1"}}}
    with pytest.raises(ValueError, match="子系统集合"):
        validate_snapshot(missing, profiles)

    changed = {"subs": {p.key: {"system": p.system} for p in profiles},
               "profiles": snapshot_meta(profiles)}
    changed["profiles"]["S1"]["entry"] += 1
    with pytest.raises(ValueError, match="S1.entry"):
        validate_snapshot(changed, profiles)

    wrong_system = {"subs": {"S1": {"system": "S2"},
                             "S2": {"system": "S2"}}}
    with pytest.raises(ValueError, match="S1.system"):
        validate_snapshot(wrong_system, profiles)


def test_engine_restore_rejects_system_mismatch():
    snap = TurtleEngine(system="S1").snapshot()
    with pytest.raises(ValueError, match="system 不匹配"):
        TurtleEngine(system="S2").restore(snap)


def _yaml_with_speeds(speed_lines: str, base_lines: str = "") -> str:
    return ("base:\n" + (base_lines or "  bar_cnt: 120\n") +
            "instruments:\n  - code: SHFE.rb.HOT\n    units: 2\n"
            "    speeds:\n" + speed_lines)


def test_config_rejects_speed_unknown_key_and_implicit_multi_system(tmp_path):
    unknown = _yaml_with_speeds(
        "      - name: fast\n        entry: 20\n        exit: 10\n"
        "        share: 0.5\n        system: S1\n"
        "      - name: slow\n        entry: 55\n        exit: 20\n"
        "        share: 0.5\n        sytem: S2\n")
    p = tmp_path / "unknown.yaml"
    p.write_text(unknown, encoding="utf-8")
    with pytest.raises(ValueError, match="未知字段.*sytem"):
        ConfigLoader.load(str(p), warn=False)

    implicit = _yaml_with_speeds(
        "      - name: fast\n        entry: 20\n        exit: 10\n"
        "        share: 0.5\n"
        "      - name: slow\n        entry: 55\n        exit: 20\n"
        "        share: 0.5\n")
    p2 = tmp_path / "implicit.yaml"
    p2.write_text(implicit, encoding="utf-8")
    with pytest.raises(ValueError, match="显式配置 system"):
        ConfigLoader.load(str(p2), warn=False)


def test_config_speed_fs_follows_base_and_long_window_fails_fast(tmp_path):
    yaml_text = _yaml_with_speeds(
        "      - name: fast\n        entry: 20\n        exit: 10\n"
        "        system: S1\n",
        base_lines="  bar_cnt: 120\n  s2_entry: 60\n")
    p = tmp_path / "fs.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    cfg = ConfigLoader.load(str(p), warn=False)
    assert cfg.instruments[0].speeds[0].fs == 60

    long_yaml = _yaml_with_speeds(
        "      - name: slow\n        entry: 250\n        exit: 125\n"
        "        system: S1\n")
    p2 = tmp_path / "long.yaml"
    p2.write_text(long_yaml, encoding="utf-8")
    with pytest.raises(ValueError, match="所需 252"):
        ConfigLoader.load(str(p2), warn=False)


def test_portfolio_sim_accepts_multi_profile_with_distinct_engines(tmp_path):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "SHFE.rb.HOT_d.csv").write_text(
        "date,open,high,low,close,volume,turnover,open_interest\n"
        "20200101,100,101,99,100,10,0,1\n",
        encoding="utf-8")
    cfg = TurtleConfig(
        base=TurtleBaseConfig(),
        instruments=[InstrumentConfig(code="SHFE.rb.HOT",
                                      system="S1+S2", units=2)])
    sim = PortfolioSim(cfg, {("SHFE", "rb"): {"volscale": 10,
                                              "pricetick": 1}},
                       {}, 1_000_000, 0.6, 0,
                       data_dir=str(csv_dir))
    assert [s["key"] for s in sim.subs["SHFE.rb.HOT"]] == ["S1", "S2"]
    assert sim.engines["SHFE.rb.HOT"] is sim.subs["SHFE.rb.HOT"][0]["engine"]
    assert sim.subs["SHFE.rb.HOT"][0]["engine"] is not \
        sim.subs["SHFE.rb.HOT"][1]["engine"]


def test_build_channels_uses_normalized_profile_windows():
    h = np.arange(1.0, 41.0)
    h[0] = 1000.0       # 早期尖峰只进入 20 日通道，不进入最近 5 日通道
    bars = {"highs": h, "lows": h - 2, "closes": h - 1}
    profile = ProfileSpec("custom", "S2", 5, 3, 7, 1.0, 1.0)
    base = TurtleBaseConfig()
    _atr, chan = build_channels(bars, base, "S1", profile=profile)
    ch = channel_at(chan, 20)
    assert ch["entry_upper"] == pytest.approx(float(np.max(h[15:20])))
    _atr2, base_chan = build_channels(bars, base, "S1")
    assert channel_at(base_chan, 20)["entry_upper"] == pytest.approx(1000.0)
