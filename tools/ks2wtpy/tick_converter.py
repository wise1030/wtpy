#!/usr/bin/env python3
"""
KS Tick数据 → WonderTrader DSB 流式转换器

架构:
  方案B (流式): S3 tick tar.gz → 解压CSV → KSTickRecord → WTSTickStruct → zstd → DSB
  - 不走CSV/LMDB中间层，直接S3→DSB
  - 不走内存缓存(tick体量太大)
  - 利用S3 tar.gz磁盘缓存避免重复下载
  - 按天+按合约粒度写入DSB

WT Tick DSB目录结构:
  {base}/his/ticks/{EXCHG}/{YYYYMMDD}/{CODE}.dsb

KS Tick CSV → WTSTickStruct 映射:
  KS timestamp(Unix ms) → action_date + action_time(HHMMSSMMM)
  KS累计volume/amount → WT total_volume/total_turnover + 计算增量volume/turn_over
  KS买卖5档 → WT买卖10档(6-10档填0)
  KS缺少字段(settle/limit/pre_close等) → 填0
"""

import os
import logging
from typing import Dict, List, Optional
from collections import defaultdict

from config import EXCHANGES, OPTION_PRODUCTS
from ks_reader import KSTickRecord
from dsb_writer import (
    encode_ticks_to_bytes, write_tick_dsb, read_tick_dsb_file,
    TICK_DSB_SUBDIR, WTS_TICK_STRUCT_SIZE,
)

logger = logging.getLogger(__name__)


