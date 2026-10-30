# -*- coding: utf-8 -*-
"""S1/S2/speeds normalized profiles、配置与快照安全回归。"""
import numpy as np
import pytest

from schemas import (InstrumentConfig, SpeedConfig, TurtleBaseConfig,
                     TurtleConfig)
from core.profiles import (ProfileSpec, resolve_profiles, required_bars,
                           snapshot_meta, validate_snapshot,
                           validate_snapshot_meta)
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


# ---------- profiles 统一入口 (2026-10-30 配置统一) ----------

from schemas import ProfileConfig  # noqa: E402


def _inst(profiles=None, units=1, system="S1", speeds=None):
    return InstrumentConfig(code="X.Y.HOT", system=system, units=units,
                            profiles=profiles or [],
                            speeds=speeds or [])


def test_profiles_single_preset_s1_equals_legacy_system():
    base = TurtleBaseConfig(s1_entry=21, s1_exit=11, s2_entry=56)
    legacy = resolve_profiles(InstrumentConfig(code="X", system="S1"), base)
    new = resolve_profiles(
        _inst(profiles=[ProfileConfig(preset="S1")]), base)
    assert new == legacy
    assert new[0].key == "S1" and new[0].filter is True


def test_profiles_single_preset_s2_uses_s2_windows():
    base = TurtleBaseConfig(s2_entry=56, s2_exit=22)
    ps = resolve_profiles(_inst(profiles=[ProfileConfig(preset="S2")]), base)
    assert (ps[0].system, ps[0].entry, ps[0].exit, ps[0].fs) == \
        ("S2", 56, 22, 56)
    assert ps[0].filter is False


def test_profiles_dual_preset_equals_legacy_s1s2():
    base = TurtleBaseConfig()
    legacy = resolve_profiles(
        InstrumentConfig(code="X", system="S1+S2", units=2), base)
    dual = resolve_profiles(_inst(
        units=2,
        profiles=[ProfileConfig(name="S1", preset="S1", share=0.5),
                  ProfileConfig(name="S2", preset="S2", share=0.5)]), base)
    assert [p.key for p in dual] == ["S1", "S2"]
    assert [(p.units, p.share) for p in dual] == \
        [(p.units, p.share) for p in legacy]
    assert [p.filter for p in dual] == [True, False]


def test_profiles_preset_window_override_keeps_semantics():
    """S2 预设 + entry 覆盖 = "S2 语义 + 40/20 窗口" (统一的核心收益)"""
    base = TurtleBaseConfig(s2_entry=56, s2_exit=22)
    ps = resolve_profiles(_inst(profiles=[ProfileConfig(
        preset="S2", entry=40)]), base)
    assert (ps[0].system, ps[0].entry, ps[0].exit) == ("S2", 40, 22)
    assert ps[0].filter is False


def test_profiles_s1_filter_off_keeps_s1_engine_semantics():
    """preset S1 + filter: false = 裸20/10: 引擎仍是 S1 状态机, 过滤器关"""
    base = TurtleBaseConfig()
    ps = resolve_profiles(
        _inst(profiles=[ProfileConfig(preset="S1", filter=False)]), base)
    assert ps[0].system == "S1" and ps[0].filter is False


def test_profiles_custom_windows_without_preset():
    base = TurtleBaseConfig()
    ps = resolve_profiles(_inst(profiles=[ProfileConfig(
        name="alt", entry=40, exit=15)]), base)
    assert (ps[0].key, ps[0].system, ps[0].entry, ps[0].exit) == \
        ("alt", "S1", 40, 15)
    assert ps[0].fs == base.s2_entry and ps[0].filter is True


