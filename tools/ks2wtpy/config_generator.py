#!/usr/bin/env python3
"""
wtpy基础配置文件生成器

从KS数据中提取品种、合约信息，生成wtpy回测所需的配置文件:
  - commodities.json  品种配置
  - contracts.json    合约配置
  - hots.json         主力合约映射
  - sessions.json     交易时间模板
  - holidays.json     节假日
  - fees.json         手续费

这些文件放在回测配置的 basefiles 目录下。
"""

import os
import json
import re
import logging
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

from config import (
    EXCHANGES, FUTURES_PRODUCTS, OPTION_PRODUCTS,
    PERIOD_MAP, SESSION_TEMPLATES,
)
from ks_reader import KSDataReader, KSBarRecord

logger = logging.getLogger(__name__)


# ============================================================
# 品种分类
# ============================================================
# 品种大类: 金融/金属/能化/农产品/有色/黑色/建材/软商品/其他
PRODUCT_CATEGORIES = {
    # 金融
    'IF': '金融', 'IH': '金融', 'IC': '金融', 'IM': '金融',
    'T': '金融', 'TF': '金融', 'TS': '金融', 'TL': '金融',
    # 黑色
    'rb': '黑色', 'hc': '黑色', 'i': '黑色', 'j': '黑色', 'jm': '黑色',
    'fb': '黑色', 'wr': '黑色',
    # 有色金属
    'cu': '有色', 'al': '有色', 'zn': '有色', 'pb': '有色', 'ni': '有色',
    'sn': '有色', 'ss': '有色', 'ao': '有色', 'bc': '有色',
    # 贵金属
    'au': '贵金属', 'ag': '贵金属',
    # 能化
    'ru': '能化', 'bu': '能化', 'fu': '能化', 'sp': '能化', 'pg': '能化',
    'eg': '能化', 'eb': '能化', 'v': '能化', 'v_f': '能化', 'l': '能化',
    'l_f': '能化', 'pp': '能化', 'pp_f': '能化', 'TA': '能化', 'MA': '能化',
    # P1-9 fix: FG(玻璃)/SA(纯碱)归建材，UR(尿素)归能化；不再重复
    'UR': '能化', 'PF': '能化', 'SH': '能化',
    'ec': '能化', 'lu': '能化', 'nr': '能化', 'sc': '能化',
    # 农产品
    'a': '农产品', 'b': '农产品', 'm': '农产品', 'y': '农产品', 'p': '农产品',
    'c': '农产品', 'cs': '农产品', 'jd': '农产品', 'rr': '农产品', 'lh': '农产品',
    'AP': '农产品', 'CJ': '农产品', 'PK': '农产品', 'WH': '农产品', 'PM': '农产品',
    'RI': '农产品', 'RS': '农产品', 'RM': '农产品', 'JR': '农产品', 'ER': '农产品',
    'LR': '农产品', 'WS': '农产品', 'WT': '农产品', 'PL': '农产品', 'PR': '农产品',
    'RO': '农产品',
    # 建材
    'FG': '建材', 'SA': '建材',
    # 软商品
    'SR': '软商品', 'CF': '软商品', 'OI': '软商品', 'CY': '软商品',
    # 其他
    'br': '其他', 'ad': '其他', 'lg': '其他',
    'SF': '其他', 'SM': '其他', 'ME': '其他',
    'ZC': '其他', 'TC': '其他', 'PX': '其他',
    'bb': '其他', 'bz': '其他',
    'lc': '其他', 'pd': '其他', 'ps': '其他', 'pt': '其他', 'si': '其他',
}

# 交易时间模板定义
# 格式: {模板ID: {品种列表, 交易时段}}
SESSION_DEFINITIONS = {
    'CFFEX_STOCK': {
        'name': '中金所股指期货',
        'products': ['IF', 'IH', 'IC', 'IM'],
        'sections': [
            {'from': '09:30', 'to': '11:30'},
            {'from': '13:00', 'to': '15:00'},
        ],
    },
    'CFFEX_BOND': {
        'name': '中金所国债期货',
        'products': ['T', 'TF', 'TS', 'TL'],
        'sections': [
            {'from': '09:15', 'to': '11:30'},
            {'from': '13:00', 'to': '15:15'},
        ],
    },
    'SHFE_NIGHT1': {
        'name': '上期所夜盘21-23点',
        'products': ['rb', 'ru', 'bu', 'hc', 'fu', 'sp', 'ag', 'au', 'cu', 'al',
                      'zn', 'ni', 'sn', 'pb', 'ss', 'br', 'ao', 'ad'],
        'sections': [
            {'from': '21:00', 'to': '23:00'},
            {'from': '09:00', 'to': '10:15'},
            {'from': '10:30', 'to': '11:30'},
            {'from': '13:30', 'to': '15:00'},
        ],
    },
    'DCE_NIGHT1': {
        'name': '大商所夜盘21-23点',
        'products': ['i', 'j', 'jm', 'm', 'y', 'p', 'a', 'b', 'bb', 'c', 'cs',
                      'eg', 'eb', 'fb', 'pg', 'pp', 'v', 'rr', 'l', 'lh', 'lg'],
        'sections': [
            {'from': '21:00', 'to': '23:00'},
            {'from': '09:00', 'to': '10:15'},
            {'from': '10:30', 'to': '11:30'},
            {'from': '13:30', 'to': '15:00'},
        ],
    },
    'CZCE_NIGHT1': {
        'name': '郑商所夜盘21-23点',
        'products': ['AP', 'CF', 'CJ', 'CY', 'FG', 'MA', 'OI', 'PF', 'PK', 'PX',
                      'RM', 'SA', 'SF', 'SM', 'SR', 'TA', 'TC', 'UR', 'ZC', 'SH',
                      'PL', 'PR', 'RO', 'ME'],
        'sections': [
            {'from': '21:00', 'to': '23:00'},
            {'from': '09:00', 'to': '10:15'},
            {'from': '10:30', 'to': '11:30'},
            {'from': '13:30', 'to': '15:00'},
        ],
    },
    'INE_NIGHT1': {
        'name': '能源中心夜盘21-23点',
        'products': ['sc', 'nr', 'lu', 'bc', 'ec'],
        'sections': [
            {'from': '21:00', 'to': '23:00'},
            {'from': '09:00', 'to': '10:15'},
            {'from': '10:30', 'to': '11:30'},
            {'from': '13:30', 'to': '15:00'},
        ],
    },
    'GFEX_DAY': {
        'name': '广期所日盘',
        'products': ['lc', 'pd', 'ps', 'pt', 'si'],
        'sections': [
            {'from': '09:00', 'to': '10:15'},
            {'from': '10:30', 'to': '11:30'},
            {'from': '13:30', 'to': '15:00'},
        ],
    },
    'CZCE_DAY': {
        'name': '郑商所纯日盘',
        'products': ['WH', 'PM', 'RI', 'RS', 'JR', 'ER', 'WS', 'WT'],
        'sections': [
            {'from': '09:00', 'to': '10:15'},
            {'from': '10:30', 'to': '11:30'},
            {'from': '13:30', 'to': '15:00'},
        ],
    },
}

