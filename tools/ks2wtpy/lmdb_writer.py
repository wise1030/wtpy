#!/usr/bin/env python3
"""
wtpy LMDB格式转换器 (方案B)

将KS K线数据直接写入WonderTrader LMDB数据库格式，
使回测引擎可通过WtDataStorageAD模块直接读取。

LMDB目录结构 (WtDataWriterAD/WtBtDtReaderAD期望的):
  {base_dir}/min1/{EXCHG}/data.mdb    — 每交易所一个LMDB数据库
  {base_dir}/min5/{EXCHG}/data.mdb    — 每交易所一个LMDB数据库
  {base_dir}/day/{EXCHG}/data.mdb     — 每交易所一个LMDB数据库

LMDB Key: LMDBBarKey (52 bytes, pack 1)
  exchg[16] + code[32] + bartime(uint32_t大端)
  bartime: 日线=YYYYMMDD, 分钟线=(date-19900000)*10000+time

LMDB Value: WTSBarStruct (88 bytes, pack 8)
  date(u32) + reserve(u32) + time(u64) + open/high/low/close/settle/money/vol/hold/add(各f64)

回测配置:
  mode: wtp
  store: {path: "...", module: "WtDataStorageAD"}
"""

import os
import struct
import logging
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

try:
    import lmdb
    HAS_LMDB = True
except ImportError:
    HAS_LMDB = False

from config import (
    PERIOD_MAP, WTPY_SUPPORTED_PERIODS,
    MAX_EXCHANGE_LEN, MAX_INSTRUMENT_LEN,
    LMDB_KLINE_MAPSIZE, WTSBAR_STRUCT_SIZE,
    LMDB_BAR_KEY_SIZE,
)
from ks_reader import KSDataReader, KSBarRecord
from standardizer import bar_time_key, minute_bar_time

logger = logging.getLogger(__name__)


# ============================================================
# WTSBarStruct 二进制编码 (pack 8 compatible, 88 bytes)
# ============================================================
# 字段: date(u32), reserve(u32), time(u64), open(f64), high(f64),
#       low(f64), close(f64), settle(f64), money(f64), vol(f64),
#       hold(f64), add(f64)
# 当前 C++ WTSBarStruct 与本工具均按紧凑布局写入:
#   date(4) + reserve(4) + time(8) + 9 * double(8) = 88 bytes
# 注意: 旧文档/旧注释中可能出现 96 bytes，是文档漂移，不能作为实现依据。

def encode_wts_bar_struct(bar: KSBarRecord, is_day: bool = False) -> bytes:
    """将KSBarRecord编码为WTSBarStruct二进制 (88 bytes, pack 8)

    @bar KS K线记录
    @is_day 是否日线
    """
    # time字段计算：统一复用 standardizer，避免 LMDB/DSB 时间语义漂移
    time_val = bar_time_key(bar, is_day=is_day)

    # pack(8)对齐的二进制布局 (88 bytes)
    # date(u32) + reserve(u32) + time(u64) + 9个f64
    # offset: 0=date(4), 4=reserve(4), 8=time(8), 16=open(8), 24=high(8),
    #         32=low(8), 40=close(8), 48=settle(8), 56=money(8), 64=vol(8),
    #         72=hold(8), 80=add(8) → total=88
    buf = struct.pack('<I', bar.date)         # date: uint32, offset 0
    buf += struct.pack('<I', 0)               # reserve: uint32, offset 4
    buf += struct.pack('<Q', time_val)         # time: uint64, offset 8
    buf += struct.pack('<d', bar.open)         # open: float64, offset 16
    buf += struct.pack('<d', bar.high)         # high: float64, offset 24
    buf += struct.pack('<d', bar.low)          # low: float64, offset 32
    buf += struct.pack('<d', bar.close)        # close: float64, offset 40
    buf += struct.pack('<d', 0.0)              # settle: float64, offset 48 (KS无结算价)
    buf += struct.pack('<d', bar.amount)       # money/turnover: float64, offset 56
    buf += struct.pack('<d', bar.volume)       # vol: float64, offset 64
    buf += struct.pack('<d', bar.openinterest) # hold/open_interest: float64, offset 72
    buf += struct.pack('<d', 0.0)              # add/diff_interest: float64, offset 80 (KS无增减量)

    # 验证大小
    assert len(buf) == WTSBAR_STRUCT_SIZE, f"WTSBarStruct大小错误: {len(buf)} != {WTSBAR_STRUCT_SIZE}"

    return buf


def encode_lmdb_bar_key(exchg: str, code: str, bartime: int) -> bytes:
    """编码LMDB K线Key (LMDBBarKey, 52 bytes, pack 1)

    @exchg 交易所代码 (最长16字节)
    @code  合约代码 (最长32字节)
    @bartime 大端uint32: 日线=YYYYMMDD, 分钟线=(date-19900000)*10000+time
    """
    exchg_bytes = exchg.encode('utf-8')[:MAX_EXCHANGE_LEN]
    exchg_bytes = exchg_bytes.ljust(MAX_EXCHANGE_LEN, b'\x00')

    code_bytes = code.encode('utf-8')[:MAX_INSTRUMENT_LEN]
    code_bytes = code_bytes.ljust(MAX_INSTRUMENT_LEN, b'\x00')

    bartime_bytes = struct.pack('>I', bartime)  # 大端uint32

    key = exchg_bytes + code_bytes + bartime_bytes
    assert len(key) == LMDB_BAR_KEY_SIZE, f"LMDBBarKey大小错误: {len(key)} != {LMDB_BAR_KEY_SIZE}"

    return key


