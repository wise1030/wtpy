from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "helpers"
if str(HELPERS) not in sys.path:
    sys.path.insert(0, str(HELPERS))

from regime_detector import RegimeDetector  # noqa: E402


class CountingRegimeDetector(RegimeDetector):
    def __init__(self, params=None):
        super().__init__(params)
        self.hurst_calls = 0
        self.adf_calls = 0

    def _calc_hurst(self, spread):
        self.hurst_calls += 1
        return 0.55

    def _calc_adf(self, spread):
        self.adf_calls += 1
        return 0.20


def test_hurst_and_adf_are_cached_between_recalc_intervals():
    detector = CountingRegimeDetector({
        "hurst_window": 60,
        "hurst_cache_every": 5,
        "adf_cache_every": 5,
    })
    spread = np.linspace(1.0, 120.0, 120)

    for i in range(12):
        detector.detect(spread + i * 0.01)

    assert detector.hurst_calls == 3  # bars 0, 5, 10
    assert detector.adf_calls == 3


def test_detect_still_updates_regime_history_every_bar_when_stats_cached():
    detector = CountingRegimeDetector({
        "hurst_window": 60,
        "hurst_cache_every": 10,
        "adf_cache_every": 10,
        "regime_persist_bars": 5,
    })
    spread = np.linspace(1.0, 120.0, 120)

    for i in range(7):
        detector.detect(spread + i * 0.01)

    assert detector.hurst_calls == 1
    assert detector.adf_calls == 1
    assert len(detector._regime_history) == 5


def test_default_adf_cache_every_is_24_so_adf_cached_within_24_bars():
    """P3-6: ADF 默认每 24 bar 重算 (m5 = 2h), Hurst 默认每 bar 重算。"""
    detector = CountingRegimeDetector({
        "hurst_window": 60,
    })
    spread = np.linspace(1.0, 120.0, 120)

    # 跑 5 bar < 24, ADF 只算 1 次, Hurst 每 bar 算
    for i in range(5):
        detector.detect(spread + i * 0.01)

    assert detector.hurst_calls == 5
    assert detector.adf_calls == 1


def test_default_adf_cache_every_recomputes_after_24_bars():
    """P3-6: 跑满 25 bar, ADF 触发第 2 次重算 (bars 0, 24)。"""
    detector = CountingRegimeDetector({
        "hurst_window": 60,
    })
    spread = np.linspace(1.0, 120.0, 120)

    for i in range(25):
        detector.detect(spread + i * 0.01)

    assert detector.hurst_calls == 25
    assert detector.adf_calls == 2  # bars 0, 24


def test_adf_maxlag_param_default_is_1():
    """P3-6: adf_maxlag 默认 1, 与 stat_factors._ols_coint 对齐。"""
    detector = RegimeDetector({"hurst_window": 60})
    assert detector._adf_maxlag == 1


def test_adf_maxlag_param_override_via_yaml():
    """P3-6: yaml 可显式覆盖 adf_maxlag。"""
    detector = RegimeDetector({"hurst_window": 60, "adf_maxlag": 10})
    assert detector._adf_maxlag == 10


def test_reset_clears_cached_hurst_adf_and_counters():
    detector = CountingRegimeDetector({
        "hurst_window": 60,
        "hurst_cache_every": 10,
        "adf_cache_every": 10,
    })
    spread = np.linspace(1.0, 120.0, 120)

    detector.detect(spread)
    detector.detect(spread + 0.01)
    assert detector.hurst_calls == 1
    assert detector.adf_calls == 1

    detector.reset()
    detector.detect(spread + 0.02)

    assert detector.hurst_calls == 2
    assert detector.adf_calls == 2
