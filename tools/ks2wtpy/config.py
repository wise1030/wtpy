#!/usr/bin/env python3
"""
KS数据 → wtpy回测数据 转换系统 - 配置模块

KS数据源: 远程服务器 ks-data/{EXCHG}/{PRODUCT}/L1/{kline_type}/
wtpy目标: CSV模式(csv/) + LMDB模式(min5/day/min1/)

支持增量更新: 记录已转换的最新日期，每日KS更新后只转换新增数据
"""

import os
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Set

# ============================================================
# 交易所映射: KS目录名 → wtpy交易所名 (一致，无需映射)
# ============================================================
EXCHANGES = ['CFFEX', 'CZCE', 'DCE', 'GFEX', 'INE', 'SHFE']

# 期权品种说明（KS S3 真实布局，2026-05-29 验证）
#
# ⚠️ 关键修正：is_option_code() 不再用 OPTION_PRODUCTS 列表做整产品判定（除 CFFEX 三品种外）。
# 详见 s3_reader.py / ks_reader.py 中的 is_option_code() 注释。
#
# 两类 product 目录的真实布局：
#
# 1) 纯期权独立目录（合约名形如 IO2509-C-4250，含 IO_main 合成主连）：
#    - CFFEX: IO / HO / MO
#
# 2) 期货+期权共享品种目录（按合约代码模式过滤期权，期货合约保留）：
#    - SHFE: op(双胶纸)/cu/al/zn/pb/ni/sn/au/ag/ru/rb/sp/ss/hc/bu/fu/bc
#      注：op 是双胶纸期货品种(与 cu/al/rb 平级)，其期权合约(op2601C3900 等)按代码模式过滤
#    - CZCE: AP/CF/FG/MA/OI/PF/PM/RM/SA/SH/SR/TA/UR/WH/ZC 等
#    - DCE:  a/b/c/m/p/y/i/j/jm/eg/eb/pg/pp/l/v/rr/lh
#    - INE:  sc
#
# 以下 OPTION_PRODUCTS 列表仅作"该品种有期权数据需过滤"的标识/文档用，
# is_option_code() 仅对 CFFEX 三品种走整产品判定，其余走合约代码模式。
# main.py:1235 的过滤逻辑用 FUTURES_PRODUCTS ∩ OPTION_PRODUCTS 排除同名期货品种
# （如 CZCE/AP 同时在期货列表和期权列表中 → 默认不转 AP 的期货数据，需
# include_option 才转期权，但期货数据也不转——这是一个已知的局限）。
OPTION_PRODUCTS = {
    'CFFEX': ['IO', 'HO', 'MO'],   # 股指期权（纯期权独立目录，整产品判定有效）
    'SHFE':  ['op', 'cu', 'al', 'zn', 'pb', 'ni', 'sn', 'au', 'ag', 'ru', 'rb',
              'sp', 'ss', 'hc', 'bu', 'fu', 'bc'],  # 与期货共享目录；op=双胶纸
    'DCE':   ['a', 'b', 'c', 'm', 'p', 'y', 'i', 'j', 'jm',
              'eg', 'eb', 'pg', 'pp', 'l', 'v', 'rr', 'lh'],  # 与期货共享目录
    'CZCE':  ['AP', 'CF', 'CZ', 'FG', 'MA', 'OI', 'PF', 'PM', 'RM',
              'SA', 'SR', 'TA', 'WH', 'ZC', 'UR', 'SH'],      # 与期货共享目录
    'INE':   ['sc'],                # 原油期权（与期货共享目录）
    'GFEX':  [],
}

# 纯期权独立品种目录（无期货合约，目录里只有期权 + 合成主连）。
# 这些品种不在 FUTURES_PRODUCTS 中，需通过 include_cffex_options 开关
# 显式启用独立流水线 (option_only 模式)：
#   - 整目录全收，无视 is_option_code() 判断
#   - 排除 *_main / *_index 合成主连/指数
#   - ConfigGenerator 以 CC_SpotOption (category=6) 写入 commodities
#     （股指期权专用枚举，参考 WTSTypes.h CC_SpotOption=6）
OPTION_ONLY_PRODUCTS = {
    'CFFEX': ['IO', 'HO', 'MO'],   # 沪深300/上证50/中证1000 股指期权
}

# 需要转换的K线周期
# KS目录名 → (wtpy周期后缀, LMDB子目录名, wtpy period枚举)
PERIOD_MAP = {
    'kline_1d': ('d', 'day', 'day'),
    'kline_1m': ('m1', 'min1', 'min1'),
    'kline_5m': ('m5', 'min5', 'min5'),
    'kline_15m': ('m15', None, None),   # wtpy不原生支持15m
    'kline_30m': ('m30', None, None),   # wtpy不原生支持30m
    'kline_1h':  ('h1', None, None),    # wtpy不原生支持1h
}

# wtpy回测原生支持的周期
WTPY_SUPPORTED_PERIODS = {'kline_1d', 'kline_1m', 'kline_5m'}