def time_to_min_bar(date: int, time: int) -> int:
    """WonderTrader timeToMinBar计算

    公式: (date - 19900000) * 10000 + time
    例: date=20210104, time=930 → 2101040930
    """
    return minute_bar_time(date, time)


class LmdbConverter:
    """KS → wtpy LMDB 转换器"""

    def __init__(self, ks_reader: KSDataReader, output_dir: str,
                 incremental: bool = True, mapsize: int = LMDB_KLINE_MAPSIZE,
                 include_option: bool = False, option_only: bool = False):
        """
        @ks_reader KS数据读取器
        @output_dir LMDB输出根目录
        @incremental 是否增量模式
        @mapsize LMDB mapsize (默认8MB)
        @include_option 是否在共享品种目录内保留期权合约
        @option_only 纯期权独立流水线 (CFFEX IO/HO/MO):仅保留期权,丢弃 *_main/*_index
        """
        if not HAS_LMDB:
            raise ImportError("需要安装lmdb库: pip install lmdb")

        self.ks_reader = ks_reader
        self.output_dir = output_dir
        self.incremental = incremental
        self.mapsize = mapsize
        self.include_option = include_option
        self.option_only = option_only

        # LMDB数据库缓存
        self._db_handles: Dict[str, lmdb.Environment] = {}
        self._existing_keys_cache: Dict[str, set] = {}  # P2-12: 增量去重缓存

    @staticmethod
    def _is_synthetic_main(code: str) -> bool:
        return code.endswith('_main') or code.endswith('_index') or code.endswith('.HOT')

    def _accept_code(self, exchange: str, product: str, code: str) -> bool:
        if self._is_synthetic_main(code):
            return not self.option_only
        is_opt = self.ks_reader.is_option_code(exchange, product, code)
        if self.option_only:
            return is_opt
        if not self.include_option and is_opt:
            return False
        return True

    def _get_db(self, subdir: str, exchg: str) -> lmdb.Environment:
        """获取或创建LMDB数据库

        路径: {output_dir}/{subdir}/{exchg}/
        """
        key = f"{subdir}/{exchg}"
        if key in self._db_handles:
            return self._db_handles[key]

        db_path = os.path.join(self.output_dir, subdir, exchg)
        os.makedirs(db_path, exist_ok=True)

        # 增量模式: 打开已有数据库; 全量模式: 重建
        if not self.incremental and os.path.exists(os.path.join(db_path, 'data.mdb')):
            os.remove(os.path.join(db_path, 'data.mdb'))
            lock_path = os.path.join(db_path, 'lock.mdb')
            if os.path.exists(lock_path):
                os.remove(lock_path)

        # P1-6 fix: 动态调整 mapsize — 如果已有 data.mdb 超过当前 mapsize，
        # 取 max(self.mapsize, existing_size * 2) 避免 MapFullError
        effective_mapsize = self.mapsize
        data_mdb_path = os.path.join(db_path, 'data.mdb')
        if os.path.exists(data_mdb_path):
            existing_size = os.path.getsize(data_mdb_path)
            if existing_size >= self.mapsize * 0.8:
                effective_mapsize = max(self.mapsize, existing_size * 2)

        env = lmdb.open(db_path, map_size=effective_mapsize, max_dbs=1)
        self._db_handles[key] = env
        return env

    def _get_existing_keys(self, subdir: str, exchg: str) -> set:
        """读取已有LMDB数据库中的key集合，用于增量去重

        必须在_get_db之后调用，复用同一个env handle
        P2-12 fix: 加实例级缓存，同一(subdir,exchg)只扫描一次
        """
        cache_key = subdir + '/' + exchg
        if cache_key in self._existing_keys_cache:
            return self._existing_keys_cache[cache_key]

        env = self._db_handles.get(cache_key)
        if env is None:
            return set()

        keys = set()
        with env.begin(write=False) as txn:
            cursor = txn.cursor()
            for key, value in cursor:
                # 使用完整 LMDBBarKey 去重，避免不同合约同一分钟互相跳过
                keys.add(bytes(key))

        self._existing_keys_cache[cache_key] = keys
        return keys

    def write_bars_to_lmdb(self, exchg: str, code: str, bars: List[KSBarRecord],
                            subdir: str, is_day: bool = False,
                            existing_bartimes: set = None) -> int:
        """将K线数据写入LMDB数据库

        @exchg 交易所
        @code 合约代码
        @bars K线数据列表
        @subdir LMDB子目录 (min5/min1/day)
        @is_day 是否日线
        @existing_bartimes 已存在的完整LMDB key集合(增量去重)

        返回写入条数
        """
        env = self._get_db(subdir, exchg)

        written = 0
        with env.begin(write=True) as txn:
            for bar in bars:
                # 计算bartime：统一复用 standardizer，避免 writer 间语义漂移
                bartime = bar_time_key(bar, is_day=is_day)

                # 编码key和value
                key = encode_lmdb_bar_key(exchg, code, bartime)

                # 增量去重：必须按完整 exchange+code+bartime 判断
                if existing_bartimes and key in existing_bartimes:
                    continue

                value = encode_wts_bar_struct(bar, is_day)

                txn.put(key, value)
                written += 1

        if written > 0:
            logger.info(f"写入LMDB {subdir}/{exchg}/{code}: {written}条")

        return written

    def convert_code_day(self, exchange: str, product: str, code: str) -> int:
        """转换单个合约的日线数据到LMDB"""
        # 先确保env已打开
        env = self._get_db('day', exchange)

        # 增量去重
        existing = None
        if self.incremental:
            existing = self._get_existing_keys('day', exchange)

        bars = self.ks_reader.read_day_csv(exchange, product, code)
        if not bars:
            return 0

        return self.write_bars_to_lmdb(exchange, code, bars, 'day',
                                        is_day=True, existing_bartimes=existing)

    def convert_code_minute(self, exchange: str, product: str, code: str,
                            period: str, dates: List[int] = None) -> int:
        """转换单个合约的分钟线数据到LMDB"""
        period_info = PERIOD_MAP.get(period)
        if period_info is None or period_info[1] is None:
            return 0

        subdir = period_info[1]  # min5/min1

        # 先确保env已打开
        env = self._get_db(subdir, exchange)

        # 增量去重
        existing = None
        if self.incremental:
            existing = self._get_existing_keys(subdir, exchange)

        # 获取日期列表
        all_dates = self.ks_reader.list_date_dirs(exchange, product, period)
        if dates:
            all_dates = [d for d in all_dates if d in set(dates)]

        if not all_dates:
            return 0

        # 逐日读取
        all_bars = []
        for date in all_dates:
            date_data = self.ks_reader.read_date_minute(exchange, product, period, date)
            if code in date_data:
                all_bars.extend(date_data[code])

        if not all_bars:
            return 0

        return self.write_bars_to_lmdb(exchange, code, all_bars, subdir,
                                        is_day=False, existing_bartimes=existing)

    def convert_product(self, exchange: str, product: str,
                        periods: List[str] = None,
                        dates: List[int] = None) -> Dict[str, int]:
        """转换某品种所有合约的数据到LMDB

        返回: {周期: 写入条数}
        """
        if periods is None:
            periods = ['kline_1d', 'kline_5m']

        stats = defaultdict(int)

        for period in periods:
            if period not in WTPY_SUPPORTED_PERIODS:
                continue

            is_day = (period == 'kline_1d')
            period_info = PERIOD_MAP[period]
            subdir = period_info[1]

            if is_day:
                codes = self.ks_reader.list_day_codes(exchange, product)
                for code in codes:
                    if not self._accept_code(exchange, product, code):
                        continue
                    written = self.convert_code_day(exchange, product, code)
                    if written > 0:
                        stats[period] += written
            else:
                all_dates = self.ks_reader.list_date_dirs(exchange, product, period)
                if dates:
                    all_dates = [d for d in all_dates if d in set(dates)]
                if not all_dates:
                    continue

                # 增量去重
                existing = None
                if self.incremental:
                    existing = self._get_existing_keys(subdir, exchange)

                # 逐日读取所有合约
                code_bars = defaultdict(list)
                for date in all_dates:
                    date_data = self.ks_reader.read_date_minute(
                        exchange, product, period, date)
                    for code, bars in date_data.items():
                        if not self._accept_code(exchange, product, code):
                            continue
                        code_bars[code].extend(bars)

                # 逐合约写入
                for code, bars in code_bars.items():
                    written = self.write_bars_to_lmdb(
                        exchange, code, bars, subdir,
                        is_day=False, existing_bartimes=existing)
                    if written > 0:
                        stats[period] += written

        return dict(stats)

    def close_all(self):
        """关闭所有LMDB数据库"""
        for key, env in self._db_handles.items():
            env.close()
        self._db_handles.clear()

    def verify_lmdb(self, subdir: str, exchg: str) -> Dict:
        """验证LMDB数据库内容"""
        db_path = os.path.join(self.output_dir, subdir, exchg)
        if not os.path.exists(os.path.join(db_path, 'data.mdb')):
            return {'exists': False}

        env = lmdb.open(db_path, map_size=self.mapsize, max_dbs=1, readonly=True)
        stats = {'exists': True, 'total_keys': 0, 'codes': set()}

        with env.begin(write=False) as txn:
            cursor = txn.cursor()
            for key, value in cursor:
                stats['total_keys'] += 1
                # 从key中提取code (offset 16-48)
                code = key[16:48].rstrip(b'\x00').decode('utf-8')
                stats['codes'].add(code)

        stats['codes'] = sorted(stats['codes'])
        env.close()
        return stats