@pytest.mark.parametrize("profiles,match", [
    ([ProfileConfig(preset="S2", filter=True)], "S2 无过滤器语义"),
    ([ProfileConfig(name="a", preset="S1", share=0.5),
      ProfileConfig(name="b", preset="S2", share=0.4)], "之和需为 1"),
    ([ProfileConfig(preset="S1", share=0.5),
      ProfileConfig(preset="S2", share=0.5)], "必须显式 name"),
    ([ProfileConfig(preset="S1"), ProfileConfig(name="b", preset="S2")],
     "必须显式 name"),
    ([ProfileConfig(name="x")], "必须显式 entry/exit"),
    ([ProfileConfig(preset="S3")], "preset 非法"),
    ([ProfileConfig(preset="S1", share=1.5)], "share 需在"),
])
def test_profiles_validation_fail_loud(profiles, match):
    base = TurtleBaseConfig()
    with pytest.raises(ValueError, match=match):
        resolve_profiles(_inst(profiles=profiles), base)


def test_profiles_system_and_speeds_mutually_exclusive(tmp_path):
    """profiles 是唯一入口: 与旧键叠加 = fail-loud, 优先级规则不借尸还魂"""
    base_yaml = (
        "base: {unit_var: 200000}\n"
        "instruments:\n"
        "  - code: SHFE.rb.HOT\n"
        "    system: S1\n"
        "    profiles:\n"
        "      - preset: S1\n")
    fp = str(tmp_path / "cfg.yaml")
    (tmp_path / "cfg.yaml").write_text(base_yaml, encoding="utf-8")
    with pytest.raises(ValueError, match="互斥"):
        ConfigLoader.load(fp, warn=False)


def test_profiles_yaml_end_to_end_and_legacy_warning(tmp_path):
    import yaml as _yaml
    p = tmp_path / "cfg.yaml"
    p.write_text(_yaml.safe_dump({
        "base": {"unit_var": 200000},
        "instruments": [{
            "code": "SHFE.rb.HOT", "units": 2,
            "profiles": [
                {"name": "fast", "preset": "S1", "entry": 15, "share": 0.5},
                {"name": "slow", "preset": "S2", "share": 0.5},
            ],
        }],
    }, allow_unicode=True), encoding="utf-8")
    cfg = ConfigLoader.load(str(p), warn=False)
    ps = resolve_profiles(cfg.instruments[0], cfg.base)
    assert [(x.key, x.system, x.entry, x.exit, x.units, x.share)
            for x in ps] == [
        ("fast", "S1", 15, cfg.base.s1_exit, 1.0, 0.5),
        ("slow", "S2", cfg.base.s2_entry, cfg.base.s2_exit, 1.0, 0.5)]
    # deprecated system 键 → 告警但可用
    p2 = tmp_path / "legacy.yaml"
    p2.write_text(_yaml.safe_dump({
        "base": {"unit_var": 200000},
        "instruments": [{"code": "SHFE.rb.HOT", "system": "S2"}],
    }, allow_unicode=True), encoding="utf-8")
    cfg2 = ConfigLoader.load(str(p2), warn=False)
    assert resolve_profiles(cfg2.instruments[0], cfg2.base)[0].system == "S2"


def test_profile_filter_reaches_engine():
    from StraTurtle import StraTurtle
    inst = InstrumentConfig(code="SHFE.rb.HOT", units=1, profiles=[
        ProfileConfig(preset="S1", filter=False)])
    stra = StraTurtle("tf", inst, TurtleBaseConfig())
    assert stra._subs[0]["engine"].enable_s1_filter is False
    assert stra._subs[0]["engine"].system == "S1"


def test_snapshot_meta_unchanged_by_unification():
    """快照指纹键集合不变 → 存量 v4 快照恢复零影响"""
    meta = ProfileSpec("S1", "S1", 20, 10, 55, 1.0, 1.0).snapshot_meta()
    assert set(meta) == {"system", "entry", "exit", "fs", "units", "share"}
    validate_snapshot_meta({"S1": meta},
                           [ProfileSpec("S1", "S1", 20, 10, 55, 1.0, 1.0)])
