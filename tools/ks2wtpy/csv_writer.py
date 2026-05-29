#!/usr/bin/env python3
"""
wtpy CSV格式转换器 (方案A)

将KS K线数据转换为wtpy回测引擎可直接使用的CSV格式。

wtpy CSV格式:
  日线: date,open,high,low,close,volume,turnover,open_interest
  分钟线: date,time,open,high,low,close,volume,turnover,open_interest

  日期: YYYY/MM/DD 或 YYYYMMDD (引擎都支持)
  时间: HH:MM:SS (分钟线)
  日线无time列

文件命名:
  合约级: {EXCHG}.{CODE}_{period}.csv  (如 SHFE.rb2605_m5.csv)
  主力合约: {EXCHG}.{PRODUCT}.HOT_{period}.csv (如 SHFE.rb.HOT_m5.csv)

文件路径:
  {csv_output_dir}/{EXCHG}.{CODE}_{period}.csv

首次回测时引擎自动将CSV转存为DSB二进制格式到 his/ 目录。
"""

import os
import csv
import math
import logging
import shutil
from typing import Dict, List, Optional, Tuple, Set
from collections import defaultdict

from config import PERIOD_MAP, WTPY_SUPPORTED_PERIODS
from ks_reader import KSDataReader, KSBarRecord, KSCodeData

logger = logging.getLogger(__name__)


def format_date_wtpy(date_val: int) -> str:
    """将YYYYMMDD格式转为YYYY/MM/DD

    wtpy的strToDate函数同时支持两种格式，
    但使用斜杠分隔更符合wtpy demo惯例。
    """
    s = str(date_val)
    if len(s) == 8:
        return f"{s[:4]}/{s[4:6]}/{s[6:8]}"
    return s


def format_time_wtpy(time_val: int) -> str:
    """将HHMM格式转为HH:MM:SS

    wtpy的strToTime函数会去掉冒号，然后:
    - 如果>4位且bHasSec=false则/100
    - 所以HH:MM:SS和HH:MM都可以，用HH:MM:SS更标准
    """
    s = str(time_val).zfill(4)
    hh = s[:-2]
    mm = s[-2:]
    return f"{hh}:{mm}:00"


