"""live contract 信号源合约码解析回归 (2026-11-11 激活包)。

实锤: hots.json 'to' 字段 CZCE 为 'TA701' (品种+3位, contracts.json
键口径), 旧实现输出 'CZCE.TA701.A701' 双重前缀 → 框架查无合约,
信号副K 静默无数据。SHFE 系 'rb2701' (品种+4位) 不受影响。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from StraTurtle import StraTurtle                          # noqa: E402
from schemas import InstrumentConfig, TurtleBaseConfig     # noqa: E402
from core.RiskManager import RiskConfig                    # noqa: E402


def _stra(code):
    return StraTurtle("t", InstrumentConfig(code=code, units=1),
                      TurtleBaseConfig(), RiskConfig(), notify=None)


def test_shfe_4digit_contract():
    s = _stra("SHFE.rb.HOT")
    assert s._full_contract_from_contract("rb2701") == "SHFE.rb.2701"
    assert s._full_contract_from_contract("cu2612") == "SHFE.cu.2612"


def test_czce_3digit_key_contract():
    """CZCE 'to'=品种+3位 (contracts.json 键口径) → ex+键, 无中段"""
    s = _stra("CZCE.TA.HOT")
    assert s._full_contract_from_contract("TA701") == "CZCE.TA701"
    s2 = _stra("CZCE.MA.HOT")
    assert s2._full_contract_from_contract("MA701") == "CZCE.MA701"


def test_already_full_code_passthrough():
    s = _stra("SHFE.rb.HOT")
    assert s._full_contract_from_contract("SHFE.rb.2701") == "SHFE.rb.2701"