class TickConverter:
    """KS Tick → WT DSB 流式转换器

    设计原则:
    - 不缓存tick解析结果(内存压力)
    - S3 tar.gz磁盘缓存复用S3KsReader机制
    - 逐天处理，每个交易日的每个合约生成一个DSB文件
    - 过滤期权/价差等非期货合约
    """

    def __init__(self, s3_reader, output_dir: str, use_compression: bool = True,
                 include_option: bool = False, option_only: bool = False):
        """
        @s3_reader    S3KsReader实例
        @output_dir   DSB输出根目录 (his/)
        @use_compression 是否使用zstd压缩
        @include_option 是否包含期权
        @option_only 纯期权独立流水线 (CFFEX IO/HO/MO):仅保留期权,丢弃 *_main/*_index/SP/&
        """
        self.s3_reader = s3_reader
        self.output_dir = output_dir
        self.use_compression = use_compression
        self.include_option = include_option
        self.option_only = option_only

    def _tick_dsb_path(self, exchange: str, date: int, code: str) -> str:
        """生成tick DSB文件路径

        WT C++路径规则: his/ticks/{exchg}/{date}/{code}.dsb
        """
        # WT C++路径规则: his/ticks/{exchg}/{date}/{code}.dsb
        # code直接使用原始合约代码(与bar DSB一致)
        return os.path.join(self.output_dir, TICK_DSB_SUBDIR,
                            exchange, str(date), f"{code}.dsb")

    def _should_skip_code(self, exchange: str, product: str, code: str) -> bool:
        """判断是否应跳过该合约代码(期权/价差/SP等)"""
        # 合成主连/index 在两种模式下都不该出现在 tick 流;option_only 必丢
        if code.endswith('_main') or code.endswith('_index') or code.endswith('.HOT'):
            return True
        # SP 价差 / 组合两种模式下都跳过
        if 'SP ' in code or '&' in code:
            return True
        if code.startswith('SP ') or code.startswith('SPD'):
            return True
        is_opt = self.s3_reader.is_option_code(exchange, product, code)
        # 兜底：is_option_code 漏判时用 '-C-/-P-' 形态二次识别为期权
        # 仅作为 is_opt 的扩充，不直接当跳过条件——避免 option_only 模式误丢期权
        if not is_opt and ('-C-' in code or '-P-' in code):
            is_opt = True
        if self.option_only:
            # 纯期权流水线:非期权(含期货/价差)一律丢弃
            if not is_opt:
                return True
            return False
        # 非 option_only: 期货保留;期权按 include_option 开关取舍
        if is_opt and not self.include_option:
            return True
        return False

    def convert_date(self, exchange: str, product: str, date: int,
                     code_filter: Optional[str] = None) -> Dict[str, int]:
        """转换单日tick数据为DSB

        @exchange     交易所
        @product      品种
        @date         交易日 YYYYMMDD（来自S3 tar文件名，通常是交易日，
                      但DSB目录名以tick数据中的trading_day为准）
        @code_filter  只转换指定合约

        返回: {合约代码: 写入tick数}

        P1-1 fix: 改用 s3_reader.iter_date_tick 流式按合约处理，
        峰值内存 = 当前单合约 ticks，替代原先整日 dict 全量加载。
        """
        stats: Dict[str, int] = {}
        # 流式 fallback：如 reader 无 iter_date_tick 则降级用 read_date_tick
        iter_fn = getattr(self.s3_reader, 'iter_date_tick', None)
        if iter_fn is None:
            tick_data = self.s3_reader.read_date_tick(
                exchange, product, date, code_filter=code_filter)
            if not tick_data:
                logger.warning("无tick数据: %s.%s %d", exchange, product, date)
                return {}
            iterable = tick_data.items()
        else:
            iterable = iter_fn(exchange, product, date, code_filter=code_filter)

        seen_any = False
        for code, ticks in iterable:
            seen_any = True
            if self._should_skip_code(exchange, product, code):
                continue
            if not ticks:
                continue

            # 校验trading_day: 以tick数据中的trading_day为准作为DSB目录名
            actual_trading_day = ticks[0].trading_day
            if actual_trading_day != date:
                logger.warning(
                    "trading_day(%d) != date参数(%d), 以trading_day为准: "
                    "code=%s, exchange=%s, product=%s",
                    actual_trading_day, date, code, exchange, product)

            # 编码 + 写入 + 立即释放（关键：del 让 GC 在下一合约前回收内存）
            ticks_bytes = encode_ticks_to_bytes(ticks, exchange, code)
            filepath = self._tick_dsb_path(exchange, actual_trading_day, code)
            written = write_tick_dsb(filepath, ticks_bytes, self.use_compression)
            stats[code] = len(ticks)
            logger.debug("写入tick DSB: %s (%d条, %d字节)", filepath, len(ticks), written)
            del ticks, ticks_bytes

        if not seen_any:
            logger.warning("无tick数据: %s.%s %d", exchange, product, date)

        return stats

    def convert_product_dates(self, exchange: str, product: str,
                               dates: Optional[List[int]] = None,
                               code_filter: Optional[str] = None) -> Dict[str, int]:
        """转换品种多个交易日的tick数据

        @exchange     交易所
        @product      品种
        @dates        日期列表，None=全部可用日期
        @code_filter  合约代码过滤

        返回: {合约代码: 总写入tick数}
        """
        if dates is None:
            dates = self.s3_reader.list_tick_dates(exchange, product)
        if not dates:
            logger.warning("无tick日期: %s.%s", exchange, product)
            return {}

        total_stats: Dict[str, int] = defaultdict(int)
        for date in dates:
            day_stats = self.convert_date(exchange, product, date, code_filter=code_filter)
            for code, count in day_stats.items():
                total_stats[code] += count

        return dict(total_stats)

    def convert_product(self, exchange: str, product: str,
                        code_filter: Optional[str] = None) -> Dict[str, int]:
        """转换品种全部tick数据(全量)

        返回: {合约代码: 总写入tick数}
        """
        return self.convert_product_dates(exchange, product,
                                           dates=None,
                                           code_filter=code_filter)

    def verify_tick_dsb(self, exchange: str, date: int,
                         max_verify: int = 0) -> Dict:
        """验证tick DSB文件

        @max_verify 最多验证文件数，0=全部
        """
        dsb_dir = os.path.join(self.output_dir, TICK_DSB_SUBDIR, exchange, str(date))
        if not os.path.exists(dsb_dir):
            return {'exists': False}

        files = sorted(f for f in os.listdir(dsb_dir) if f.endswith('.dsb'))
        total_files = len(files)
        verify_files = files if max_verify == 0 else files[:max_verify]

        result = {
            'exists': True,
            'file_count': total_files,
            'verified_count': len(verify_files),
            'total_ticks': 0,
            'details': [],
        }

        for fname in verify_files:
            fpath = os.path.join(dsb_dir, fname)
            info = read_tick_dsb_file(fpath)
            if info:
                result['total_ticks'] += info['tick_count']
                result['details'].append({
                    'file': fname,
                    'ticks': info['tick_count'],
                    'compressed': info['compressed'],
                    'type': info['type'],
                })

        return result