# 品种→交易时间模板映射
PRODUCT_SESSION_MAP = {}
for sess_id, sess_def in SESSION_DEFINITIONS.items():
    for product in sess_def['products']:
        PRODUCT_SESSION_MAP[product] = sess_id


def parse_contract_code(code: str, reference_date: int = 0) -> Optional[Tuple[str, int, int]]:
    """解析合约代码 → (品种, 年, 月)

    格式:
      CFFEX/DCE/SHFE/INE/GFEX: 品种+年月 (如 rb2605, IF2606)
      CZCE: 品种+年月 (如 AP2605, SR109)
        - 旧合约3位码 (如 SR109 = 2021年9月)
        - 新合约4位码 (如 AP2605 = 2026年5月)

    P0-3 fix: 增加 reference_date 参数，用 bar 数据的日期锚定年份，
    不再依赖 time.localtime().tm_year（跨年边界误判）。
    """
    # CZCE旧合约3位码: 品种(1-2字母) + 3位数字
    # 如 SR109, CF109, TA109
    # 第一位=年份个位, 后两位=月份
    # SR109 → 2021年9月, SR909 → 2019年9月, SR009 → 2020年9月
    m = re.match(r'^([A-Za-z]+)(\d{3})$', code)
    if m:
        product = m.group(1)
        num = int(m.group(2))
        year_digit = num // 100
        month = num % 100
        if 1 <= month <= 12:
            # P0-3 fix: 优先用 reference_date 锚定年份
            if reference_date > 0:
                ref_year = reference_date // 10000
            else:
                import time
                ref_year = time.localtime().tm_year
                logger.warning(
                    "parse_contract_code('%s'): 无reference_date, "
                    "fallback到当前年份%d, CZCE 3位码可能误判",
                    code, ref_year,
                )
            # 选择离参考年最近的候选年份
            base = (ref_year // 10) * 10
            candidates = [base - 10 + year_digit, base + year_digit, base + 10 + year_digit]
            # 合约交割月应在 bar 日期之后或同月(合约活跃期)
            # 选使 (candidate, month) 最接近 ref_year 且 >= bar年份-2 的候选
            year = min(candidates, key=lambda y: abs(y - ref_year))
            return product, year, month

    # 标准格式: 品种+4位年月 (如 rb2605, IF2606, AP2605)
    m = re.match(r'^([A-Za-z]+)(\d{4})$', code)
    if m:
        product = m.group(1)
        ym = int(m.group(2))
        year = 2000 + ym // 100
        month = ym % 100
        if 1 <= month <= 12:
            return product, year, month

    return None


def determine_hot_contract(bars_by_code: Dict[str, List[KSBarRecord]],
                           date: int) -> Optional[str]:
    """根据持仓量确定某日的主力合约

    规则: 持仓量最大的合约为主力
    """
    max_oi = -1
    hot_code = None

    for code, bars in bars_by_code.items():
        for bar in bars:
            if bar.date == date and bar.openinterest > max_oi:
                max_oi = bar.openinterest
                hot_code = code

    return hot_code


def _session_time_to_hhmm(value) -> int:
    """Convert session time values like '09:30' to wtpy integer HHMM."""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if ':' in text:
            hour, minute = text.split(':', 1)
            return int(hour) * 100 + int(minute)
        return int(text)
    raise TypeError(f"unsupported session time value: {value!r}")


# ============================================================
# WT 原版 commodities.json 快照 (p2-1 basefiles 生产化)
# ============================================================
# 快照来源: wondertrader/dist/common/commodities.json (87 个品种)
# 升级流程见 references/README.md
_WT_TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'references',
    'wt_commodities_snapshot.json',
)

_WT_TEMPLATE_CACHE: Optional[Dict[str, Dict[str, dict]]] = None

# WT C++ ProductMgr 要求的必需字段
_WT_REQUIRED_FIELDS = (
    'covermode', 'pricemode', 'category', 'trademode', 'precision',
    'pricetick', 'volscale', 'name', 'exchg', 'session', 'holiday',
)