def write_wtpy_hot_csv_with_logret(filepath: str,
                                   segments: List[Tuple[str, List[KSBarRecord], Optional[float]]],
                                   is_day: bool = False):
    """写入 HOT 拼接 CSV，并附带 logret 列。

    @segments: [(code, bars_in_this_code_segment, prev_close_anchor), ...]
               按时间顺序排好的段。
               - code: 该段对应的实际合约（如 rb2510）
               - bars_in_this_code_segment: 该合约在 [from_date, to_date) 区间的 bars
               - prev_close_anchor: 该合约在 from_date **之前** 最近一根 bar 的 close
                 用于段首 bar 计算 logret —— 实现"换月时用新合约自身的前一根"语义。
                 若新合约就是首次上市当日成为主力（切换日就是该合约史首根），
                 传 None，则段首 bar 的 logret = 0。

    logret 公式: logret_i = ln(close_i / close_{i-1})
      - 段内第 0 根: prev = prev_close_anchor (新合约切换日之前的最后一根)
      - 段内第 k>0 根: prev = bars[k-1].close
      - 全合约史第 0 根 (HOT 整个序列首根): prev_anchor=None → logret=0

    输出 schema:
      日线:    date,open,high,low,close,volume,turnover,open_interest,logret,code
      分钟线:  date,time,open,high,low,close,volume,turnover,open_interest,logret,code
    """
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    if is_day:
        header = ['date', 'open', 'high', 'low', 'close',
                  'volume', 'turnover', 'open_interest', 'logret', 'code']
    else:
        header = ['date', 'time', 'open', 'high', 'low', 'close',
                  'volume', 'turnover', 'open_interest', 'logret', 'code']

    total = 0
    with open(filepath, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(header)

        for code, bars, prev_anchor in segments:
            if not bars:
                continue
            prev_close: Optional[float] = prev_anchor
            for bar in bars:
                base_row = ks_bar_to_wtpy_csv_row(bar, is_day)
                if prev_close is None or prev_close <= 0 or bar.close <= 0:
                    logret = 0.0
                else:
                    try:
                        logret = math.log(bar.close / prev_close)
                    except (ValueError, ZeroDivisionError):
                        logret = 0.0
                writer.writerow(base_row + [f"{logret:.10f}", code])
                prev_close = bar.close
                total += 1

    logger.info(f"写入HOT CSV (含logret): {filepath} ({total}条)")


def ks_bar_to_wtpy_csv_row(bar: KSBarRecord, is_day: bool = False) -> List[str]:
    """将KSBarRecord转为wtpy CSV行

    wtpy CSV字段:
      日线: date,open,high,low,close,volume,turnover,open_interest
      分钟线: date,time,open,high,low,close,volume,turnover,open_interest

    注意: wtpy还支持settle和diff_interest字段，但CSV中可选
    """
    date_str = format_date_wtpy(bar.date)

    if is_day:
        return [
            date_str,
            str(bar.open),
            str(bar.high),
            str(bar.low),
            str(bar.close),
            str(bar.volume),
            str(bar.amount),        # amount → turnover
            str(bar.openinterest),  # openinterest → open_interest
        ]
    else:
        time_str = format_time_wtpy(bar.time)
        return [
            date_str,
            time_str,
            str(bar.open),
            str(bar.high),
            str(bar.low),
            str(bar.close),
            str(bar.volume),
            str(bar.amount),
            str(bar.openinterest),
        ]


def write_wtpy_csv(filepath: str, bars: List[KSBarRecord], is_day: bool = False):
    """写入wtpy CSV文件

    @filepath 输出文件路径
    @bars K线数据列表（需已排序）
    @is_day 是否日线
    """
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    if is_day:
        header = ['date', 'open', 'high', 'low', 'close', 'volume', 'turnover', 'open_interest']
    else:
        header = ['date', 'time', 'open', 'high', 'low', 'close', 'volume', 'turnover', 'open_interest']

    with open(filepath, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for bar in bars:
            row = ks_bar_to_wtpy_csv_row(bar, is_day)
            writer.writerow(row)

    logger.info(f"写入CSV: {filepath} ({len(bars)}条)")


def append_wtpy_csv(filepath: str, new_bars: List[KSBarRecord], is_day: bool = False,
                    existing_dates: Optional[set] = None):
    """增量追加数据到wtpy CSV文件

    @existing_dates 已存在的日期集合，用于去重
    """
    if not new_bars:
        return

    # 过滤已存在的数据
    if existing_dates:
        if is_day:
            new_bars = [b for b in new_bars if b.date not in existing_dates]
        else:
            new_bars = [b for b in new_bars if (b.date, b.time) not in existing_dates]

    if not new_bars:
        logger.debug(f"无新增数据需要追加: {filepath}")
        return

    # 如果文件不存在，直接写入
    if not os.path.exists(filepath):
        write_wtpy_csv(filepath, new_bars, is_day)
        return

    # 追加模式
    with open(filepath, 'a', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        for bar in new_bars:
            row = ks_bar_to_wtpy_csv_row(bar, is_day)
            writer.writerow(row)

    logger.info(f"追加CSV: {filepath} (+{len(new_bars)}条)")


def read_existing_csv_dates(filepath: str, is_day: bool = False) -> set:
    """读取已有CSV文件中的日期集合，用于增量去重"""
    dates = set()
    if not os.path.exists(filepath):
        return dates

    try:
        with open(filepath, 'r', encoding='utf-8-sig') as f:
            reader = csv.reader(f)
            header = next(reader, None)  # 跳过表头
            for row in reader:
                if not row:
                    continue
                date_str = row[0].replace('/', '').replace('-', '')
                if not date_str.isdigit():
                    continue
                date_val = int(date_str)
                if is_day:
                    dates.add(date_val)
                else:
                    time_str = row[1].replace(':', '') if len(row) > 1 else '0'
                    time_val = int(time_str[:4]) if time_str.isdigit() else 0
                    dates.add((date_val, time_val))
    except Exception as e:
        logger.warning(f"读取已有CSV失败: {filepath}, 错误: {e}")

    return dates


def read_wtpy_csv_bars(filepath: str, is_day: bool = False) -> List[KSBarRecord]:
    """读取已生成的wtpy CSV，用于增量合并后保持全文件有序。"""
    bars: List[KSBarRecord] = []
    if not os.path.exists(filepath):
        return bars
    with open(filepath, 'r', encoding='utf-8-sig', newline='') as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if not row:
                continue
            date_val = int(row[0].replace('/', '').replace('-', ''))
            if is_day:
                offset = 1
                time_val = 0
            else:
                time_str = row[1].replace(':', '') if len(row) > 1 else '0'
                time_val = int(time_str[:4]) if time_str.isdigit() else 0
                offset = 2
            bars.append(KSBarRecord(
                date=date_val,
                time=time_val,
                open=float(row[offset]),
                high=float(row[offset + 1]),
                low=float(row[offset + 2]),
                close=float(row[offset + 3]),
                volume=float(row[offset + 4]),
                amount=float(row[offset + 5]),
                openinterest=float(row[offset + 6]),
            ))
    return bars


class CsvConverter:
    """KS → wtpy CSV 转换器"""

    def __init__(self, ks_reader: KSDataReader, output_dir: str,
                 incremental: bool = True, include_option: bool = False,
                 option_only: bool = False):
        """
        @ks_reader KS数据读取器
        @output_dir CSV输出目录 (如 storage/csv/)
        @incremental 是否增量模式
        @include_option 是否在共享品种目录内保留期权合约
        @option_only 纯期权独立流水线 (CFFEX IO/HO/MO):
            - 仅保留期权合约 (is_option_code() == True)
            - 主动丢弃 *_main / *_index 合成主连 (方案 A)
            - 与 include_option 互斥优先 (option_only=True 时无视 include_option)
        """
        self.ks_reader = ks_reader
        self.output_dir = output_dir
        self.incremental = incremental
        self.include_option = include_option
        self.option_only = option_only

    @staticmethod
    def _is_synthetic_main(code: str) -> bool:
        """合成主连/index 后缀，纯期权目录必丢弃。"""
        return code.endswith('_main') or code.endswith('_index') or code.endswith('.HOT')

    def _accept_code(self, exchange: str, product: str, code: str) -> bool:
        """统一的合约级过滤：option_only 优先，其次 include_option。"""
        if self._is_synthetic_main(code):
            # option_only 模式严格丢弃合成主连；普通模式由 HOT 流水线另行处理
            return not self.option_only
        is_opt = self.ks_reader.is_option_code(exchange, product, code)
        if self.option_only:
            return is_opt
        if not self.include_option and is_opt:
            return False
        return True

    def get_csv_path(self, exchange: str, code: str, period_suffix: str) -> str:
        """获取CSV文件路径

        命名规则: {EXCHG}.{CODE}_{period}.csv
        主力合约: {EXCHG}.{PRODUCT}.HOT_{period}.csv
        """
        return os.path.join(self.output_dir, f"{exchange}.{code}_{period_suffix}.csv")

    def _write_wt_hot_alias_if_needed(self, exchange: str, product: str, code: str, period_suffix: str):
        """为 KS 连续序列生成 wtpy CSV loader 直接查找的 PRODUCT.HOT 文件。"""
        if code != f"{product}_main":
            return
        src = self.get_csv_path(exchange, code, period_suffix)
        dst = self.get_csv_path(exchange, f"{product}.HOT", period_suffix)
        if not os.path.exists(src):
            return
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        logger.info(f"生成WT HOT兼容CSV: {dst} -> {src}")

    # 注：_write_wt_contract_alias_if_needed 已删除（修订方案 A, fix-dup）。
    # 原意是把 IC1505_d.csv 复制成 IC.1505_d.csv 以"匹配 WT 三段 stdCode"，
    # 但 CSV 不是 WT C++ 消费物（C++ 只读 his/{period}/{exchg}/{code}.dsb），
    # alias 纯属磁盘冗余 + 扫描歧义，且与 wtpy datahelper 现有约定
    # (SSE.600000_d.csv 不带点) 冲突。DSB 物理文件名一直是不带点
    # (IC1505.dsb)，符合 C++ extractStdCode 结果；CSV 统一不带点。
    # acceptance 反推三段 stdCode (CFFEX.IC.1505) 在 wt_readback gate
    # 通过 commodities.json product 字典从 DSB 文件名反推得到，不再依赖 CSV alias。

    def convert_code_day(self, exchange: str, product: str, code: str) -> bool:
        """转换单个合约的日线数据"""
        csv_path = self.get_csv_path(exchange, code, 'd')

        # 增量模式：读取已有数据
        existing_dates = set()
        if self.incremental and os.path.exists(csv_path):
            existing_dates = read_existing_csv_dates(csv_path, is_day=True)

        # 读取KS日线数据
        bars = self.ks_reader.read_day_csv(exchange, product, code)
        if not bars:
            return False

        # 过滤已存在的数据
        if existing_dates:
            bars = [b for b in bars if b.date not in existing_dates]

        if not bars:
            logger.debug(f"日线无新增: {exchange}.{code}")
            return True

        # 排序
        bars.sort(key=lambda b: b.date)

        if self.incremental and os.path.exists(csv_path):
            self._write_incremental_csv(csv_path, bars, is_day=True, existing_keys=existing_dates)
        else:
            write_wtpy_csv(csv_path, bars, is_day=True)
        self._write_wt_hot_alias_if_needed(exchange, product, code, 'd')

        return True

    def _write_incremental_csv(self, filepath: str, new_bars: List[KSBarRecord],
                               is_day: bool = False, existing_keys: Optional[Set] = None):
        """增量写入CSV。

        P1-8 fix: KS 数据按时间有序产出，优先真追加；若新数据时间戳可能乱序则回退全量重排。
        """
        if existing_keys:
            if is_day:
                new_bars = [b for b in new_bars if b.date not in existing_keys]
            else:
                new_bars = [b for b in new_bars if (b.date, b.time) not in existing_keys]
        if not new_bars:
            return

        # 如果文件不存在，直接写入
        if not os.path.exists(filepath):
            write_wtpy_csv(filepath, new_bars, is_day=is_day)
            return

        # 检查能否安全追加：新数据的最小时间戳必须 >= 已有数据的最大时间戳
        can_append = True
        if is_day:
            new_min_date = min(b.date for b in new_bars)
            existing_dates = read_existing_csv_dates(filepath, is_day=True)
            if existing_dates and new_min_date < max(existing_dates):
                can_append = False
        else:
            new_min_key = min((b.date, b.time) for b in new_bars)
            existing_keys_full = read_existing_csv_dates(filepath, is_day=False)
            if existing_keys_full and new_min_key < max(existing_keys_full):
                can_append = False

        if can_append:
            # 真追加：O(新增条数)
            append_wtpy_csv(filepath, new_bars, is_day=is_day, existing_dates=None)
        else:
            # 回退全量重排：保证时间有序（罕见路径，仅在数据乱序时触发）
            all_bars = read_wtpy_csv_bars(filepath, is_day=is_day) + new_bars
            if is_day:
                all_bars.sort(key=lambda b: b.date)
                dedup = {}
                for bar in all_bars:
                    dedup[bar.date] = bar
                all_bars = [dedup[k] for k in sorted(dedup)]
            else:
                all_bars.sort(key=lambda b: (b.date, b.time))
                dedup = {}
                for bar in all_bars:
                    dedup[(bar.date, bar.time)] = bar
                all_bars = [dedup[k] for k in sorted(dedup)]
            write_wtpy_csv(filepath, all_bars, is_day=is_day)

    def convert_code_minute(self, exchange: str, product: str, code: str,
                            period: str, dates: List[int] = None) -> bool:
        """转换单个合约的分钟线数据

        @dates 指定日期列表，为None时转换所有日期
        """
        period_suffix = PERIOD_MAP.get(period, (None,))[0]
        if period_suffix is None:
            logger.warning(f"不支持的周期: {period}")
            return False

        csv_path = self.get_csv_path(exchange, code, period_suffix)

        # 增量模式
        existing_keys = set()
        if self.incremental and os.path.exists(csv_path):
            existing_keys = read_existing_csv_dates(csv_path, is_day=False)

        # 获取日期目录列表
        all_dates = self.ks_reader.list_date_dirs(exchange, product, period)
        if dates:
            all_dates = [d for d in all_dates if d in set(dates)]

        if not all_dates:
            return False

        # 逐日读取并合并
        all_bars = []
        for date in all_dates:
            date_data = self.ks_reader.read_date_minute(exchange, product, period, date)
            if code in date_data:
                all_bars.extend(date_data[code])

        if not all_bars:
            return False

        # 过滤已存在的数据
        if existing_keys:
            all_bars = [b for b in all_bars if (b.date, b.time) not in existing_keys]

        if not all_bars:
            logger.debug(f"分钟线无新增: {exchange}.{code}_{period_suffix}")
            return True

        # 排序
        all_bars.sort(key=lambda b: (b.date, b.time))

        if self.incremental and os.path.exists(csv_path):
            self._write_incremental_csv(csv_path, all_bars, is_day=False, existing_keys=existing_keys)
        else:
            write_wtpy_csv(csv_path, all_bars, is_day=False)
        self._write_wt_hot_alias_if_needed(exchange, product, code, period_suffix)

        return True

    def convert_product(self, exchange: str, product: str,
                        periods: List[str] = None,
                        dates: List[int] = None) -> Dict[str, int]:
        """转换某品种所有合约的数据

        返回: {周期: 转换合约数}
        """
        if periods is None:
            periods = ['kline_1d', 'kline_5m']

        stats = defaultdict(int)

        for period in periods:
            if period not in WTPY_SUPPORTED_PERIODS:
                continue

            is_day = (period == 'kline_1d')

            if is_day:
                # 日线：按合约文件读取
                codes = self.ks_reader.list_day_codes(exchange, product)
                for code in codes:
                    if not self._accept_code(exchange, product, code):
                        continue
                    if self.convert_code_day(exchange, product, code):
                        stats[period] += 1
            else:
                # 分钟线：按日期目录读取
                all_dates = self.ks_reader.list_date_dirs(exchange, product, period)
                if dates:
                    all_dates = [d for d in all_dates if d in set(dates)]
                if not all_dates:
                    continue

                # 先收集所有合约代码
                code_bars = defaultdict(list)
                for date in all_dates:
                    date_data = self.ks_reader.read_date_minute(
                        exchange, product, period, date)
                    for code, bars in date_data.items():
                        if not self._accept_code(exchange, product, code):
                            continue
                        code_bars[code].extend(bars)

                # 逐合约写入
                period_suffix = PERIOD_MAP[period][0]
                for code, bars in code_bars.items():
                    csv_path = self.get_csv_path(exchange, code, period_suffix)

                    existing_keys = set()
                    if self.incremental and os.path.exists(csv_path):
                        existing_keys = read_existing_csv_dates(csv_path, is_day=False)

                    if existing_keys:
                        bars = [b for b in bars if (b.date, b.time) not in existing_keys]

                    if not bars:
                        continue

                    bars.sort(key=lambda b: (b.date, b.time))

                    if self.incremental and os.path.exists(csv_path):
                        self._write_incremental_csv(csv_path, bars, is_day=False, existing_keys=existing_keys)
                    else:
                        write_wtpy_csv(csv_path, bars, is_day=False)
                    self._write_wt_hot_alias_if_needed(exchange, product, code, period_suffix)

                    stats[period] += 1

        return dict(stats)

    def build_hot_csv(self, exchange: str, product: str, events,
                      periods: List[str] = None):
        """根据主力切换 events 生成 HOT 一等公民 CSV。

        @events: List[Dict] 形如 hots.json[EXCHG][product]，
                 [{date, from, to, oldclose, newclose}, ...]
                 按 [event[i].date, event[i+1].date) 区间用 event[i].to 合约 bars 拼接，
                 末段开放区间（最后一个事件直到合约结束）。

        兼容旧签名: 若 events 是 Dict[int,str]（旧 hot_map 形态），自动转 events 列表。
        """
        if periods is None:
            periods = ['kline_1d', 'kline_5m']

        # 兼容旧 hot_map 形态: {date: code} -> events
        if isinstance(events, dict):
            sorted_items = sorted(events.items(), key=lambda kv: int(kv[0]))
            events = [
                {
                    'date': int(d),
                    'from': '' if i == 0 else sorted_items[i - 1][1],
                    'to': code,
                    'oldclose': 0.0,
                    'newclose': 0.0,
                }
                for i, (d, code) in enumerate(sorted_items)
            ]

        if not events:
            return

        events = sorted(events, key=lambda ev: int(ev['date']))

        for period in periods:
            if period not in WTPY_SUPPORTED_PERIODS:
                continue

            is_day = (period == 'kline_1d')
            period_suffix = PERIOD_MAP[period][0]
            csv_path = self.get_csv_path(exchange, f"{product}.HOT", period_suffix)

            hot_bars: List[KSBarRecord] = []
            # 预拉取每个 to 合约的全量 bars（按合约缓存避免重复 IO）
            cache: Dict[str, List[KSBarRecord]] = {}

            def _bars_for(code: str) -> List[KSBarRecord]:
                if code not in cache:
                    if is_day:
                        cache[code] = self.ks_reader.read_day_csv(exchange, product, code) or []
                    else:
                        # 分钟线: 走 list_date_dirs + read_date_minute
                        all_dates = self.ks_reader.list_date_dirs(exchange, product, period)
                        bars: List[KSBarRecord] = []
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
                # 区间 [start_date, end_date)
                segment = [b for b in contract_bars if start_date <= b.date < end_date]
                hot_bars.extend(segment)

            if not hot_bars:
                logger.warning(f"HOT 拼接无数据: {exchange}.{product} period={period}")
                continue

            if is_day:
                hot_bars.sort(key=lambda b: b.date)
            else:
                hot_bars.sort(key=lambda b: (b.date, b.time))

            write_wtpy_csv(csv_path, hot_bars, is_day)
            logger.info(f"生成HOT合约CSV: {csv_path} ({len(hot_bars)}条, "
                        f"覆盖 {len(events)} 段切换)")

            # 同时生成带 logret 的 HOT CSV（一等公民产物，供研究使用）
            # 命名：{EXCHG}.{PRODUCT}.HOT_{period}_logret.csv
            logret_path = csv_path.replace(
                f"_{period_suffix}.csv",
                f"_{period_suffix}_logret.csv"
            )
            try:
                segments_with_anchor: List[Tuple[str, List[KSBarRecord], Optional[float]]] = []
                for i, ev in enumerate(events):
                    code = ev['to']
                    if not code:
                        continue
                    start_date = int(ev['date'])
                    end_date = int(events[i + 1]['date']) if i + 1 < len(events) else 99999999
                    contract_bars = _bars_for(code)  # 已 cache
                    # 段：合约在 [start_date, end_date)
                    if is_day:
                        sorted_bars = sorted(contract_bars, key=lambda b: b.date)
                    else:
                        sorted_bars = sorted(contract_bars, key=lambda b: (b.date, b.time))
                    seg = [b for b in sorted_bars if start_date <= b.date < end_date]
                    if not seg:
                        continue
                    # prev_anchor：该合约在 start_date 之前的最近一根 close
                    # P2-14 fix: 用 bisect 替代 list.index()，O(logN) vs O(N)
                    import bisect
                    if is_day:
                        first_idx = bisect.bisect_left(sorted_bars, start_date,
                                                        key=lambda b: b.date)
                    else:
                        first_idx = bisect.bisect_left(sorted_bars, (start_date, 0),
                                                        key=lambda b: (b.date, b.time))
                    prev_anchor: Optional[float] = None
                    if first_idx > 0:
                        prev_anchor = float(sorted_bars[first_idx - 1].close)
                    segments_with_anchor.append((code, seg, prev_anchor))
                if segments_with_anchor:
                    write_wtpy_hot_csv_with_logret(logret_path, segments_with_anchor, is_day)
            except Exception as exc:
                logger.warning(f"生成 logret HOT CSV 失败: {logret_path}, 错误: {exc}")