# ============================================================
# WTSBarStruct 二进制布局 (pack 8, 共88字节)
# ============================================================
WTSBAR_STRUCT_SIZE = 88  # bytes (pack 8: 4+4+8+8*9=88)

# WTSBarStruct字段布局:
# offset  size  field
# 0       4     date (uint32)
# 4       4     reserve (uint32)
# 8       8     time (uint64)
# 16      8     open (float64)
# 24      8     high (float64)
# 32      8     low (float64)
# 40      8     close (float64)
# 48      8     settle (float64)
# 56      8     money/turnover (float64)
# 64      8     vol (float64)
# 72      8     hold/open_interest (float64)
# 80      8     add/diff_interest (float64)
# 总大小: 88 bytes

# LMDB Key布局
# LMDBBarKey: exchg[16] + code[32] + bartime(uint32大端) = 52 bytes
LMDB_BAR_KEY_SIZE = 52
MAX_EXCHANGE_LEN = 16
MAX_INSTRUMENT_LEN = 32

# LMDB mapsize
LMDB_KLINE_MAPSIZE = 512 * 1024 * 1024   # 512MB per exchange DB (100品种+万合约)
LMDB_TICK_MAPSIZE = 16 * 1024 * 1024      # 16MB per contract DB

# DSB文件头
BLK_FLAG = b'&^%$#@!\x00'
BLOCK_VERSION_CMP_V2 = 0x04
BT_HIS_MINUTE1 = 21
BT_HIS_MINUTE5 = 22
BT_HIS_DAY = 23
BT_HIS_TICKS = 24

# ============================================================
# WTSTickStruct 二进制布局 (pack 8, 共512字节)
# ============================================================
WTS_TICK_STRUCT_SIZE = 512  # bytes

# WTSTickStruct字段布局 (pack 8):
# offset  size  field
# 0       16    exchg (char[16])
# 16      32    code (char[32])
# 48      8     price (float64)
# 56      8     open (float64)
# 64      8     high (float64)
# 72      8     low (float64)
# 80      8     settle_price (float64)
# 88      8     upper_limit (float64)
# 96      8     lower_limit (float64)
# 104     8     total_volume (float64)
# 112     8     volume (float64)
# 120     8     total_turnover (float64)
# 128     8     turn_over (float64)
# 136     8     open_interest (float64)
# 144     8     diff_interest (float64)
# 152     4     trading_date (uint32)
# 156     4     action_date (uint32)
# 160     4     action_time (uint32)  HHMMSSMMM
# 164     4     reserve (uint32)
# --- padding 4 bytes ---
# 168     8     pre_close (float64)
# 176     8     pre_settle (float64)
# 184     8     pre_interest (float64)
# 192     80    bid_price[10] (10 * float64)
# 272     80    ask_price[10] (10 * float64)
# 352     80    bid_qty[10] (10 * float64)
# 432     80    ask_qty[10] (10 * float64)
# 总大小: 512 bytes

# KS tick 数据买卖档位数
KS_TICK_BID_ASK_LEVELS_FULL = 5   # SHFE/DCE/INE/GFEX: 5档
KS_TICK_BID_ASK_LEVELS_MINIMAL = 1  # CZCE/CFFEX: 1档


