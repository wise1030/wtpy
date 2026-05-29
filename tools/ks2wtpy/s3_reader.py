#!/usr/bin/env python3
"""
S3 KS数据读取器 (MinIO)

直接从MinIO S3对象存储读取KS格式K线数据，替代SSH方式。
优势:
  - 所有100品种都有完整7类数据(1d/1m/5m/15m/30m/1h/tick)
  - 无需SSH，直连S3 API下载
  - 本地缓存避免重复下载
  - 支持增量读取

MinIO目录结构:
  ks-data/{EXCHG}/{PRODUCT}/L1/{data_type}/
    kline_1d/kline_1d.tar.gz          — 日线，含所有合约CSV
    kline_5m/{YYYYMMDD}.tar.gz        — 分钟线，按日分压缩包
    kline_1m/{YYYYMMDD}.tar.gz
    kline_15m/{YYYYMMDD}.tar.gz
    kline_30m/{YYYYMMDD}.tar.gz
    kline_1h/{YYYYMMDD}.tar.gz
    tick/{YYYYMMDD}.tar.gz

tar.gz内部CSV格式:
  date,time,open,high,low,close,volume,amount,openinterest
  日线文件名: {CODE}.csv (如 cu2506.csv)
  分钟线文件名: {CODE}_{date}.csv (如 cu2108_20210104.csv)
"""

import os
import io
import logging
import tarfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Set
from collections import defaultdict, OrderedDict
from dataclasses import dataclass

from config import EXCHANGES, PERIOD_MAP, WTPY_SUPPORTED_PERIODS
from ks_reader import KSBarRecord, KSTickRecord, parse_ks_csv_content, parse_ks_tick_csv_content, extract_code_from_filename

logger = logging.getLogger(__name__)

# 内存缓存的最大条目数（每条≈一个tar.gz解析后的{code:bars}字典）
# rb 五年≈1300日 m5 + 200日历史 = 1500 项；预留余量
_MEM_CACHE_MAX_ITEMS = int(os.environ.get('KS2WTPY_MEM_CACHE_ITEMS', '4000'))
# 内存缓存的最大字节数（默认 4GB，0=不限制）
# P1-2: 防止 SHFE.ag 等大品种 m1 预热导致 OOM
_MEM_CACHE_MAX_BYTES = int(os.environ.get('KS2WTPY_MEM_CACHE_BYTES', str(4 * 1024 * 1024 * 1024)))
# pinned 项最大数量（防 pinned 集合无限增长）
# P1-3: 单品种 m1 5年≈1300条，留 2x 余量
_MEM_CACHE_MAX_PINNED = int(os.environ.get('KS2WTPY_MEM_CACHE_MAX_PINNED', '3000'))
# 并行预热下载的线程数
_PREFETCH_WORKERS = int(os.environ.get('KS2WTPY_PREFETCH_WORKERS', '16'))


def _estimate_bars_dict_bytes(d: Dict[str, List]) -> int:
    """估算 {code: [KSBarRecord]} 字典的字节开销（粗略，用于容量控制）。

    KSBarRecord 是 dataclass，9 个字段（多数 int/float）≈ 64+88 bytes 含开销；
    保守按 200 字节/条估算，覆盖 Python 对象头开销。
    """
    if not d:
        return 0
    total = 0
    for code, bars in d.items():
        total += 64 + len(code)  # dict 桶 + key 字符串
        total += 200 * len(bars)
    return total