# fallback session: 按交易所选默认（与 sessions.json 中存在的模板一致）
# FD0900 = 纯日盘 09:00 起步, FN2300 = 夜盘 23:00 收, FN0100 = 夜盘 01:00 收, FN0230 = 夜盘 02:30 收, SD0930 = 股指日盘
_FALLBACK_SESSION_BY_EXCHG = {
    'CFFEX': 'SD0930',
    'CZCE': 'FD0900',
    'DCE': 'FN2300',
    'INE': 'FN2300',
    'SHFE': 'FN2300',
    'GFEX': 'FD0900',
}


def _load_wt_template() -> Dict[str, Dict[str, dict]]:
    """读取并缓存 WT 原版 commodities.json 快照。

    返回结构: {EXCHG: {product: {covermode, pricemode, category, ...}}}
    """
    global _WT_TEMPLATE_CACHE
    if _WT_TEMPLATE_CACHE is None:
        if not os.path.isfile(_WT_TEMPLATE_PATH):
            logger.warning(
                "WT commodities 快照不存在: %s — 所有品种将走 fallback。"
                "请确认 references/wt_commodities_snapshot.json 已发布。",
                _WT_TEMPLATE_PATH,
            )
            _WT_TEMPLATE_CACHE = {}
        else:
            try:
                with open(_WT_TEMPLATE_PATH, encoding='utf-8') as fh:
                    _WT_TEMPLATE_CACHE = json.load(fh)
            except (OSError, json.JSONDecodeError) as e:
                logger.error("WT commodities 快照读取失败 (%s): %s", _WT_TEMPLATE_PATH, e)
                _WT_TEMPLATE_CACHE = {}
    return _WT_TEMPLATE_CACHE


def _build_fallback_commodity(exchg: str, product: str) -> dict:
    """快照未覆盖的品种 fallback 字段（仍保证 WT 必需字段齐全）。"""
    return {
        'covermode': 0,
        'pricemode': 0,
        'category': 1,                   # 1=期货 (WT C++ 枚举)
        'trademode': 0,
        'precision': 2,
        'pricetick': 1.0,                # float 占位
        'volscale': 1,
        'name': product,                  # 用 product code 作占位名
        'exchg': exchg,
        'session': _FALLBACK_SESSION_BY_EXCHG.get(exchg, 'FD0900'),
        'holiday': 'CHINA',
    }


# ============================================================================
# p2-2: WT contracts 模板加载 + 按品种派生合约属性
# ----------------------------------------------------------------------------
# WT 模板 contracts.json 922 个合约里, 同品种的合约 maxlimit/maxmarket/margin
# 大多统一; 我们按品种聚合 (取众数) 作为派生属性, 缺失品种 fallback.
# ----------------------------------------------------------------------------
_WT_CONTRACTS_TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'references',
    'wt_contracts_snapshot.json',
)
_WT_CONTRACTS_TEMPLATE_CACHE: Optional[Dict] = None
_WT_CONTRACT_ATTRS_CACHE: Optional[Dict[str, Dict[str, dict]]] = None
_WT_CONTRACT_ATTR_FIELDS = (
    'maxlimitqty', 'maxmarketqty', 'minlimitqty', 'minmarketqty',
    'longmarginratio', 'shortmarginratio',
)


def _load_wt_contracts_template() -> Dict:
    """读取 WT contracts.json 快照, 失败时空 dict + 一次性 error log。"""
    global _WT_CONTRACTS_TEMPLATE_CACHE
    if _WT_CONTRACTS_TEMPLATE_CACHE is None:
        if not os.path.exists(_WT_CONTRACTS_TEMPLATE_PATH):
            logger.warning(
                "WT contracts 快照不存在: %s — 所有品种将走 contracts fallback。"
                "请发布 references/wt_contracts_snapshot.json。",
                _WT_CONTRACTS_TEMPLATE_PATH,
            )
            _WT_CONTRACTS_TEMPLATE_CACHE = {}
        else:
            try:
                with open(_WT_CONTRACTS_TEMPLATE_PATH, encoding='utf-8') as fh:
                    _WT_CONTRACTS_TEMPLATE_CACHE = json.load(fh)
            except (OSError, json.JSONDecodeError) as e:
                logger.error("WT contracts 快照读取失败 (%s): %s",
                             _WT_CONTRACTS_TEMPLATE_PATH, e)
                _WT_CONTRACTS_TEMPLATE_CACHE = {}
    return _WT_CONTRACTS_TEMPLATE_CACHE


def _load_contract_attrs() -> Dict[str, Dict[str, dict]]:
    """按 {EXCHG: {product: {field: 众数值}}} 聚合 WT contracts 模板。

    对每个品种, 聚合该品种下所有合约的 6 个字段并取众数;
    取众数策略避免被少数异常合约 (CFFEX 个别 maxmarket=10) 干扰。
    """
    global _WT_CONTRACT_ATTRS_CACHE
    if _WT_CONTRACT_ATTRS_CACHE is not None:
        return _WT_CONTRACT_ATTRS_CACHE

    from collections import Counter, defaultdict
    template = _load_wt_contracts_template()
    agg: Dict[str, Dict[str, dict]] = defaultdict(lambda: defaultdict(lambda: defaultdict(Counter)))
    for ex, contracts in template.items():
        for code, c in contracts.items():
            p = c.get('product')
            if not p:
                continue
            for f in _WT_CONTRACT_ATTR_FIELDS:
                v = c.get(f)
                if v is not None:
                    agg[ex][p][f][v] += 1

    result: Dict[str, Dict[str, dict]] = {}
    for ex, prods in agg.items():
        result[ex] = {}
        for p, fields in prods.items():
            picked = {}
            for f, counter in fields.items():
                picked[f] = counter.most_common(1)[0][0]
            result[ex][p] = picked
    _WT_CONTRACT_ATTRS_CACHE = result
    return result