@dataclass
class ConvertConfig:
    """转换配置"""
    # 数据源连接信息：必须通过CLI参数、环境变量或外部配置显式提供，避免源码内置主机/凭据
    remote_host: str = ''
    remote_user: str = ''
    remote_ks_base: str = ''
    
    # 本地输出目录
    local_output_base: str = ''  # 运行时设置
    
    # CSV模式输出
    csv_output_dir: str = ''     # {base}/csv/
    # LMDB模式输出
    lmdb_output_dir: str = ''    # {base}/lmdb/
    # DSB模式输出
    dsb_output_dir: str = ''     # {base}/his/
    # 基础配置输出
    config_output_dir: str = ''  # {base}/common/
    # 增量状态文件
    state_file: str = ''         # {base}/convert_state.json
    
    # 转换选项
    enable_csv: bool = True
    enable_lmdb: bool = True
    enable_dsb: bool = True       # CSV→DSB转换
    enable_config: bool = True    # 生成基础配置文件
    is_remote: bool = False       # 是否远程读取
    
    # 数据过滤
    exchanges: List[str] = field(default_factory=lambda: EXCHANGES[:])
    periods: List[str] = field(default_factory=lambda: ['kline_1d', 'kline_5m'])
    include_option: bool = False  # 是否包含期权数据（共享目录品种期权过滤开关）
    include_cffex_options: bool = False  # 是否启用 CFFEX 纯期权独立流水线 (IO/HO/MO)
    specific_products: Optional[Dict[str, List[str]]] = None  # 指定品种 {exchg: [product, ...]}
    
    # S3数据源 (MinIO)
    use_s3: bool = False                          # 使用S3而非SSH
    s3_endpoint: str = ''
    s3_access_key: str = ''
    s3_secret_key: str = ''
    s3_bucket: str = 'ks-data'
    s3_secure: bool = False
    s3_cache_dir: str = ''                        # S3本地缓存目录, 留空自动生成

    # 增量更新
    incremental: bool = True     # 增量模式
    force_full: bool = False     # 强制全量转换
    
    def __post_init__(self):
        if not self.local_output_base:
            self.local_output_base = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'storage', 'ks_converted'
            )
        self._fill_output_paths()

    def _fill_output_paths(self):
        if not self.csv_output_dir:
            self.csv_output_dir = os.path.join(self.local_output_base, 'csv')
        if not self.lmdb_output_dir:
            self.lmdb_output_dir = os.path.join(self.local_output_base, 'lmdb')
        if not self.dsb_output_dir:
            self.dsb_output_dir = os.path.join(self.local_output_base, 'his')
        if not self.config_output_dir:
            self.config_output_dir = os.path.join(self.local_output_base, 'common')
        if not self.state_file:
            self.state_file = os.path.join(self.local_output_base, 'convert_state.json')
        if not self.s3_cache_dir and self.use_s3:
            self.s3_cache_dir = os.path.join(self.local_output_base, 's3_cache')

    def set_output_base(self, output_base: str):
        self.local_output_base = output_base
        self.csv_output_dir = os.path.join(self.local_output_base, 'csv')
        self.lmdb_output_dir = os.path.join(self.local_output_base, 'lmdb')
        self.dsb_output_dir = os.path.join(self.local_output_base, 'his')
        self.config_output_dir = os.path.join(self.local_output_base, 'common')
        self.state_file = os.path.join(self.local_output_base, 'convert_state.json')
        if self.use_s3:
            self.s3_cache_dir = os.path.join(self.local_output_base, 's3_cache')


# ============================================================
# 品种信息 (从wtpy commodities.json提取)
# ============================================================
# 品种类型: futures/option/stock/etf
PRODUCT_TYPE_FUTURES = 'futures'
PRODUCT_TYPE_OPTION = 'option'

# 期货品种→交易所映射 (从KS目录结构确认)
FUTURES_PRODUCTS = {
    'CFFEX': ['IF', 'IH', 'IC', 'IM', 'T', 'TF', 'TS', 'TL'],
    'DCE': ['a', 'b', 'bb', 'bz', 'c', 'cs', 'eb', 'eg', 'fb', 'i', 'j', 'jd', 'jm',
            'l', 'l_f', 'lg', 'lh', 'm', 'p', 'pg', 'pp', 'pp_f', 'rr', 'v', 'v_f', 'y'],
    'SHFE': ['ad', 'ag', 'al', 'ao', 'au', 'br', 'bu', 'cu', 'fu', 'hc', 'ni', 'op', 'pb',
             'rb', 'ru', 'sn', 'sp', 'ss', 'wr', 'zn'],
    'CZCE': ['AP', 'CF', 'CJ', 'CY', 'ER', 'FG', 'JR', 'LR', 'MA', 'ME', 'OI', 'PF',
             'PK', 'PL', 'PM', 'PR', 'PX', 'RI', 'RM', 'RO', 'RS', 'SA', 'SF', 'SH',
             'SM', 'SR', 'TA', 'TC', 'UR', 'WH', 'WS', 'WT', 'ZC'],
    'INE': ['bc', 'ec', 'lu', 'nr', 'sc'],
    'GFEX': ['lc', 'pd', 'ps', 'pt', 'si'],
}

# 交易时间模板 (主要品种)
# 格式: [(开市时间, 收市时间), ...] 时间为HH:MM
SESSION_TEMPLATES = {
    # 日盘品种
    'day_only': {
        'CFFEX': ['IF', 'IH', 'IC', 'IM', 'T', 'TF', 'TS', 'TL'],
        'CZCE': ['WH', 'PM', 'RI', 'RS', 'LR', 'JR', 'ER', 'WS', 'WT'],
    },
    # 日盘+夜盘品种
    'day_night': {
        'SHFE': ['rb', 'ru', 'bu', 'hc', 'fu', 'sp', 'ag', 'au', 'cu', 'al', 'zn', 'ni', 'sn', 'pb', 'ss', 'br', 'ao', 'ad'],
        'DCE': ['i', 'j', 'jm', 'm', 'y', 'p', 'a', 'b', 'bb', 'c', 'cs', 'eg', 'eb', 'fb', 'pg', 'pp', 'v', 'rr', 'l', 'lh', 'lg'],
        'CZCE': ['AP', 'CF', 'CJ', 'CY', 'FG', 'MA', 'OI', 'PF', 'PK', 'PX', 'RM', 'SA', 'SF', 'SM', 'SR', 'TA', 'TC', 'UR', 'ZC', 'SH', 'PL', 'PR', 'RO', 'ME'],
        'INE': ['sc', 'nr', 'lu', 'bc', 'ec'],
        'GFEX': ['lc', 'pd', 'ps', 'pt', 'si'],
    },
}