class S3KsReader:
    """基于MinIO S3的KS数据读取器，替代SSH方式的KSDataReader

    接口与KSDataReader兼容，可直接替换CsvConverter/LmdbWriter中的reader。
    """

    def __init__(
        self,
        endpoint: str = '',
        access_key: str = '',
        secret_key: str = '',
        bucket: str = 'ks-data',
        secure: bool = False,
        cache_dir: Optional[str] = None,
    ):
        """
        @endpoint   MinIO S3 API地址
        @access_key S3 access key
        @secret_key S3 secret key
        @bucket     数据桶名
        @secure     是否HTTPS
        @cache_dir  本地缓存目录，避免重复下载tar.gz
        """
        if not endpoint or not access_key or not secret_key:
            raise ValueError('S3 endpoint/access_key/secret_key 必须显式提供')
        try:
            from minio import Minio
        except ImportError as exc:
            raise ImportError('使用S3数据源需要安装minio库: pip install minio') from exc

        self.client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )
        self.bucket = bucket
        self.cache_dir = cache_dir
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

        # 元数据缓存: {exchange: [products]}
        self._products_cache: Dict[str, List[str]] = {}
        # 元数据缓存: {(exchange, product, period): sorted_dates}
        self._dates_cache: Dict[tuple, List[int]] = {}

        # 内存缓存: {(exchange, product, period, date|None, is_day): {code: [KSBarRecord]}}
        # 解决问题: 同一个 tar.gz 在 csv/lmdb/dsb/HOT 四阶段被反复解压解析
        # 命中内存缓存可省去 gzip+tar+CSV 三层解析成本（实测占总耗时 60% 以上）
        # P2-10 fix: 改用 OrderedDict 实现 LRU
        # P1-2 fix: 增加 _mem_cache_bytes 跟踪近似字节占用，支持字节级淘汰
        self._mem_cache: OrderedDict[tuple, Dict[str, List[KSBarRecord]]] = OrderedDict()
        self._mem_cache_lock = threading.Lock()
        # 跟踪每个 key 的字节估算，用于字节级淘汰
        self._mem_cache_sizes: Dict[tuple, int] = {}
        self._mem_cache_bytes_total: int = 0
        # 缓存模式: 'auto'(默认,LRU满则淘汰), 'full'(永不淘汰,需事先估算容量)
        self._mem_cache_mode = os.environ.get('KS2WTPY_MEM_CACHE_MODE', 'auto')
        # 已知"安全可常驻"的key集合，由prefetch阶段填充，淘汰时不动
        # P1-3 fix: pinned 集合容量上限，防止跨品种累积无限增长
        self._mem_cache_pinned: Set[tuple] = set()

    # ==========================================
    # 缓存管理
    # ==========================================

    def _cache_path(self, exchange: str, product: str, period: str,
                    date: Optional[int] = None) -> Optional[str]:
        """计算缓存文件路径"""
        if not self.cache_dir:
            return None
        if period == 'kline_1d':
            return os.path.join(self.cache_dir, exchange, product,
                                period, 'kline_1d.tar.gz')
        else:
            return os.path.join(self.cache_dir, exchange, product,
                                period, f'{date}.tar.gz')

    def _read_cached_tar(self, cache_path: Optional[str]) -> Optional[bytes]:
        """读取缓存文件"""
        if cache_path and os.path.isfile(cache_path):
            logger.debug(f'缓存命中: {cache_path}')
            with open(cache_path, 'rb') as f:
                return f.read()
        return None

    def _write_cached_tar(self, cache_path: Optional[str], data: bytes):
        """写入缓存文件"""
        if cache_path:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, 'wb') as f:
                f.write(data)
            logger.debug(f'写入缓存: {cache_path} ({len(data)} bytes)')

    # ==========================================
    # 内存缓存：解析后的 {code: [KSBarRecord]}
    # ==========================================

    def _mem_key(self, exchange: str, product: str, period: str,
                 date: Optional[int]) -> tuple:
        is_day = (period == 'kline_1d')
        return (exchange, product, period, None if is_day else date, is_day)

    def _mem_get(self, key: tuple) -> Optional[Dict[str, List[KSBarRecord]]]:
        with self._mem_cache_lock:
            if key in self._mem_cache:
                # P2-10 fix: 命中时提升为最近使用，实现真 LRU
                self._mem_cache.move_to_end(key)
                return self._mem_cache[key]
            return None

    def _mem_put(self, key: tuple, value: Dict[str, List[KSBarRecord]],
                 pinned: bool = False):
        with self._mem_cache_lock:
            # 估算新条目字节数
            est_bytes = _estimate_bars_dict_bytes(value)

            # P1-3 fix: pinned 集合容量上限——超限则降级为非 pinned
            if pinned and len(self._mem_cache_pinned) >= _MEM_CACHE_MAX_PINNED:
                logger.debug(
                    "pinned 集合已达上限 %d，降级新项为非 pinned: %s",
                    _MEM_CACHE_MAX_PINNED, key
                )
                pinned = False

            # P1-2 fix: 同时按条目数和字节数双维度限制；auto 模式下淘汰非 pinned 的 LRU 项
            def _need_evict() -> bool:
                if self._mem_cache_mode != 'auto':
                    return False
                if len(self._mem_cache) >= _MEM_CACHE_MAX_ITEMS:
                    return True
                if (_MEM_CACHE_MAX_BYTES > 0
                        and self._mem_cache_bytes_total + est_bytes > _MEM_CACHE_MAX_BYTES):
                    return True
                return False

            # 循环淘汰直到容量足够（或没东西可淘汰）
            evicted = 0
            while _need_evict():
                victim = None
                for k in self._mem_cache:
                    if k not in self._mem_cache_pinned:
                        victim = k
                        break
                if victim is None:
                    # 全是 pinned，无法淘汰
                    break
                old_size = self._mem_cache_sizes.pop(victim, 0)
                self._mem_cache_bytes_total -= old_size
                del self._mem_cache[victim]
                evicted += 1
            if evicted > 0:
                logger.debug(
                    "mem_cache 淘汰 %d 项, 当前 %d 项 / %.1f MB",
                    evicted, len(self._mem_cache),
                    self._mem_cache_bytes_total / (1024 * 1024)
                )

            # 写入或覆盖
            if key in self._mem_cache:
                old_size = self._mem_cache_sizes.pop(key, 0)
                self._mem_cache_bytes_total -= old_size
            self._mem_cache[key] = value
            self._mem_cache.move_to_end(key)  # 新项放尾部(最新)
            self._mem_cache_sizes[key] = est_bytes
            self._mem_cache_bytes_total += est_bytes
            if pinned:
                self._mem_cache_pinned.add(key)

    def _load_parsed_tar(self, exchange: str, product: str, period: str,
                         date: Optional[int] = None,
                         pin: bool = False) -> Dict[str, List[KSBarRecord]]:
        """加载并解析 tar.gz，返回 {code: [bars]}（修正日期）。优先走内存缓存。"""
        is_day = (period == 'kline_1d')
        key = self._mem_key(exchange, product, period, date)
        cached = self._mem_get(key)
        if cached is not None:
            return cached

        s3_key = self._s3_key(exchange, product, period, date)
        cache_path = self._cache_path(exchange, product, period, date)
        tar_data = self._download_tar(s3_key, cache_path)
        if tar_data is None:
            # P0-1 fix: 下载失败不缓存空结果，避免后续请求永久命中空值
            # 特别是在 prefetch(pin=True) 场景下，空结果被 pin 后永远无法重试
            logger.warning("S3下载失败,不缓存空结果: %s (pin=%s)", key, pin)
            return {}

        result = self._extract_csv_from_tar(tar_data, code_filter=None, is_day=is_day)
        # 分钟线修正日期
        if not is_day and date is not None:
            for code, bars in result.items():
                for bar in bars:
                    if bar.date != date:
                        bar.date = date
        # P0-1 fix (扩展): tar 下载成功但解析后为空 dict 时也不缓存，避免被 pin 后永久驻留。
        # 例如 tar 内只有未识别格式或全被 filter 掉，会得到空 {}；如果允许 _mem_put(pin=True)，
        # 后续 _mem_get 会持续命中空结果，造成数据静默丢失。
        if not result:
            logger.warning("S3解析为空,不缓存空结果: %s (pin=%s)", key, pin)
            return {}
        self._mem_put(key, result, pinned=pin)
        return result

    # ==========================================
    # 并行预热
    # ==========================================

    def prefetch_minute_dates(self, exchange: str, product: str, period: str,
                              dates: List[int],
                              workers: Optional[int] = None,
                              pin: bool = True,
                              batch_size: Optional[int] = None) -> int:
        """并行下载+解压+解析多日分钟线，结果驻留内存供后续阶段命中。

        分批执行以控制峰值内存：每批最多 batch_size 个日期同时在线程池中，
        避免 SHFE.ag ~2400 个 m1 tar 全部并发导致 OOM。

        @return 成功加载的日期数
        """
        if not dates:
            return 0
        n = workers or _PREFETCH_WORKERS
        _batch = batch_size or int(os.environ.get('KS2WTPY_PREFETCH_BATCH', '200'))
        ok = 0
        # 跳过已在内存的
        pending = [d for d in dates
                   if self._mem_get(self._mem_key(exchange, product, period, d)) is None]
        if not pending:
            return len(dates)
        logger.info(f'预热 {exchange}.{product} {period}: {len(pending)}/{len(dates)} 日, '
                    f'workers={n}, batch_size={_batch}')
        # 分批提交，每批 _batch 个日期
        for batch_start in range(0, len(pending), _batch):
            batch = pending[batch_start:batch_start + _batch]
            with ThreadPoolExecutor(max_workers=n) as ex:
                futures = {
                    ex.submit(self._load_parsed_tar, exchange, product, period, d, pin): d
                    for d in batch
                }
                for fut in as_completed(futures):
                    d = futures[fut]
                    try:
                        res = fut.result()
                        if res:
                            ok += 1
                    except Exception as exc:
                        logger.warning(f'预热失败 {exchange}.{product} {period} {d}: {exc}')
            if len(pending) > _batch:
                logger.debug(f'预热批次 {batch_start // _batch + 1}/{(len(pending) - 1) // _batch + 1} '
                             f'完成, 已加载 {ok}/{len(pending)}')
        logger.info(f'预热完成 {exchange}.{product} {period}: 成功 {ok}/{len(pending)}')
        return ok

    def prefetch_day(self, exchange: str, product: str, pin: bool = True) -> bool:
        """预热日线 tar（单文件）。"""
        res = self._load_parsed_tar(exchange, product, 'kline_1d', None, pin=pin)
        return bool(res)

    def clear_mem_cache(self):
        """清空内存缓存（不影响磁盘 tar 缓存）。pinned 项也清。"""
        with self._mem_cache_lock:
            self._mem_cache.clear()
            self._mem_cache_pinned.clear()
            self._mem_cache_sizes.clear()
            self._mem_cache_bytes_total = 0

    # ==========================================
    # S3下载
    # ==========================================

    def _download_tar(self, s3_key: str, cache_path: Optional[str] = None) -> Optional[bytes]:
        """从S3下载tar.gz文件，优先使用缓存

        @s3_key     S3对象键，如 'SHFE/rb/L1/kline_1d/kline_1d.tar.gz'
        @cache_path 本地缓存路径
        """
        # 尝试缓存
        data = self._read_cached_tar(cache_path)
        if data is not None:
            return data

        # S3下载
        try:
            response = self.client.get_object(self.bucket, s3_key)
            data = response.read()
            response.close()
            logger.debug(f'S3下载: {s3_key} ({len(data)} bytes)')
        except Exception as e:
            logger.warning(f'S3下载失败: {s3_key}, 错误: {e}')
            return None

        # 写入缓存
        self._write_cached_tar(cache_path, data)
        return data

    def _s3_key(self, exchange: str, product: str, period: str,
                date: Optional[int] = None) -> str:
        """构建S3对象键"""
        if period == 'kline_1d':
            return f'{exchange}/{product}/L1/{period}/kline_1d.tar.gz'
        else:
            return f'{exchange}/{product}/L1/{period}/{date}.tar.gz'

    # ==========================================
    # tar.gz解压与CSV解析
    # ==========================================

    def _extract_csv_from_tar(self, tar_data: bytes,
                              code_filter: Optional[str] = None,
                              is_day: bool = False) -> Dict[str, List[KSBarRecord]]:
        """从tar.gz数据中提取CSV并解析

        @tar_data     tar.gz二进制数据
        @code_filter  合约代码过滤，None时提取所有
        @is_day       是否日线

        返回: {合约代码: [KSBarRecord, ...]}
        """
        result = defaultdict(list)
        try:
            tf = tarfile.open(fileobj=io.BytesIO(tar_data), mode='r:gz')
        except Exception as e:
            logger.warning(f'tar.gz解压失败: {e}')
            return dict(result)

        for member in tf.getmembers():
            if not member.name.endswith('.csv'):
                continue

            # 提取合约代码
            basename = os.path.basename(member.name)
            code = extract_code_from_filename(basename, is_day=is_day)
            if code is None:
                continue

            # 过滤
            if code_filter and code != code_filter:
                continue

            # 读取解析
            f = tf.extractfile(member)
            if f is None:
                continue
            content = f.read().decode('utf-8-sig')
            bars = parse_ks_csv_content(content, is_day=is_day)
            if bars:
                result[code].extend(bars)

        tf.close()
        return dict(result)

    # ==========================================
    # 公开接口 — 与KSDataReader兼容
    # ==========================================

    def list_exchanges(self) -> List[str]:
        """列出所有交易所"""
        result = []
        for obj in self.client.list_objects(self.bucket, recursive=False):
            name = obj.object_name.rstrip('/')
            if name in EXCHANGES:
                result.append(name)
        return sorted(result)

    def list_products(self, exchange: str) -> List[str]:
        """列出交易所下的所有品种"""
        if exchange in self._products_cache:
            return self._products_cache[exchange]

        products = []
        for obj in self.client.list_objects(self.bucket, prefix=f'{exchange}/',
                                            recursive=False):
            parts = obj.object_name.split('/')
            if len(parts) >= 2 and parts[1]:
                products.append(parts[1])

        self._products_cache[exchange] = sorted(products)
        return self._products_cache[exchange]

    def list_date_dirs(self, exchange: str, product: str, period: str) -> List[int]:
        """列出分钟线的日期目录（YYYYMMDD格式）

        返回排序后的日期列表
        """
        cache_key = (exchange, product, period)
        if cache_key in self._dates_cache:
            return self._dates_cache[cache_key]

        if period == 'kline_1d':
            return []

        prefix = f'{exchange}/{product}/L1/{period}/'
        dates = []
        for obj in self.client.list_objects(self.bucket, prefix=prefix,
                                            recursive=False):
            # 对象名: {prefix}20210104.tar.gz 或 {prefix}20210104/
            name = obj.object_name[len(prefix):]
            # 提取日期部分
            date_part = name.split('.')[0].rstrip('/')
            if date_part.isdigit() and len(date_part) == 8:
                dates.append(int(date_part))

        self._dates_cache[cache_key] = sorted(dates)
        return self._dates_cache[cache_key]

    def list_day_codes(self, exchange: str, product: str) -> List[str]:
        """列出日线目录下的所有合约代码

        需要下载日线tar.gz并查看内部文件列表。
        为避免重复下载，结果缓存到实例变量。
        """
        # 尝试缓存
        cache_key = (exchange, product, 'day_codes')
        if not hasattr(self, '_day_codes_cache'):
            self._day_codes_cache = {}
        if cache_key in self._day_codes_cache:
            return self._day_codes_cache[cache_key]

        s3_key = self._s3_key(exchange, product, 'kline_1d')
        cache_path = self._cache_path(exchange, product, 'kline_1d')
        tar_data = self._download_tar(s3_key, cache_path)
        if tar_data is None:
            return []

        codes = []
        try:
            tf = tarfile.open(fileobj=io.BytesIO(tar_data), mode='r:gz')
            for member in tf.getmembers():
                if not member.name.endswith('.csv'):
                    continue
                basename = os.path.basename(member.name)
                code = extract_code_from_filename(basename, is_day=True)
                if code:
                    codes.append(code)
            tf.close()
        except Exception as e:
            logger.warning(f'列出日线合约失败: {e}')

        self._day_codes_cache[cache_key] = sorted(codes)
        return self._day_codes_cache[cache_key]

    def read_day_csv(self, exchange: str, product: str, code: str) -> List[KSBarRecord]:
        """读取日线CSV（按合约合并的单文件）

        @code 合约代码如 IF1810
        """
        result = self._load_parsed_tar(exchange, product, 'kline_1d', None)
        return result.get(code, [])

    def read_all_codes_day(self, exchange: str, product: str) -> Dict[str, List[KSBarRecord]]:
        """读取某品种所有合约的日线数据

        返回: {合约代码: [KSBarRecord, ...]}
        """
        return self._load_parsed_tar(exchange, product, 'kline_1d', None)

    def read_minute_csv(self, exchange: str, product: str, period: str,
                        date: int, code: str = None) -> List[KSBarRecord]:
        """读取分钟线CSV（按日）

        @date  YYYYMMDD
        @code  合约代码，为None时读取该日所有合约
        """
        result = self._load_parsed_tar(exchange, product, period, date)
        if code:
            return result.get(code, [])
        all_bars = []
        for c, bars in result.items():
            all_bars.extend(bars)
        return all_bars

    def read_date_minute(self, exchange: str, product: str, period: str,
                         date: int) -> Dict[str, List[KSBarRecord]]:
        """读取某品种某日所有合约的分钟线数据

        返回: {合约代码: [KSBarRecord, ...]}
        """
        return self._load_parsed_tar(exchange, product, period, date)

    def is_option_code(self, exchange: str, product: str, code: str) -> bool:
        """判断是否为期权合约代码

        ⚠️ 经 2026-05-29 真实 S3 验证后修正的判定规则：

        独立期权 product 目录（整目录均为期权数据，product 整判 + 排除 *_main 合成主连）：
        - CFFEX: IO / HO / MO （股指期权，合约形如 IO2509-C-4250、含 IO_main 合成主连）

        期货+期权共享 product 目录（必须按合约代码模式过滤，期货合约保留）：
        - SHFE.op:  双胶纸期货品种(op<YYMM>) + 双胶纸期权(op<YYMM>{C|P}<strike>) 共处
        - SHFE 其它: cu/al/zn/pb/ni/sn/au/ag/ru/rb/sp/ss/hc/bu/fu/bc 等
        - CZCE/DCE/INE: 标的品种目录共享期货与期权（如 CZCE/SR 既有 SR2509 期货也有
          SR109P16800 期权）

        合约代码模式：
          (a) 含 '-C-' 或 '-P-'（如 IO2509-C-4250、m2609-P-2950）
          (b) <数字><C|P><数字{3+}>（如 AP610C7000、SR109P16800、op2601C3900、rb2405C3000）
        必须 3+ 位行权价数字才算期权，否则 IC1505 / AP605 等含 C/P 字母的期货合约会被误判。

        OPTION_PRODUCTS 列表此时仅作"文档/标识"用途，不参与判定。
        """
        import re

        # 仅 CFFEX(IO/HO/MO) 是真正的独立纯期权 product 目录
        if exchange == 'CFFEX' and product in ('IO', 'HO', 'MO'):
            return True
        # 其余全部按合约代码模式判定（SHFE.op 是双胶纸期货品种，期权按代码模式过滤）
        if '-C-' in code or '-P-' in code:
            return True
        if re.search(r'\d+[CP]\d{3,}', code):
            return True
        return False

    def list_data_types(self, exchange: str, product: str) -> List[str]:
        """列出品种可用的数据类型"""
        prefix = f'{exchange}/{product}/L1/'
        dtypes = []
        for obj in self.client.list_objects(self.bucket, prefix=prefix,
                                            recursive=False):
            dtype = obj.object_name[len(prefix):].rstrip('/')
            if dtype:
                dtypes.append(dtype)
        return sorted(dtypes)

    def clear_cache(self):
        """清除元数据缓存"""
        self._products_cache.clear()
        self._dates_cache.clear()
        if hasattr(self, '_day_codes_cache'):
            self._day_codes_cache.clear()

    def get_stats(self) -> Dict[str, any]:
        """获取读取器统计信息"""
        total_products = sum(len(v) for v in self._products_cache.values())
        return {
            'bucket': self.bucket,
            'cached_products': total_products,
            'cached_dates_keys': len(self._dates_cache),
            'cache_dir': self.cache_dir,
        }

    # ==========================================
    # Tick数据接口
    # ==========================================

    def list_tick_dates(self, exchange: str, product: str) -> List[int]:
        """列出品种可用的tick日期目录

        KS S3 tick路径: {EXCHG}/{PRODUCT}/L1/tick/{YYYYMMDD}.tar.gz
        """
        return self.list_date_dirs(exchange, product, 'tick')

    def read_date_tick(self, exchange: str, product: str, date: int,
                       code_filter: Optional[str] = None) -> Dict[str, List[KSTickRecord]]:
        """读取某品种某日tick数据

        @exchange   交易所
        @product    品种
        @date       YYYYMMDD
        @code_filter 合约代码过滤，None时读取所有合约

        返回: {合约代码: [KSTickRecord, ...]}

        Tick数据不走内存缓存(体量太大)，直接从S3下载+解压+解析。
        如需缓存，使用本地磁盘缓存(tar.gz级别)。

        注意: 此接口会将整日 tar 内全部合约一次性载入内存（rb 单日≈数百MB）；
        若需逐合约流式处理大体量品种(IF/cu等)请改用 iter_date_tick。
        """
        s3_key = f'{exchange}/{product}/L1/tick/{date}.tar.gz'
        cache_path = self._cache_path(exchange, product, 'tick', date)
        tar_data = self._download_tar(s3_key, cache_path)
        if tar_data is None:
            logger.warning("S3 tick下载失败: %s", s3_key)
            return {}

        return self._extract_tick_csv_from_tar(tar_data, code_filter=code_filter)

    def iter_date_tick(self, exchange: str, product: str, date: int,
                       code_filter: Optional[str] = None):
        """流式迭代某品种某日 tick 数据，按 tar 成员逐 (code, ticks) yield。

        P1-1 fix: 替代 read_date_tick 的一次性全量解析，让上层
        tick_converter 可以逐合约 encode+write+release，将单日峰值内存
        从"整日全部合约 raw ticks 总和"降到"当前单合约 ticks"。

        @yield: (code: str, ticks: List[KSTickRecord])
        """
        import tarfile as _tar
        s3_key = f'{exchange}/{product}/L1/tick/{date}.tar.gz'
        cache_path = self._cache_path(exchange, product, 'tick', date)
        tar_data = self._download_tar(s3_key, cache_path)
        if tar_data is None:
            logger.warning("S3 tick下载失败: %s", s3_key)
            return

        try:
            tf = _tar.open(fileobj=io.BytesIO(tar_data), mode='r:gz')
        except Exception as e:
            logger.warning(f'tick tar.gz解压失败: {e}')
            return

        try:
            for member in tf.getmembers():
                if not member.name.endswith('.csv'):
                    continue
                basename = os.path.basename(member.name)
                code = extract_code_from_filename(basename, is_day=False)
                if code is None:
                    continue
                if code_filter and code != code_filter:
                    continue
                f = tf.extractfile(member)
                if f is None:
                    continue
                content = f.read().decode('utf-8-sig')
                ticks = parse_ks_tick_csv_content(content)
                if ticks:
                    yield code, ticks
        finally:
            tf.close()

    def _extract_tick_csv_from_tar(self, tar_data: bytes,
                                    code_filter: Optional[str] = None) -> Dict[str, List[KSTickRecord]]:
        """从tick tar.gz中提取CSV并解析为KSTickRecord

        返回: {合约代码: [KSTickRecord, ...]}
        """
        result = defaultdict(list)
        try:
            tf = tarfile.open(fileobj=io.BytesIO(tar_data), mode='r:gz')
        except Exception as e:
            logger.warning(f'tick tar.gz解压失败: {e}')
            return dict(result)

        for member in tf.getmembers():
            if not member.name.endswith('.csv'):
                continue

            basename = os.path.basename(member.name)
            # Tick文件名: {CODE}_{YYYYMMDD}.csv
            code = extract_code_from_filename(basename, is_day=False)
            if code is None:
                continue

            if code_filter and code != code_filter:
                continue

            f = tf.extractfile(member)
            if f is None:
                continue
            content = f.read().decode('utf-8-sig')
            ticks = parse_ks_tick_csv_content(content)
            if ticks:
                result[code].extend(ticks)

        tf.close()
        return dict(result)


def create_s3_reader(config: dict = None) -> S3KsReader:
    """工厂函数: 从配置创建S3KsReader

    config示例:
    {
        "endpoint": "S3_ENDPOINT",
        "access_key": "S3_ACCESS_KEY",
        "secret_key": "S3_SECRET_KEY",
        "bucket": "ks-data",
        "secure": False,
        "cache_dir": "/path/to/cache"
    }
    """
    if config is None:
        config = {}

    return S3KsReader(
        endpoint=config.get('endpoint', ''),
        access_key=config.get('access_key', ''),
        secret_key=config.get('secret_key', ''),
        bucket=config.get('bucket', 'ks-data'),
        secure=config.get('secure', False),
        cache_dir=config.get('cache_dir'),
    )
