#!/usr/bin/env python3
"""
KS K线数据 → WonderTrader DSB二进制格式转换器

DSB格式 (BlockHeaderV2 + zstd压缩):
  BLK_FLAG(8) + type(u16) + version(u16) + compressed_size(u64) + zstd(WTSBarStruct[])

目录结构:
  {base}/his/day/{EXCHG}/{CODE}.dsb
  {base}/his/min5/{EXCHG}/{CODE}.dsb
  {base}/his/min1/{EXCHG}/{CODE}.dsb

WTSBarStruct (pack 8, 88 bytes):
  date(u32) + reserve(u32) + time(u64) + open(f64) + high(f64) + low(f64) +
  close(f64) + settle(f64) + money(f64) + vol(f64) + hold(f64) + add(f64)
"""

import os
import struct
import logging
from typing import Dict, List, Optional
from collections import defaultdict

# zstd模块统一检测（优先zstandard，fallback python-zstd）
_zstd_module = None  # 'zstandard' 或 'zstd' 或 'none'
_zstd_compressor = None  # ZstdCompressor实例（仅zstandard时缓存）


def _init_zstd():
    """延迟初始化zstd模块引用，优先zstandard（更活跃维护、标准帧格式）"""
    global _zstd_module, _zstd_compressor
    if _zstd_module is not None:
        return
    try:
        import zstandard as _zs
        _zstd_module = 'zstandard'
        _zstd_compressor = _zs.ZstdCompressor(level=3)
    except ImportError:
        try:
            import zstd as _zs
            _zstd_module = 'zstd'
        except ImportError:
            _zstd_module = 'none'

from config import (
    PERIOD_MAP, WTPY_SUPPORTED_PERIODS,
    WTSBAR_STRUCT_SIZE, WTS_TICK_STRUCT_SIZE,
    BLK_FLAG, BLOCK_VERSION_CMP_V2,
    BT_HIS_MINUTE1, BT_HIS_MINUTE5, BT_HIS_DAY, BT_HIS_TICKS,
)
from ks_reader import KSBarRecord, KSTickRecord
from standardizer import bar_time_key

logger = logging.getLogger(__name__)

# BlockType → DSB type mapping
PERIOD_TO_BT = {
    'kline_1d': BT_HIS_DAY,
    'kline_5m': BT_HIS_MINUTE5,
    'kline_1m': BT_HIS_MINUTE1,
}

# DSB子目录名
PERIOD_TO_DIR = {
    'kline_1d': 'day',
    'kline_5m': 'min5',
    'kline_1m': 'min1',
}


def encode_bar_struct(bar: KSBarRecord, is_day: bool = False) -> bytes:
    """将KSBarRecord编码为WTSBarStruct (88 bytes, pack 8)

    注意: pack(8)对齐, 但我们用手工pack而非struct.pack('>...')来确保布局
    offset 0:  date(u32) + reserve(u32) = 8 bytes
    offset 8:  time(u64) = 8 bytes
    offset 16: open(f64) = 8 bytes
    offset 24: high(f64) = 8 bytes
    offset 32: low(f64) = 8 bytes
    offset 40: close(f64) = 8 bytes
    offset 48: settle(f64) = 8 bytes
    offset 56: money(f64) = 8 bytes
    offset 64: vol(f64) = 8 bytes
    offset 72: hold(f64) = 8 bytes
    offset 80: add(f64) = 8 bytes
    total = 88 bytes
    """
    # time字段: 日线=date, 分钟线=(date-19900000)*10000+time
    # 统一复用 standardizer，避免 LMDB/DSB 时间语义漂移。
    time_val = bar_time_key(bar, is_day=is_day)

    buf = struct.pack('<II', bar.date, 0)            # date + reserve
    buf += struct.pack('<Q', time_val)                 # time
    buf += struct.pack('<d', bar.open)                 # open
    buf += struct.pack('<d', bar.high)                 # high
    buf += struct.pack('<d', bar.low)                  # low
    buf += struct.pack('<d', bar.close)                # close
    buf += struct.pack('<d', 0.0)                      # settle (KS无结算价)
    buf += struct.pack('<d', bar.amount)               # money/turnover
    buf += struct.pack('<d', bar.volume)               # vol
    buf += struct.pack('<d', bar.openinterest)         # hold
    buf += struct.pack('<d', 0.0)                      # add (KS无增减量)

    assert len(buf) == WTSBAR_STRUCT_SIZE, f"WTSBarStruct大小错误: {len(buf)} != {WTSBAR_STRUCT_SIZE}"
    return buf