_CONTRACT_FALLBACK_ATTRS = {
    'maxlimitqty':      1000,
    'maxmarketqty':     0,
    'minlimitqty':      1,
    'minmarketqty':     1,
    'longmarginratio':  0.1,
    'shortmarginratio': 0.1,
}


def _build_fallback_contract_attrs() -> dict:
    """品种未覆盖时 contracts 字段 fallback 占位 (与历史行为兼容)。"""
    return dict(_CONTRACT_FALLBACK_ATTRS)


# ----------------------------------------------------------------------------
# p2-3: WT fees.json 快照拷贝层 (KS 无手续费, 走 WT 官方模板)
# ----------------------------------------------------------------------------
# WT 模板按 EXCHG.product 平铺 36 个品种, 字段 {open, close, closetoday, byvolume};
# 我们 13 目标品种命中 8/13, 缺失品种 (CFFEX.IH/IM, DCE.b, INE.ec, GFEX.pd)
# fallback 到 (0, 0, 0, False) 并发聚合 warning 提示手动补全。
# ----------------------------------------------------------------------------
_WT_FEES_TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'references',
    'wt_fees_snapshot.json',
)
_WT_FEES_TEMPLATE_CACHE: Optional[Dict] = None
_FEE_FALLBACK_ENTRY = {
    'open':       0.0,
    'close':      0.0,
    'closetoday': 0.0,
    'byvolume':   False,
}


def _load_wt_fees_template() -> Dict:
    """读取 WT fees.json 快照, 失败时空 dict + 一次性 warning log。"""
    global _WT_FEES_TEMPLATE_CACHE
    if _WT_FEES_TEMPLATE_CACHE is None:
        if not os.path.exists(_WT_FEES_TEMPLATE_PATH):
            logger.warning(
                "WT fees 快照不存在: %s — 所有品种将走 fees fallback (全 0)。"
                "请发布 references/wt_fees_snapshot.json。",
                _WT_FEES_TEMPLATE_PATH,
            )
            _WT_FEES_TEMPLATE_CACHE = {}
        else:
            try:
                with open(_WT_FEES_TEMPLATE_PATH, encoding='utf-8') as fh:
                    _WT_FEES_TEMPLATE_CACHE = json.load(fh)
            except (OSError, json.JSONDecodeError) as e:
                logger.error("WT fees 快照读取失败 (%s): %s",
                             _WT_FEES_TEMPLATE_PATH, e)
                _WT_FEES_TEMPLATE_CACHE = {}
    return _WT_FEES_TEMPLATE_CACHE


def _build_fallback_fee_entry() -> dict:
    """品种未覆盖时 fees 占位 (open/close/closetoday=0, byvolume=False)。"""
    return dict(_FEE_FALLBACK_ENTRY)


# ----------------------------------------------------------------------------
# p2-4: WT holidays.json 快照 + 2023-2026 手工扩展
# ----------------------------------------------------------------------------
# WT 原版 holidays.json 顶层 {"CHINA": [YYYYMMDD,...]} 仅覆盖 2008-2022 (265 天);
# commodities.holiday 字段统一引用 "CHINA"; 我们需要补 2023-2026 (KS 不提供节假日)。
# 扩展表 references/extra_holidays_2023_2026.json 按年组织便于人工维护审计。
# ----------------------------------------------------------------------------
_WT_HOLIDAYS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'references',
    'wt_holidays_snapshot.json',
)
_EXTRA_HOLIDAYS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'references',
    'extra_holidays_2023_2026.json',
)
_WT_HOLIDAYS_CACHE: Optional[List[str]] = None
_EXTRA_HOLIDAYS_CACHE: Optional[List[str]] = None


def _load_wt_holidays() -> List[str]:
    """读取 WT holidays.json 快照, 返回 CHINA 日期 list (已排序去重)。"""
    global _WT_HOLIDAYS_CACHE
    if _WT_HOLIDAYS_CACHE is None:
        if not os.path.exists(_WT_HOLIDAYS_PATH):
            logger.warning(
                "WT holidays 快照不存在: %s — 节假日将仅含 extras 部分。",
                _WT_HOLIDAYS_PATH,
            )
            _WT_HOLIDAYS_CACHE = []
        else:
            try:
                with open(_WT_HOLIDAYS_PATH, encoding='utf-8') as fh:
                    data = json.load(fh)
                _WT_HOLIDAYS_CACHE = sorted(set(data.get('CHINA', [])))
            except (OSError, json.JSONDecodeError) as e:
                logger.error("WT holidays 快照读取失败 (%s): %s",
                             _WT_HOLIDAYS_PATH, e)
                _WT_HOLIDAYS_CACHE = []
    return _WT_HOLIDAYS_CACHE


