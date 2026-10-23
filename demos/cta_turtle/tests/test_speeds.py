"""多速度装配测试 (2026-10-20)

修复点: speeds[].name 只作展示/快照键, 不再直接传给 TurtleEngine
(system 断言只接受 S1/S2); 单速度也必须用子系统 name 取通道。
"""
import os
import tempfile

import numpy as np
import pytest

from conftest import *  # noqa: F401,F403
from StraTurtle import StraTurtle
from schemas import (InstrumentConfig, TurtleBaseConfig, SpeedConfig)
from core.ConfigLoader import ConfigLoader


def _mk(speeds, units=2):
    inst = InstrumentConfig(code="SHFE.rb.HOT", period="d1", system="S1",
                            units=units, speeds=speeds)
    stra = StraTurtle("ts", inst, TurtleBaseConfig())
    stra._volscale = 10.0
    stra._pricetick = 1.0
    return stra


def _series(n=90):
    highs = np.linspace(100.0, 140.0, n)
    lows = highs - 2.0
    closes = highs - 1.0
    return highs, lows, closes


def test_speed_names_are_decoupled_from_engine_system():
    speeds = [
        SpeedConfig(name="fast", entry=20, exit=10, share=0.5, system="S1"),
        SpeedConfig(name="slow", entry=55, exit=20, share=0.5, system="S2"),
    ]
    stra = _mk(speeds)
    assert [s["name"] for s in stra._subs] == ["fast", "slow"]
    assert [s["engine"].system for s in stra._subs] == ["S1", "S2"]
    assert [s["system"] for s in stra._subs] == ["S1", "S2"]


def test_single_speed_channel_key_uses_sub_name():
    """单速度时 _calc_indicators 的 key 是速度名, _eval_core 不能再按
    inst.system 取 (旧实现 KeyError)。"""
    stra = _mk([SpeedConfig(name="fast", entry=20, exit=10, share=1.0,
                            system="S1")])
    highs, lows, closes = _series()
    atr, n, chan = stra._calc_indicators(highs, lows, closes)
    assert np.isfinite(n)
    assert set(chan) == {"fast"}
    assert chan["fast"]["entry_upper"] > 0


def test_speed_config_parsing_system_and_invalid():
    yml = """
instruments:
  - code: SHFE.rb.HOT
    units: 2
    speeds:
      - name: fast
        entry: 20
        exit: 10
        share: 0.5
        system: S1
      - name: slow
        entry: 55
        exit: 20
        share: 0.5
        system: S2
"""
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "c.yaml")
        with open(p, "w", encoding="utf-8") as f:
            f.write(yml)
        cfg = ConfigLoader.load(p)
    sp = cfg.instruments[0].speeds
    assert [s.name for s in sp] == ["fast", "slow"]
    assert [s.system for s in sp] == ["S1", "S2"]

    bad = yml.replace("system: S2", "system: S3")
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "c.yaml")
        with open(p, "w", encoding="utf-8") as f:
            f.write(bad)
        with pytest.raises(ValueError, match="system"):
            ConfigLoader.load(p)