# WT tick DSB路径模板
TICK_DSB_SUBDIR = 'ticks'


def _ms_to_wt_datetime(timestamp_ms: int) -> tuple:
    """将Unix毫秒时间戳转换为WT (action_date, action_time) 格式

    action_date: YYYYMMDD (uint32)
    action_time: HHMMSSMMM (uint32), 如 09:00:00.500 → 90000500

    时区: 中国标准时间 UTC+8
    """
    from datetime import datetime, timezone, timedelta
    CST = timezone(timedelta(hours=8))
    dt = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=CST)
    action_date = dt.year * 10000 + dt.month * 100 + dt.day
    action_time = (dt.hour * 10000000 + dt.minute * 100000
                   + dt.second * 1000 + dt.microsecond // 1000)
    return action_date, action_time


def encode_tick_struct(tick: KSTickRecord, exchg: str, code: str,
                       prev_volume: float = 0.0,
                       prev_amount: float = 0.0) -> tuple:
    """将KSTickRecord编码为WTSTickStruct (512 bytes, pack 8)

    @tick          KS tick记录
    @exchg         交易所代码(如 SHFE)
    @code          合约代码(如 rb2610)
    @prev_volume   前一条tick的累计成交量(用于计算增量)
    @prev_amount   前一条tick的累计成交额(用于计算增量)

    返回: (bytes, incremental_volume, incremental_amount)
      bytes: 512字节WTSTickStruct二进制
      incremental_volume: 本条tick增量成交量
      incremental_amount: 本条tick增量成交额
    """
    # 防御性检查: timestamp_ms <= 0 时无法正确转换日期
    if tick.timestamp_ms <= 0:
        logger.warning(
            "encode_tick_struct: timestamp_ms=%d <= 0 (合约=%s, 交易日=%d)，"
            "action_date/action_time 设为 0",
            tick.timestamp_ms, code, tick.trading_day
        )
        action_date = 0
        action_time = 0
    else:
        action_date, action_time = _ms_to_wt_datetime(tick.timestamp_ms)

    # 计算增量
    inc_vol = tick.volume - prev_volume
    inc_amt = tick.amount - prev_amount
    if inc_vol < 0:
        logger.warning(
            "encode_tick_struct: 增量成交量<0 (合约=%s, 交易日=%d, "
            "prev_vol=%.2f, cur_vol=%.2f)，已归零",
            code, tick.trading_day, prev_volume, tick.volume
        )
        inc_vol = 0.0
    if inc_amt < 0:
        logger.warning(
            "encode_tick_struct: 增量成交额<0 (合约=%s, 交易日=%d, "
            "prev_amt=%.2f, cur_amt=%.2f)，已归零",
            code, tick.trading_day, prev_amount, tick.amount
        )
        inc_amt = 0.0

    buf = b''

    # offset 0: exchg (char[16], null-padded)
    buf += struct.pack('16s', exchg.encode('ascii')[:16])

    # offset 16: code (char[32], null-padded)
    buf += struct.pack('32s', code.encode('ascii')[:32])

    # offset 48-144: 12 doubles (price, open, high, low, settle, upper, lower,
    #               total_vol, vol, total_turnover, turnover, OI, diff_interest)
    doubles = [
        tick.last_price,       # price
        tick.open,             # open
        tick.high,             # high
        tick.low,              # low
        0.0,                   # settle_price (KS无)
        0.0,                   # upper_limit (KS无)
        0.0,                   # lower_limit (KS无)
        tick.volume,           # total_volume (累计)
        inc_vol,               # volume (增量)
        tick.amount,           # total_turnover (累计)
        inc_amt,               # turn_over (增量)
        tick.openinterest,     # open_interest
        0.0,                   # diff_interest (KS无)
    ]
    for val in doubles:
        buf += struct.pack('<d', val)

    # offset 152-167: 4 uint32
    buf += struct.pack('<IIII',
                       tick.trading_day,   # trading_date
                       action_date,        # action_date
                       action_time,        # action_time
                       0)                  # reserve

    # offset 168-191: 3 doubles (pre_close, pre_settle, pre_interest)
    buf += struct.pack('<d', 0.0)  # pre_close
    buf += struct.pack('<d', 0.0)  # pre_settle
    buf += struct.pack('<d', 0.0)  # pre_interest

    # offset 192-271: bid_price[10] (KS最多5档, 填0补齐10档)
    for i in range(10):
        val = tick.bid_prices[i] if i < len(tick.bid_prices) else 0.0
        buf += struct.pack('<d', val)

    # offset 272-351: ask_price[10]
    for i in range(10):
        val = tick.ask_prices[i] if i < len(tick.ask_prices) else 0.0
        buf += struct.pack('<d', val)

    # offset 352-431: bid_qty[10]
    for i in range(10):
        val = tick.bid_volumes[i] if i < len(tick.bid_volumes) else 0.0
        buf += struct.pack('<d', val)

    # offset 432-511: ask_qty[10]
    for i in range(10):
        val = tick.ask_volumes[i] if i < len(tick.ask_volumes) else 0.0
        buf += struct.pack('<d', val)

    assert len(buf) == WTS_TICK_STRUCT_SIZE, \
        f"WTSTickStruct大小错误: {len(buf)} != {WTS_TICK_STRUCT_SIZE}"
    return buf, inc_vol, inc_amt


def compress_data(raw_data: bytes) -> bytes:
    """zstd压缩数据（统一使用_init_zstd缓存的模块引用）"""
    _init_zstd()
    if _zstd_module == 'none':
        # 无zstd库，返回原始数据（write_dsb_file会用version=0x01标记）
        return raw_data
    if _zstd_module == 'zstandard':
        return _zstd_compressor.compress(raw_data)
    # zstd (python-zstd)
    import zstd
    return zstd.compress(raw_data)


def write_dsb_file(filepath: str, block_type: int, bars_data: bytes,
                    use_compression: bool = True) -> int:
    """写入DSB文件

    @filepath 输出文件路径
    @block_type BlockType (BT_HIS_DAY/BT_HIS_MINUTE5/BT_HIS_MINUTE1)
    @bars_data 原始WTSBarStruct数组bytes
    @use_compression 是否压缩

    返回写入字节数
    """
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    if use_compression:
        compressed = compress_data(bars_data)
        # 无zstd时写入未压缩数据，用version=0x01标记
        if _zstd_module == 'none':
            version_flag = 0x01
        else:
            version_flag = BLOCK_VERSION_CMP_V2
        # BlockHeaderV2: BLK_FLAG(8) + type(u16) + version(u16) + size(u64) = 20 bytes
        header = BLK_FLAG  # 8 bytes
        header += struct.pack('<H', block_type)     # type: u16
        header += struct.pack('<H', version_flag)   # version: u16
        header += struct.pack('<Q', len(compressed)) # compressed_size: u64

        with open(filepath, 'wb') as f:
            f.write(header)
            f.write(compressed)

        return len(header) + len(compressed)
    else:
        # BlockHeader (V2 RAW): BLK_FLAG(8) + type(u16) + version(u16) + size(u64)
        BLOCK_VERSION_RAW_V2 = 0x03
        header = BLK_FLAG
        header += struct.pack('<H', block_type)
        header += struct.pack('<H', BLOCK_VERSION_RAW_V2)
        header += struct.pack('<Q', len(bars_data))

        with open(filepath, 'wb') as f:
            f.write(header)
            f.write(bars_data)

        return len(header) + len(bars_data)


def read_dsb_file(filepath: str) -> Optional[Dict]:
    """读取DSB文件(用于验证)

    返回: {type, version, compressed, bar_count, bars_bytes}
    """
    if not os.path.exists(filepath):
        return None

    with open(filepath, 'rb') as f:
        data = f.read()

    if len(data) < 20:
        return None

    flag = data[:8]
    if flag != BLK_FLAG:
        return None

    block_type = struct.unpack('<H', data[8:10])[0]
    version = struct.unpack('<H', data[10:12])[0]
    comp_size = struct.unpack('<Q', data[12:20])[0]

    payload = data[20:]
    is_compressed = version in (0x02, 0x04)

    if is_compressed and len(payload) == comp_size:
        _init_zstd()
        if _zstd_module == 'none':
            # 无zstd库，直接返回原始数据
            bars_bytes = payload
        elif _zstd_module == 'zstandard':
            import zstandard as _zs
            dctx = _zs.ZstdDecompressor()
            bars_bytes = dctx.decompress(payload)
        else:
            # python-zstd
            import zstd
            bars_bytes = zstd.decompress(payload)
    else:
        bars_bytes = payload

    bar_count = len(bars_bytes) // WTSBAR_STRUCT_SIZE

    return {
        'type': block_type,
        'version': version,
        'compressed': is_compressed,
        'bar_count': bar_count,
        'bars_bytes': bars_bytes,
    }


def read_tick_dsb_file(filepath: str) -> Optional[Dict]:
    """读取tick DSB文件(用于验证)

    返回: {type, version, compressed, tick_count, ticks_bytes}
    """
    if not os.path.exists(filepath):
        return None

    with open(filepath, 'rb') as f:
        data = f.read()

    if len(data) < 20:
        return None

    flag = data[:8]
    if flag != BLK_FLAG:
        return None

    block_type = struct.unpack('<H', data[8:10])[0]
    version = struct.unpack('<H', data[10:12])[0]
    comp_size = struct.unpack('<Q', data[12:20])[0]

    payload = data[20:]
    is_compressed = version in (0x02, 0x04)

    if is_compressed and len(payload) == comp_size:
        _init_zstd()
        if _zstd_module == 'none':
            # 无zstd库，直接返回原始数据
            ticks_bytes = payload
        elif _zstd_module == 'zstandard':
            import zstandard as _zs
            dctx = _zs.ZstdDecompressor()
            ticks_bytes = dctx.decompress(payload)
        else:
            # python-zstd
            import zstd
            ticks_bytes = zstd.decompress(payload)
    else:
        ticks_bytes = payload

    tick_count = len(ticks_bytes) // WTS_TICK_STRUCT_SIZE

    return {
        'type': block_type,
        'version': version,
        'compressed': is_compressed,
        'tick_count': tick_count,
        'ticks_bytes': ticks_bytes,
    }


def encode_ticks_to_bytes(ticks: List[KSTickRecord], exchg: str, code: str) -> bytes:
    """将KSTickRecord列表编码为WTSTickStruct字节数组

    自动处理增量成交量/额计算: 前一条tick的累计值作为prev。
    ticks必须已按timestamp_ms排序。

    返回: 连续WTSTickStruct bytes (len * 512)
    """
    result = bytearray()
    prev_vol = 0.0
    prev_amt = 0.0
    for tick in ticks:
        tick_bytes, inc_vol, inc_amt = encode_tick_struct(
            tick, exchg, code, prev_volume=prev_vol, prev_amount=prev_amt)
        result.extend(tick_bytes)
        prev_vol = tick.volume
        prev_amt = tick.amount
    return bytes(result)


def write_tick_dsb(filepath: str, ticks_bytes: bytes,
                    use_compression: bool = True) -> int:
    """写入tick DSB文件

    @filepath    输出路径: {base}/his/ticks/{exchg}/{date}/{code}.dsb
    @ticks_bytes WTSTickStruct原始字节数组
    @use_compression 是否压缩

    返回写入字节数
    """
    return write_dsb_file(filepath, BT_HIS_TICKS, ticks_bytes, use_compression)


class DsbConverter:
    """KS → wtpy DSB 转换器"""

    def __init__(self, ks_reader, output_dir: str, use_compression: bool = True,
                 include_option: bool = False, option_only: bool = False):
        """
        @ks_reader 数据读取器 (KSDataReader 或 S3KsReader)
        @output_dir DSB输出根目录 (his/)
        @use_compression 是否使用zstd压缩
        @include_option 是否在共享品种目录内保留期权合约
        @option_only 纯期权独立流水线 (CFFEX IO/HO/MO):仅保留期权,丢弃 *_main/*_index
        """
        self.ks_reader = ks_reader
        self.output_dir = output_dir
        self.use_compression = use_compression
        self.include_option = include_option
        self.option_only = option_only

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

    def _dsb_path(self, subdir: str, exchange: str, code: str) -> str:
        """生成DSB文件路径。

        匹配 WT C++ WtBtDtReader::read_raw_bars 的拼路径规则：
          his/{period}/{exchg}/{code}.dsb
        注意：文件名只用 raw code，**不带 EXCHG 前缀**——目录已按 exchg 区分。
        若加 EXCHG 前缀（如 'SHFE.rb0909.dsb'），WtDtServo.get_bars 永远返回 None。
        """
        return os.path.join(self.output_dir, subdir, exchange, f"{code}.dsb")

    def convert_code(self, exchange: str, product: str, code: str,
                     period: str, dates: List[int] = None) -> Optional[int]:
        """转换单个合约单个周期到DSB

        返回写入的bar数
        """
        if period not in PERIOD_TO_BT:
            return None

        is_day = (period == 'kline_1d')
        bt = PERIOD_TO_BT[period]
        subdir = PERIOD_TO_DIR[period]

        # 读取数据
        if is_day:
            bars = self.ks_reader.read_day_csv(exchange, product, code)
        else:
            all_dates = self.ks_reader.list_date_dirs(exchange, product, period)
            if dates:
                all_dates = [d for d in all_dates if d in set(dates)]
            if not all_dates:
                return 0
            bars = []
            for date in all_dates:
                date_data = self.ks_reader.read_date_minute(
                    exchange, product, period, date)
                if code in date_data:
                    bars.extend(date_data[code])

        if not bars:
            return 0

        # 排序 (按date, time)
        bars.sort(key=lambda b: (b.date, b.time))

        # 编码为WTSBarStruct
        bars_bytes = b''.join(encode_bar_struct(bar, is_day) for bar in bars)

        # 写入DSB
        filepath = self._dsb_path(subdir, exchange, code)
        written = write_dsb_file(filepath, bt, bars_bytes, self.use_compression)

        logger.info(f"写入DSB: {filepath} ({len(bars)}条, {written}字节)")
        return len(bars)

    def convert_product(self, exchange: str, product: str,
                        periods: List[str] = None,
                        dates: List[int] = None) -> Dict[str, int]:
        """转换某品种所有合约到DSB

        返回: {周期: 写入bar数}

        P0-2 fix: 分钟线路径改为"逐合约流式写入"——先扫描所有日 tar
        识别出现过哪些合约，再按合约名独立读取并立即写盘释放，
        峰值内存 = 当前正在处理的单合约 bars（5年 m1 SHFE 单合约≈60万条≈120MB），
        替代原先 defaultdict(list) 全量累积所有合约所有日（爆 GB 级）。
        依赖 reader 的内存缓存使得跨合约重复访问同一日 tar 不会重新下载/解压。
        """
        if periods is None:
            periods = ['kline_1d', 'kline_5m']

        stats = defaultdict(int)

        for period in periods:
            if period not in WTPY_SUPPORTED_PERIODS:
                continue

            is_day = (period == 'kline_1d')

            # 获取合约列表
            if is_day:
                codes = self.ks_reader.list_day_codes(exchange, product)
                for code in codes:
                    if not self._accept_code(exchange, product, code):
                        continue
                    count = self.convert_code(exchange, product, code, period, dates=dates)
                    if count and count > 0:
                        stats[period] += count
            else:
                # 分钟线: 逐合约流式 — 先识别所有合约，再逐个独立写入
                all_dates = self.ks_reader.list_date_dirs(exchange, product, period)
                if dates:
                    all_dates = [d for d in all_dates if d in set(dates)]
                if not all_dates:
                    continue

                # 第一阶段：扫描所有日 tar 得到合约→日期列表的映射
                # 依赖 ks_reader 内存缓存避免后续重复解压
                code_to_dates: Dict[str, List[int]] = defaultdict(list)
                for date in all_dates:
                    date_data = self.ks_reader.read_date_minute(
                        exchange, product, period, date)
                    for code in date_data.keys():
                        if not self._accept_code(exchange, product, code):
                            continue
                        code_to_dates[code].append(date)

                bt = PERIOD_TO_BT[period]
                subdir = PERIOD_TO_DIR[period]

                # 第二阶段：逐合约流式 — 读取该合约所有日 bars → 排序 → 写盘 → 立即释放
                for code in sorted(code_to_dates.keys()):
                    code_dates = code_to_dates[code]
                    bars = []
                    for date in code_dates:
                        date_data = self.ks_reader.read_date_minute(
                            exchange, product, period, date)
                        if code in date_data:
                            bars.extend(date_data[code])
                    if not bars:
                        continue
                    bars.sort(key=lambda b: (b.date, b.time))
                    bars_bytes = b''.join(encode_bar_struct(bar, is_day) for bar in bars)
                    filepath = self._dsb_path(subdir, exchange, code)
                    written = write_dsb_file(filepath, bt, bars_bytes, self.use_compression)
                    logger.info(f"写入DSB: {filepath} ({len(bars)}条, {written}字节)")
                    stats[period] += len(bars)
                    # 显式释放，让 GC 尽早回收单合约 bars
                    del bars, bars_bytes

        return dict(stats)

    def build_hot_dsb(self, exchange: str, product: str, events,
                      periods: Optional[List[str]] = None) -> Dict[str, int]:
        """按主力切换 events 拼接出 {product}.HOT.dsb 一等公民 DSB。

        @events: List[Dict] 同 csv_writer.build_hot_csv 的 events 形态
        返回: {period: 写入 bar 数}

        语义: 在 [event[i].date, event[i+1].date) 区间用 event[i].to 合约 bars,
              最后一个事件区间开放到 99999999。直接拼接，不复权（WT HotMgr 上层处理因子）。

        产物路径: {output_dir}/{day|min1|min5}/{EXCHG}/{product}.HOT.dsb
        WtDtServo 当 hotmgr 解析失败时会按文件名 fallback 命中此文件。

        P1-4 fix: 分钟线优先从已写入的合约 DSB 读取（avoid 重复 KS 解压）；
                  日线仍走 reader 内存缓存（单 tar 含全部合约，成本低）。
        """
        if periods is None:
            periods = ['kline_1d', 'kline_5m']

        if not events:
            return {}

        events = sorted(events, key=lambda ev: int(ev['date']))
        stats: Dict[str, int] = {}

        for period in periods:
            if period not in PERIOD_TO_BT:
                continue

            is_day = (period == 'kline_1d')
            bt = PERIOD_TO_BT[period]
            subdir = PERIOD_TO_DIR[period]

            hot_bars: List = []
            cache: Dict[str, List] = {}

            def _bars_for(code: str) -> List:
                if code in cache:
                    return cache[code]
                if is_day:
                    cache[code] = self.ks_reader.read_day_csv(exchange, product, code) or []
                else:
                    # P1-4 fix: 优先读已写入的合约 DSB，绕开 KS tar 重复解压
                    dsb_path = self._dsb_path(subdir, exchange, code)
                    if os.path.exists(dsb_path):
                        info = read_dsb_file(dsb_path)
                        if info and info['bar_count'] > 0:
                            bars = self._decode_bars_from_dsb(info['bars_bytes'], is_day=False)
                            cache[code] = bars
                            return cache[code]
                    # Fallback: DSB 不存在则从 KS 读（保险，理论上 convert_product 已先跑）
                    all_dates = self.ks_reader.list_date_dirs(exchange, product, period)
                    bars: List = []
                    for d in all_dates:
                        date_data = self.ks_reader.read_date_minute(exchange, product, period, d)
                        if code in date_data:
                            bars.extend(date_data[code])
                    cache[code] = bars
                return cache[code]

            for i, ev in enumerate(events):
                code = ev['to']
                if not code:
                    continue
                start_date = int(ev['date'])
                end_date = int(events[i + 1]['date']) if i + 1 < len(events) else 99999999
                contract_bars = _bars_for(code)
                hot_bars.extend(b for b in contract_bars if start_date <= b.date < end_date)
                # P1-4: HOT 拼完一段后立即从 cache 释放该合约 bars
                # （events 通常按日期单调，主力切换后不会再回到旧合约）
                if i + 1 < len(events) and events[i + 1]['to'] != code:
                    cache.pop(code, None)

            if not hot_bars:
                logger.warning(f"HOT DSB 拼接无数据: {exchange}.{product} period={period}")
                continue

            if is_day:
                hot_bars.sort(key=lambda b: b.date)
            else:
                hot_bars.sort(key=lambda b: (b.date, b.time))

            bars_bytes = b''.join(encode_bar_struct(bar, is_day) for bar in hot_bars)
            hot_code = f"{product}.HOT"
            filepath = self._dsb_path(subdir, exchange, hot_code)
            written = write_dsb_file(filepath, bt, bars_bytes, self.use_compression)
            logger.info(f"写入HOT DSB: {filepath} ({len(hot_bars)}条, {written}字节, "
                        f"覆盖 {len(events)} 段切换)")
            stats[period] = len(hot_bars)

        return stats

    def _decode_bars_from_dsb(self, bars_bytes: bytes, is_day: bool) -> List[KSBarRecord]:
        """从 DSB bars_bytes 反解为 KSBarRecord 列表（P1-4 helper）。

        WTSBarStruct 88B 布局（参见 encode_bar_struct）。注意 time 字段：
        - 日线：date 字段已是 YYYYMMDD，time_val = date
        - 分钟线：time_val = (date - 19900000) * 10000 + minute_in_day
                  bar.time = time_val % 10000 即原 KS time (HHMM)
        """
        bars: List[KSBarRecord] = []
        n = len(bars_bytes) // WTSBAR_STRUCT_SIZE
        for i in range(n):
            off = i * WTSBAR_STRUCT_SIZE
            date_v = struct.unpack_from('<I', bars_bytes, off)[0]
            time_v = struct.unpack_from('<Q', bars_bytes, off + 8)[0]
            open_v = struct.unpack_from('<d', bars_bytes, off + 16)[0]
            high_v = struct.unpack_from('<d', bars_bytes, off + 24)[0]
            low_v = struct.unpack_from('<d', bars_bytes, off + 32)[0]
            close_v = struct.unpack_from('<d', bars_bytes, off + 40)[0]
            money_v = struct.unpack_from('<d', bars_bytes, off + 56)[0]
            vol_v = struct.unpack_from('<d', bars_bytes, off + 64)[0]
            hold_v = struct.unpack_from('<d', bars_bytes, off + 72)[0]
            if is_day:
                t_field = 0  # 日线时间无意义
            else:
                t_field = int(time_v) % 10000
            bars.append(KSBarRecord(
                date=int(date_v), time=t_field,
                open=open_v, high=high_v, low=low_v, close=close_v,
                volume=vol_v, amount=money_v, openinterest=hold_v,
            ))
        return bars

    def verify_dsb(self, subdir: str, exchange: str,
                    max_verify: int = 0) -> Dict:
        """验证DSB文件

        @max_verify 最多验证文件数，0=全部验证（P1-5 fix: 原先硬编码[:10]）
        """
        dsb_dir = os.path.join(self.output_dir, subdir, exchange)
        if not os.path.exists(dsb_dir):
            return {'exists': False}

        files = sorted(f for f in os.listdir(dsb_dir) if f.endswith('.dsb'))
        total_files = len(files)
        verify_files = files if max_verify == 0 else files[:max_verify]

        result = {
            'exists': True,
            'file_count': total_files,
            'verified_count': len(verify_files),
            'total_bars': 0,
            'details': [],
        }

        for fname in verify_files:
            fpath = os.path.join(dsb_dir, fname)
            info = read_dsb_file(fpath)
            if info:
                result['total_bars'] += info['bar_count']
                result['details'].append({
                    'file': fname,
                    'bars': info['bar_count'],
                    'compressed': info['compressed'],
                    'type': info['type'],
                })

        return result