def _load_extra_holidays() -> List[str]:
    """读取 references/extra_holidays_2023_2026.json, 摊平按年表为 list。

    结构: {"CHINA": {"2023": [...], "2024": [...], ...}} (_前缀字段为注释跳过)。
    """
    global _EXTRA_HOLIDAYS_CACHE
    if _EXTRA_HOLIDAYS_CACHE is None:
        if not os.path.exists(_EXTRA_HOLIDAYS_PATH):
            logger.warning(
                "extra_holidays 文件不存在: %s — 2023+ 年份节假日为空, "
                "回测/实盘可能误判交易日。请在 references/ 中维护。",
                _EXTRA_HOLIDAYS_PATH,
            )
            _EXTRA_HOLIDAYS_CACHE = []
        else:
            try:
                with open(_EXTRA_HOLIDAYS_PATH, encoding='utf-8') as fh:
                    data = json.load(fh)
                china = data.get('CHINA', {})
                # 摊平 {year: [dates]} → flat list
                flat: List[str] = []
                for year_key, dates in china.items():
                    if year_key.startswith('_'):
                        continue
                    flat.extend(dates)
                _EXTRA_HOLIDAYS_CACHE = sorted(set(flat))
            except (OSError, json.JSONDecodeError) as e:
                logger.error("extra_holidays 读取失败 (%s): %s",
                             _EXTRA_HOLIDAYS_PATH, e)
                _EXTRA_HOLIDAYS_CACHE = []
    return _EXTRA_HOLIDAYS_CACHE


# ----------------------------------------------------------------------------
# p2-5: WT sessions.json 快照拷贝 (修复命名失配)
# ----------------------------------------------------------------------------
# 老实现自定义命名 (CFFEX_STOCK/SHFE_NIGHT1/...) 但 commodities.session 字段
# 引用的是 WT 命名 (FN0230/SD0930/FD0900) → 命名失配, C++ loader 无法解析。
# 新方案: 拷贝 WT 官方 8 个 session ID (按收盘时间命名), 与 commodities 一致。
# ----------------------------------------------------------------------------
_WT_SESSIONS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'references',
    'wt_sessions_snapshot.json',
)
_WT_SESSIONS_CACHE: Optional[Dict] = None


def _load_wt_sessions() -> Dict:
    """读取 WT sessions.json 快照 (UTF-8), 失败时空 dict + warning log。"""
    global _WT_SESSIONS_CACHE
    if _WT_SESSIONS_CACHE is None:
        if not os.path.exists(_WT_SESSIONS_PATH):
            logger.warning(
                "WT sessions 快照不存在: %s — sessions.json 将为空, "
                "commodities.session 引用全部解析失败。",
                _WT_SESSIONS_PATH,
            )
            _WT_SESSIONS_CACHE = {}
        else:
            try:
                with open(_WT_SESSIONS_PATH, encoding='utf-8') as fh:
                    _WT_SESSIONS_CACHE = json.load(fh)
            except (OSError, json.JSONDecodeError) as e:
                logger.error("WT sessions 快照读取失败 (%s): %s",
                             _WT_SESSIONS_PATH, e)
                _WT_SESSIONS_CACHE = {}
    return _WT_SESSIONS_CACHE


