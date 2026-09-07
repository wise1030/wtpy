"""scripts/pick_hot_sina.py 纯函数测试"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "scripts"))
import pick_hot_sina as phs  # noqa: E402


def test_parse_symbol():
    assert phs._parse_symbol("铜2610") == "CU2610"
    assert phs._parse_symbol("沪深300指数期货2609") == "IF2609"  # 品种名含数字
    assert phs._parse_symbol("上海原油2610") == "SC2610"
    assert phs._parse_symbol("PTA2610") == "TA2610"
    assert phs._parse_symbol("甲醇2701") == "MA2701"
    assert phs._parse_symbol("螺纹钢2701") == "RB2701"
    assert phs._parse_symbol("鸡蛋2609") == ""              # 未收录品种
    assert phs._parse_symbol("铜") == ""                     # 无数字


def test_to_wt_code():
    # CZCE: 4位 → 3位
    assert phs._to_wt_code("CZCE", "TA2701") == "TA701"
    assert phs._to_wt_code("CZCE", "MA610") == "MA610"   # 已是3位不动
    # CFFEX: 大写保持
    assert phs._to_wt_code("CFFEX", "IF2609") == "IF2609"
    # 其余小写
    assert phs._to_wt_code("SHFE", "CU2610") == "cu2610"
    assert phs._to_wt_code("DCE", "L2701") == "l2701"
    assert phs._to_wt_code("INE", "SC2610") == "sc2610"


def test_to_sina_code_czce():
    # CZCE 3位 → 4位, 年份用 expiredate
    assert phs._to_sina_code("CZCE", "TA", "TA610", 20260815) == "TA2610"
    assert phs._to_sina_code("CZCE", "CF", "CF701", 20261115) == "CF2701"
    # 非 CZCE 仅大写
    assert phs._to_sina_code("SHFE", "cu", "cu2610", 20261015) == "CU2610"


def test_active_contracts_filter_and_keep():
    contracts = {"SHFE": {
        "cu2609": {"product": "cu", "expiredate": 20260915},
        "cu2610": {"product": "cu", "expiredate": 20261015},
        "cu2611": {"product": "cu", "expiredate": 20261115},
        "cu2509": {"product": "cu", "expiredate": 20250915},  # 已过期
        "rb2610": {"product": "rb", "expiredate": 20261015},  # 其他品种
    }}
    codes = phs.active_contracts(contracts, "SHFE", "cu", 20260903,
                                 keep=2)
    assert codes == ["CU2609", "CU2610"]      # 过期/他品种剔除, keep 截断


def test_merge_hots_append_and_dedup():
    hots = {"SHFE": {"rb": [{"date": 20260901, "from": "",
                             "to": "rb2701", "oldclose": 0.0,
                             "newclose": 0.0}]}}
    picked = {"SHFE.rb": ("RB2701", 100.0),      # 同主力 → 不动
              "SHFE.cu": ("CU2610", 50.0)}       # 新品种 → 追加
    n = phs.merge_hots(hots, picked, 20260903)
    assert n == 1
    assert len(hots["SHFE"]["rb"]) == 1                     # 未追加
    cu = hots["SHFE"]["cu"][0]
    assert cu["to"] == "cu2610" and cu["date"] == 20260903  # WT 小写格式
    # 再跑一次 → 全部幂等
    assert phs.merge_hots(hots, picked, 20260903) == 0
