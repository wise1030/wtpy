"""资金分配体系测试 (2026-10-22 S1)

portfolio_capital>0 启用分配: 策略权益基数 = portfolio × weight/Σweight;
共享账本下 RiskManager 按 sid 注册分配基数, 比例闸各用各的钱。
"""
import dataclasses

import pytest

from conftest import *  # noqa: F401,F403
from core.ConfigLoader import ConfigLoader, allocated_capital
from core.RiskManager import RiskManager
from schemas import RiskConfig
from StraTurtle import StraTurtle

_YAML = """\
base:
  unit_var: 200000
risk:
  portfolio_capital: {port}
  alloc_weights: {weights}
instruments:
  - code: SHFE.rb.HOT
    units: 1
  - code: DCE.m.HOT
    units: 1
"""


def _load(tmp_path, port=0, weights="null"):
    p = tmp_path / "cfg.yaml"
    p.write_text(_YAML.format(port=port, weights=weights),
                 encoding="utf-8")
    return ConfigLoader.load(str(p), warn=False), str(p)


class TestAllocatedCapital:
    def test_disabled_passthrough(self, tmp_path):
        cfg, _ = _load(tmp_path, 0)
        codes = ["SHFE.rb.HOT", "DCE.m.HOT"]
        assert allocated_capital(cfg.risk, "SHFE.rb.HOT", codes) == \
            cfg.risk.fallback_capital

    def test_equal_weight_split(self, tmp_path):
        cfg, _ = _load(tmp_path, 12_000_000)
        codes = ["SHFE.rb.HOT", "DCE.m.HOT"]
        assert allocated_capital(cfg.risk, "SHFE.rb.HOT", codes) == \
            pytest.approx(6_000_000)
        assert allocated_capital(cfg.risk, "DCE.m.HOT", codes) == \
            pytest.approx(6_000_000)

    def test_explicit_weights(self, tmp_path):
        cfg, _ = _load(tmp_path, 12_000_000,
                       "{SHFE.rb.HOT: 3.0, DCE.m.HOT: 1.0}")
        codes = ["SHFE.rb.HOT", "DCE.m.HOT"]
        assert allocated_capital(cfg.risk, "SHFE.rb.HOT", codes) == \
            pytest.approx(9_000_000)
        assert allocated_capital(cfg.risk, "DCE.m.HOT", codes) == \
            pytest.approx(3_000_000)

    def test_unknown_weight_code_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="未知 code"):
            _load(tmp_path, 12_000_000, "{SHFE.xx.HOT: 1.0}")

    def test_invalid_values_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="portfolio_capital"):
            _load(tmp_path, -1)
        with pytest.raises(ValueError, match="alloc_weights"):
            _load(tmp_path, 12_000_000, "{SHFE.rb.HOT: 0}")


class TestFromConfigAllocation:
    def test_allocation_replaces_equity_base(self, tmp_path):
        """from_config 装配后, 策略侧风控基数 = 分配额而非 fallback"""
        _cfg, path = _load(tmp_path, 12_000_000)
        stra = StraTurtle.from_config("t1", "SHFE.rb.HOT", path)
        assert stra._risk.cfg.fallback_capital == pytest.approx(6_000_000)

    def test_capital_override_maps_to_portfolio(self, tmp_path):
        """分配启用时 --capital 覆盖 portfolio_capital (组合总资金口径)"""
        _cfg, path = _load(tmp_path, 12_000_000)
        stra = StraTurtle.from_config("t1", "SHFE.rb.HOT", path,
                                      capital=20_000_000)
        # 单品种等权 → allocated = 20M × 1/2 = 10M
        assert stra._risk.cfg.fallback_capital == pytest.approx(10_000_000)
        # 关闭分配时走旧语义
        _cfg2, path2 = _load(tmp_path, 0)
        stra2 = StraTurtle.from_config("t2", "SHFE.rb.HOT", path2,
                                       capital=7_000_000)
        assert stra2._risk.cfg.fallback_capital == 7_000_000


class TestSidCapitalSharedBook:
    def test_shared_book_per_sid_base(self):
        """共享账本: 单位/总敞口共享, 权益基数按 sid 独立"""
        rm = RiskManager(RiskConfig(fallback_capital=15_000_000))
        rm.set_sid_capital("stra_a", 2_000_000)
        rm.set_sid_capital("stra_b", 4_000_000)
        assert rm.capital_base("stra_a") == 2_000_000
        assert rm.capital_base("stra_b") == 4_000_000
        assert rm.capital_base("unknown") == 15_000_000   # 未注册回退

    def test_equity_uses_sid_base(self):
        class Ctx:
            def stra_get_fund_data(self, _i):
                return 100_000.0        # 净盈亏 +10 万
        rm = RiskManager(RiskConfig(fallback_capital=15_000_000))
        rm.set_sid_capital("stra_a", 1_000_000)
        assert rm.equity(Ctx(), sid="stra_a") == pytest.approx(1_100_000)
        assert rm.equity(Ctx()) == pytest.approx(15_100_000)