class ConfigGenerator:
    """wtpy基础配置文件生成器"""

    def __init__(self, ks_reader: KSDataReader, output_dir: str):
        self.ks_reader = ks_reader
        self.output_dir = output_dir

    def _iter_products(self, products_filter: Optional[Dict[str, List[str]]] = None,
                        option_products: Optional[Dict[str, List[str]]] = None):
        """按过滤范围迭代品种；未传过滤时迭代全市场期货品种。

        @option_products 纯期权独立品种 (OPTION_ONLY_PRODUCTS),
            走独立流水线 (category=6), 不在此迭代的期货分支中;
            后续在 generate_commodities 中单独追加。
        """
        exchanges = list(products_filter.keys()) if products_filter else EXCHANGES
        # 缺省按 OPTION_ONLY_PRODUCTS 跳过纯期权独立目录 (向后兼容旧测试);
        # 显式传 option_products 时以传入为准。
        from config import OPTION_ONLY_PRODUCTS as _DEFAULT_OPT_ONLY
        _opt = option_products if option_products is not None else _DEFAULT_OPT_ONLY
        for exchg in exchanges:
            products = products_filter.get(exchg, []) if products_filter else FUTURES_PRODUCTS.get(exchg, [])
            for product in products:
                # 仅跳过纯期权独立品种目录 (IO/HO/MO);
                # 共享品种目录 (SHFE/cu 等) 里的期权由 is_option_code() 合约级过滤
                if product in _opt.get(exchg, []):
                    continue
                yield exchg, product

    def generate_commodities(self, products_info: Optional[Dict[str, List[str]]] = None,
                              option_products: Optional[Dict[str, List[str]]] = None) -> Dict:
        """生成 commodities.json — 以 WT 原版快照为权威，未覆盖品种 fallback。

        p2-1 改造：
          - 优先从 references/wt_commodities_snapshot.json 拷贝权威字段
            (category=int, pricetick, volscale, precision, session, holiday)
          - 未覆盖的品种：fallback 占位 + warning（字段仍齐全, 可被 WT 加载）
          - 期权品种 (OPTION_PRODUCTS) 在 _iter_products 中已跳过

        格式: {EXCHG: {PRODUCT: {covermode, pricemode, category, trademode,
                                  precision, pricetick, volscale, name, exchg,
                                  session, holiday}}}
        @products_info 可传 {EXCHG: [PRODUCT, ...]} 限定生成范围。
        @option_products 纯期权独立品种 (如 {'CFFEX':['IO','HO','MO']}),
            category=6 (CC_SpotOption)。
        """
        commodities: Dict[str, Dict[str, dict]] = {}
        template = _load_wt_template()
        snapshot_hits = 0
        fallback_used: List[str] = []

        # ---- 期货品种 ----
        for exchg, product in self._iter_products(products_info, option_products):
            commodities.setdefault(exchg, {})

            tpl_info = template.get(exchg, {}).get(product)
            if tpl_info is not None:
                # 拷贝快照（避免外部修改污染缓存），强制 exchg/holiday 正确
                info = dict(tpl_info)
                info['exchg'] = exchg
                info.setdefault('holiday', 'CHINA')
                # 补齐任何缺失的必需字段（理论上快照都有，但稳健起见）
                fallback = _build_fallback_commodity(exchg, product)
                for k in _WT_REQUIRED_FIELDS:
                    info.setdefault(k, fallback[k])
                commodities[exchg][product] = info
                snapshot_hits += 1
            else:
                commodities[exchg][product] = _build_fallback_commodity(exchg, product)
                fallback_used.append(f'{exchg}.{product}')

        # ---- 纯期权独立品种 (CFFEX IO/HO/MO 等) ----
        # category=6 (CC_SpotOption), 其余字段走 fallback + template
        for exchg, opt_prods in (option_products or {}).items():
            commodities.setdefault(exchg, {})
            for product in opt_prods:
                tpl_info = template.get(exchg, {}).get(product)
                if tpl_info is not None:
                    info = dict(tpl_info)
                    info['exchg'] = exchg
                    info['category'] = 6  # CC_SpotOption — 股指期权
                    info.setdefault('holiday', 'CHINA')
                    fallback = _build_fallback_commodity(exchg, product)
                    for k in _WT_REQUIRED_FIELDS:
                        info.setdefault(k, fallback[k])
                    commodities[exchg][product] = info
                    snapshot_hits += 1
                else:
                    info = _build_fallback_commodity(exchg, product)
                    info['category'] = 6  # CC_SpotOption — 股指期权
                    commodities[exchg][product] = info
                    fallback_used.append(f'{exchg}.{product}(option)')

        if fallback_used:
            logger.warning(
                "%d 个品种未在 WT commodities 快照中，使用 fallback 占位字段 (pricetick/volscale 不可靠): %s. "
                "请在 references/wt_commodities_snapshot.json 中补充权威值。",
                len(fallback_used),
                ', '.join(fallback_used),
            )
        if snapshot_hits:
            logger.info("commodities.json: %d 个品种从 WT 快照拷贝权威字段", snapshot_hits)

        return commodities

    def generate_contracts(self, codes_info: Optional[Dict[str, List[str]]] = None,
                           bars_by_product: Optional[Dict[str, Dict[str, List[KSBarRecord]]]] = None) -> Dict:
        """生成contracts.json

        格式: {EXCHG: {CODE: {name, code, exchg, product, maxlimitqty, ...}}}

        p2-2 改造 (方案 A):
          - name: 优先用 commodities[exchg][product].name (中文) + YYMM,
            如 "沪银2603"; 缺失时回退到英文 product+YYYY+MM
          - maxlimitqty/maxmarketqty/minlimitqty/minmarketqty/long/shortmarginratio:
            按品种从 WT contracts 模板众数派生; 未覆盖品种 fallback (1000/0/1/1/0.1/0.1)
            + warning
          - opendate/expiredate: 保留 KS bars 首末日期 (KS 数据覆盖期作为合约存在期近似;
            注意活跃合约 expiredate = 最后数据日, 不是真实到期日 — HOT 切换应由
            hots.json 主导, 不要依赖 expiredate)

        @codes_info 从KS日线数据中提取的合约信息，或 {EXCHG: [PRODUCT, ...]} 过滤范围
        """
        contracts: Dict[str, Dict[str, dict]] = {}
        commodities_map = self.generate_commodities(codes_info)
        attrs_map = _load_contract_attrs()
        attr_fallback_products: List[str] = []

        for exchg, product in self._iter_products(codes_info):
            contracts.setdefault(exchg, {})
            # 从KS数据中获取合约列表
            codes = self.ks_reader.list_day_codes(exchg, product)
            product_bars = (bars_by_product or {}).get(f'{exchg}.{product}', {})

            attrs = attrs_map.get(exchg, {}).get(product)
            if attrs is None:
                attrs = _build_fallback_contract_attrs()
                attr_fallback_products.append(f'{exchg}.{product}')

            # commodities 中文名 (失败回退英文)
            product_name_cn = (
                commodities_map.get(exchg, {}).get(product, {}).get('name')
                or product
            )

            for code in codes:
                # KS日线包里可能包含 *_main、*_index、SPD/IPS组合等非可交易期货合约
                if code.endswith('_main') or code.endswith('_index') or ' ' in code or '&' in code:
                    continue
                if self.ks_reader.is_option_code(exchg, product, code):
                    continue

                parsed = parse_contract_code(code)  # 3位码会 fallback 到当前年
                if parsed is None:
                    logger.warning(f"无法解析合约代码: {exchg}.{code}")
                    continue

                _, year, month = parsed
                bars = product_bars.get(code, [])
                valid_dates = sorted({bar.date for bar in bars if getattr(bar, 'date', 0)})
                # P0-3 fix: 用 bar 首日锚定 CZCE 3位码年份
                if valid_dates and re.match(r'^[A-Za-z]+\d{3}$', code):
                    ref_parsed = parse_contract_code(code, reference_date=valid_dates[0])
                    if ref_parsed:
                        _, year, month = ref_parsed
                open_date = valid_dates[0] if valid_dates else 0
                expire_date = valid_dates[-1] if valid_dates else year * 10000 + month * 100 + 15

                contracts[exchg][code] = {
                    'name': f'{product_name_cn}{year % 100:02d}{month:02d}',
                    'code': code,
                    'exchg': exchg,
                    'product': product,
                    'maxlimitqty':      int(attrs['maxlimitqty']),
                    'maxmarketqty':     int(attrs['maxmarketqty']),
                    'minlimitqty':      int(attrs['minlimitqty']),
                    'minmarketqty':     int(attrs['minmarketqty']),
                    'opendate':         int(open_date),
                    'expiredate':       int(expire_date),
                    'longmarginratio':  float(attrs['longmarginratio']),
                    'shortmarginratio': float(attrs['shortmarginratio']),
                }

        if attr_fallback_products:
            logger.warning(
                "%d 个品种未在 WT contracts 模板中覆盖, 使用 contracts fallback "
                "(maxlimit=1000/maxmarket=0/margin=0.1): %s. "
                "请在 references/wt_contracts_snapshot.json 中补充该品种的真实合约样本。",
                len(attr_fallback_products),
                ', '.join(attr_fallback_products),
            )

        return contracts

    def generate_hots(self, bars_by_product: Dict[str, Dict[str, List[KSBarRecord]]] = None) -> Dict:
        """生成hots.json (主力合约切换历史)

        匹配 WT C++ WTSHotMgr::loadCustomRules (WTSHotMgr.cpp:198-257) 规范:
            {EXCHG: {PRODUCT: [ {date, from, to, oldclose, newclose}, ... ]}}

        - 顶层按 EXCHG 嵌套（不能用 'EXCHG.product' 平铺）
        - 每个 PRODUCT 对应一个**切换事件 list**（按 date 升序）
        - 每条事件必含 5 字段：
            date     切换当日 YYYYMMDD（int）
            from     切换前主力 raw code（首条 = ''）
            to       切换后新主力 raw code
            oldclose 切换前一日**旧主力**收盘价（首条 = 0.0）
            newclose 切换当日**新主力**收盘价
          HotMgr 计算复权因子: factor *= oldclose / newclose

        @bars_by_product 从KS日线数据中提取的 {exchg.product: {code: [bars]}}
        """
        hots: Dict[str, Dict[str, List[Dict]]] = {}

        if bars_by_product is None:
            return hots

        for key, code_bars in bars_by_product.items():
            parts = key.split('.')
            if len(parts) != 2:
                continue
            exchg, product = parts
            tradable_code_bars = {
                code: bars
                for code, bars in code_bars.items()
                if not code.endswith('_main')
                and not code.endswith('_index')
                and ' ' not in code
                and '&' not in code
            }
            if not tradable_code_bars:
                continue

            # 收集所有交易日期
            all_dates = set()
            for bars in tradable_code_bars.values():
                for bar in bars:
                    all_dates.add(bar.date)
            if not all_dates:
                continue
            sorted_dates = sorted(all_dates)

            # 为快速取某 code 某日 close 建索引
            close_by_code_date: Dict[str, Dict[int, float]] = {}
            for code, bars in tradable_code_bars.items():
                close_by_code_date[code] = {b.date: b.close for b in bars}

            # 逐日确定主力合约，记录切换事件
            switch_events: List[Dict] = []
            prev_hot: Optional[str] = None
            for idx, date in enumerate(sorted_dates):
                hot_code = determine_hot_contract(tradable_code_bars, date)
                if not hot_code:
                    continue

                if prev_hot is None:
                    # initial 事件: from='', oldclose=0.0
                    new_close = close_by_code_date.get(hot_code, {}).get(date, 0.0)
                    switch_events.append({
                        "date": int(date),
                        "from": "",
                        "to": hot_code,
                        "oldclose": 0.0,
                        "newclose": float(new_close),
                    })
                    prev_hot = hot_code
                elif hot_code != prev_hot:
                    # 切换: oldclose=旧主力切换前最后交易日收盘, newclose=新主力切换当日收盘
                    # P0-2 fix: 向前回溯找旧主力实际最后一个有 bar 的日期，
                    # 而非简单用 sorted_dates[idx-1]（可能假日/退市导致无数据→0.0→复权因子归零）
                    prev_close_map = close_by_code_date.get(prev_hot, {})
                    old_close = 0.0
                    for d in reversed(sorted_dates[:idx]):
                        if d in prev_close_map:
                            old_close = float(prev_close_map[d])
                            break
                    new_close = float(close_by_code_date.get(hot_code, {}).get(date, 0.0))
                    if old_close == 0.0:
                        logger.warning(
                            "hots.json: %s.%s 切换日 %d 旧主力 %s 无历史close, "
                            "oldclose=0.0将导致复权因子归零",
                            exchg, product, date, prev_hot,
                        )
                    if new_close == 0.0:
                        logger.warning(
                            "hots.json: %s.%s 切换日 %d 新主力 %s 无当日close, "
                            "newclose=0.0将导致复权因子异常",
                            exchg, product, date, hot_code,
                        )
                    switch_events.append({
                        "date": int(date),
                        "from": prev_hot,
                        "to": hot_code,
                        "oldclose": float(old_close),
                        "newclose": float(new_close),
                    })
                    prev_hot = hot_code

            if switch_events:
                hots.setdefault(exchg, {})[product] = switch_events

        return hots

    def generate_sessions(self) -> Dict:
        """生成 sessions.json (交易时间模板)。

        采用 WT 官方 8 个 session ID (按收盘时间命名):
          FD0900/FD0915 - 期货白盘 (15:00 / 15:15 收)
          SD0930        - 股票白盘
          FN0100/FN0230/FN2300/FN2330 - 期货夜盘 (按收盘时间区分品种)
          TRADING       - 全天交易模板

        commodities.session 字段引用这 8 个 ID 之一, 必须命名一致才能在
        C++ loader 中解析到。老实现自定义命名 (CFFEX_STOCK/SHFE_NIGHT1/...)
        与 commodities 失配, 已废弃。
        """
        template = _load_wt_sessions()
        # 复制快照避免被外部修改污染缓存
        return {sid: dict(sess) for sid, sess in template.items()}

    def generate_holidays(self) -> Dict:
        """生成 holidays.json (节假日)。

        顶层结构必须是 {"CHINA": [YYYYMMDD,...]} 才能匹配 commodities.holiday='CHINA'。

        数据源:
          - WT 官方 holidays.json 快照 (2008-2022, 265 天)
          - references/extra_holidays_2023_2026.json (国务院发文手工维护, KS 不提供)

        升级流程: 每年 11-12 月国务院发布次年放假安排后, 更新 extras 文件即可。
        """
        wt_dates    = _load_wt_holidays()      # 2008-2022
        extra_dates = _load_extra_holidays()   # 2023-2026
        merged = sorted(set(wt_dates) | set(extra_dates))
        return {'CHINA': merged}

    def generate_fees(self, products_filter: Optional[Dict[str, List[str]]] = None) -> Dict:
        """生成 fees.json (手续费)。

        KS 数据本身不提供手续费, 走 WT 官方 fees.json 快照拷贝模式:
          - WT 模板覆盖品种: 拷贝快照值 {open, close, closetoday, byvolume}
          - 未覆盖品种: fallback (全 0.0 + byvolume=False) + 聚合 warning,
            提醒用户在 references/wt_fees_snapshot.json 中手动补充或在产物中编辑。

        字段名遵循 WT 标准: byvolume (bool) - True=按手数固定, False=按比例。
        历史实现的 'mode': 'ratio' 字段已废弃, 不再生成。
        """
        template = _load_wt_fees_template()
        fees: Dict[str, Dict] = {}
        missing: List[str] = []
        for exchg, product in self._iter_products(products_filter):
            key = f'{exchg}.{product}'
            tpl = template.get(key)
            if tpl is not None:
                fees[key] = {
                    'open':       float(tpl.get('open',       0.0)),
                    'close':      float(tpl.get('close',      0.0)),
                    'closetoday': float(tpl.get('closetoday', 0.0)),
                    'byvolume':   bool(tpl.get('byvolume',    False)),
                }
            else:
                fees[key] = _build_fallback_fee_entry()
                missing.append(key)
        if missing:
            logger.warning(
                "fees.json 有 %d 个品种走 fallback (open/close/closetoday 均为 0): %s. "
                "请在 references/wt_fees_snapshot.json 中补全, "
                "或人工编辑生成产物 common/fees.json.",
                len(missing), ', '.join(sorted(missing)),
            )
        return fees

    def generate_all(self, bars_by_product: Optional[Dict] = None,
                      products_filter: Optional[Dict[str, List[str]]] = None,
                      option_products: Optional[Dict[str, List[str]]] = None) -> Dict[str, Dict]:
        """生成所有配置文件

        @option_products 纯期权独立品种 (如 {'CFFEX':['IO','HO','MO']}),
            仅写入 commodities.json (category=6, CC_SpotOption);
            contracts/hots/fees 暂不为期权生成 (期权合约由 contracts 走 bars_by_product 自动包含,
            主连/手续费按需后续扩展)。
        返回: {文件名: 配置内容}
        """
        result = {}

        logger.info("生成 commodities.json ...")
        result['commodities.json'] = self.generate_commodities(
            products_filter, option_products=option_products)

        logger.info("生成 contracts.json ...")
        result['contracts.json'] = self.generate_contracts(products_filter, bars_by_product)

        logger.info("生成 hots.json ...")
        result['hots.json'] = self.generate_hots(bars_by_product)

        logger.info("生成 sessions.json ...")
        result['sessions.json'] = self.generate_sessions()

        logger.info("生成 holidays.json ...")
        result['holidays.json'] = self.generate_holidays()

        logger.info("生成 fees.json ...")
        result['fees.json'] = self.generate_fees(products_filter)

        return result

    def save_all(self, configs: Dict[str, Dict]):
        """保存所有配置文件到磁盘"""
        os.makedirs(self.output_dir, exist_ok=True)

        for filename, content in configs.items():
            filepath = os.path.join(self.output_dir, filename)
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(content, f, ensure_ascii=False, indent=2)
            logger.info(f"保存配置文件: {filepath}")

    def collect_bars_for_hots(self, exchanges: List[str] = None,
                               products: Dict[str, List[str]] = None) -> Dict:
        """从KS日线数据中收集品种数据，用于生成hots.json

        返回: {EXCHG.PRODUCT: {code: [KSBarRecord]}}
        """
        if exchanges is None:
            exchanges = EXCHANGES

        bars_by_product = {}

        for exchg in exchanges:
            prods = products.get(exchg, []) if products else FUTURES_PRODUCTS.get(exchg, [])
            for product in prods:
                if product in OPTION_PRODUCTS.get(exchg, []):
                    continue

                logger.info(f"收集日线数据: {exchg}.{product}")
                code_bars = self.ks_reader.read_all_codes_day(exchg, product)
                if code_bars:
                    key = f"{exchg}.{product}"
                    bars_by_product[key] = code_bars

        return bars_by_product
