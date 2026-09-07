"""scripts/update_hots.py 纯函数/解析测试"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "scripts"))
import pick_hot_sina as phs  # noqa: E402
import update_hots as uh  # noqa: E402


# ------------------------------------------------------------------
# parse_hq_text: 新浪 hq.sinajs.cn 响应解析
# ------------------------------------------------------------------
CF_LINE = ('var hq_str_nf_RB2701="螺纹钢2701,230000,3164.000,3183.000,'
           '3161.000,0.000,3172.000,3173.000,3173.000,0.000,3160.000,'
           '218,7,1502783.000,277062,沪,2026-09-04,0,0";')
FF_LINE = ('var hq_str_nf_IF2609="4568.000,4594.000,4519.000,4537.400,'
           '70771,322908019.400,134178.000,4537.400,0.000,4995.200";')


def test_parse_hq_text_cf():
    holds = phs.parse_hq_text(CF_LINE, ["rb2701"], 13)
    assert holds == {"rb2701": 1502783.0}


def test_parse_hq_text_ff():
    holds = phs.parse_hq_text(FF_LINE, ["IF2609"], 6)
    assert holds == {"IF2609": 134178.0}


def test_parse_hq_text_case_insensitive_match():
    """请求小写/响应大写混洗照常匹配, 返回键保持请求原样"""
    holds = phs.parse_hq_text(CF_LINE, ["RB2701"], 13)
    assert holds == {"RB2701": 1502783.0}


def test_parse_hq_text_invalid_code_empty_payload():
    """无效代码返回空 payload (CZCE 3 位码实测), 静默跳过"""
    holds = phs.parse_hq_text('var hq_str_nf_TA701="";', ["TA701"], 13)
    assert holds == {}


def test_parse_hq_text_mixed_batch():
    text = CF_LINE + "\n" + FF_LINE.replace("IF2609", "XX9999") \
        + '\nvar hq_str_nf_TA2610="PTA2610,230000,3164,3183,3161,0,3172,3173,3173,0,3160,218,7,88888.000,277062,郑";'
    holds = phs.parse_hq_text(text, ["rb2701", "TA2610"], 13)
    assert holds == {"rb2701": 1502783.0, "TA2610": 88888.0}


# ------------------------------------------------------------------
# _current_hots: 变更比对快照
# ------------------------------------------------------------------
def test_current_hots_snapshot():
    hots = {"SHFE": {"rb": [{"date": 20260901, "from": "", "to": "rb2701"}],
                     "cu": []},
            "CZCE": {"TA": [{"date": 20260901, "from": "", "to": "TA701"}]}}
    cur = uh._current_hots(hots)
    assert cur == {"SHFE.rb": "rb2701", "CZCE.TA": "TA701"}


def test_diff_detects_roll():
    before = {"SHFE.rb": "rb2701", "CZCE.TA": "TA701"}
    after = {"SHFE.rb": "rb2705", "CZCE.TA": "TA701"}
    diff = {k: (before.get(k), after[k]) for k in after
            if before.get(k) != after[k]}
    assert diff == {"SHFE.rb": ("rb2701", "rb2705")}


def test_diff_new_product_key():
    before = {"SHFE.rb": "rb2701"}
    after = {"SHFE.rb": "rb2701", "CZCE.TA": "TA701"}
    diff = {k: (before.get(k), after[k]) for k in after
            if before.get(k) != after[k]}
    assert diff == {"CZCE.TA": (None, "TA701")}
