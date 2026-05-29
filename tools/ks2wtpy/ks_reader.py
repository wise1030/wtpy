#!/usr/bin/env python3
"""
KS数据读取器

支持从远程服务器或本地目录读取KS格式的期货K线数据。
KS目录结构:
  {base}/{EXCHG}/{PRODUCT}/L1/{kline_type}/
    kline_1d/kline_1d/{CODE}.csv          — 日线，按合约合并
    kline_5m/{YYYYMMDD}/{CODE}_{date}.csv — 分钟线，按日分目录
    kline_5m/{YYYYMMDD}.tar.gz            — 分钟线，按日压缩

支持增量读取: 只读取指定日期之后的新数据。
"""

import os
import io
import csv
import tarfile
import subprocess
import logging
from typing import Dict, List, Optional, Tuple, Generator
from dataclasses import dataclass, field
from collections import defaultdict

from config import (
    EXCHANGES, PERIOD_MAP, WTPY_SUPPORTED_PERIODS,
    OPTION_PRODUCTS, FUTURES_PRODUCTS,
)

logger = logging.getLogger(__name__)


@dataclass
class KSBarRecord:
    """KS K线数据记录"""
    date: int        # YYYYMMDD
    time: int        # HHMM (分钟线) 或 0 (日线)
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float    # turnover
    openinterest: float  # open_interest


@dataclass
class KSTickRecord:
    """KS Tick数据记录

    KS tick CSV字段(5档): tradingDay,timestamp,last,open,high,low,volume,amount,
        openinterest,buyprice1,buyvolume1,...,buyprice5,buyvolume5,
        sellprice1,selvolume1,...,sellprice5,selvolume5
    KS tick CSV字段(1档): tradingDay,timestamp,last,open,high,low,volume,amount,
        openinterest,buyprice1,buyvolume1,sellprice1,selvolume1

    timestamp: Unix毫秒时间戳
    volume/amount: 累计值(日内)
    """
    trading_day: int       # YYYYMMDD (tradingDay)
    timestamp_ms: int      # Unix毫秒时间戳
    last_price: float      # 最新价 (last)
    open: float
    high: float
    low: float
    volume: float          # 累计成交量
    amount: float          # 累计成交额
    openinterest: float    # 持仓量
    bid_prices: list = field(default_factory=list)   # [buyprice1, ...buyprice5]
    bid_volumes: list = field(default_factory=list)   # [buyvolume1, ...buyvolume5]
    ask_prices: list = field(default_factory=list)    # [sellprice1, ...sellprice5]
    ask_volumes: list = field(default_factory=list)   # [selvolume1, ...selvolume5]


@dataclass
class KSCodeData:
    """单个合约的K线数据集合"""
    exchange: str
    product: str
    code: str
    period: str       # kline_1d / kline_5m / kline_1m
    bars: List[KSBarRecord] = field(default_factory=list)

    def add_bar(self, bar: KSBarRecord):
        self.bars.append(bar)

    def sort_bars(self):
        """按日期(时间)排序"""
        self.bars.sort(key=lambda b: (b.date, b.time))

    def deduplicate(self):
        """去重（同一日期+时间只保留最新）"""
        seen = set()
        unique = []
        for bar in reversed(self.bars):
            key = (bar.date, bar.time)
            if key not in seen:
                seen.add(key)
                unique.append(bar)
        unique.reverse()
        self.bars = unique


def parse_ks_csv_line(line: str, is_day: bool = False) -> Optional[KSBarRecord]:
    """解析KS CSV单行数据

    KS格式: date,time,open,high,low,close,volume,amount,openinterest
    日线: date,00:00,open,high,low,close,volume,amount,openinterest
    """
    parts = line.strip().split(',')
    if len(parts) < 9:
        return None

    try:
        date_str = parts[0].strip()
        time_str = parts[1].strip()

        # 解析日期 YYYYMMDD
        date_val = int(date_str.replace('/', '').replace('-', ''))

        # 解析时间 HH:MM → HHMM
        time_parts = time_str.split(':')
        time_val = int(time_parts[0]) * 100 + int(time_parts[1]) if len(time_parts) >= 2 else 0

        return KSBarRecord(
            date=date_val,
            time=time_val,
            open=float(parts[2]),
            high=float(parts[3]),
            low=float(parts[4]),
            close=float(parts[5]),
            volume=float(parts[6]),
            amount=float(parts[7]),
            openinterest=float(parts[8]),
        )
    except (ValueError, IndexError) as e:
        logger.warning(f"解析KS CSV行失败: {line.strip()}, 错误: {e}")
        return None


def parse_ks_csv_content(content: str, is_day: bool = False) -> List[KSBarRecord]:
    """解析KS CSV文件内容"""
    bars = []
    lines = content.strip().split('\n')
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        # 跳过表头
        if i == 0 and 'date' in line.lower():
            continue
        bar = parse_ks_csv_line(line, is_day)
        if bar is not None:
            bars.append(bar)
    return bars


def extract_code_from_filename(filename: str, is_day: bool = False) -> Optional[str]:
    """从KS文件名提取合约代码

    日线: IF1810.csv → IF1810
    分钟线/Tick: IF2102_20210104.csv → IF2102
    """
    basename = os.path.splitext(filename)[0]
    if is_day:
        return basename
    else:
        # 分钟线/Tick: CODE_YYYYMMDD
        parts = basename.rsplit('_', 1)
        if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 8:
            return parts[0]
        return basename


def parse_ks_tick_csv_line(header_fields: List[str], parts: List[str]) -> Optional[KSTickRecord]:
    """解析KS tick CSV单行数据

    支持两种格式:
      5档(29字段): tradingDay,timestamp,last,open,high,low,volume,amount,openinterest,
                   buyprice1,buyvolume1,...,buyprice5,buyvolume5,
                   sellprice1,selvolume1,...,sellprice5,selvolume5
      1档(13字段): tradingDay,timestamp,last,open,high,low,volume,amount,openinterest,
                   buyprice1,buyvolume1,sellprice1,selvolume1
    """
    if len(parts) < 9:
        return None
    try:
        trading_day = int(parts[0])
        timestamp_ms = int(parts[1])
        last_price = float(parts[2])
        open_p = float(parts[3])
        high_p = float(parts[4])
        low_p = float(parts[5])
        vol = float(parts[6])
        amt = float(parts[7])
        oi = float(parts[8])

        # 解析买卖盘 (字段从 index 9 开始)
        bid_prices, bid_volumes = [], []
        ask_prices, ask_volumes = [], []

        # 从 header 推断档位数
        n_levels = 0
        for i in range(1, 11):
            if f'buyprice{i}' in header_fields:
                n_levels = i
            else:
                break

        for lvl in range(1, n_levels + 1):
            bp_key = f'buyprice{lvl}'
            bv_key = f'buyvolume{lvl}'
            sp_key = f'sellprice{lvl}'
            sv_key = f'selvolume{lvl}'

            bp_idx = header_fields.index(bp_key) if bp_key in header_fields else -1
            bv_idx = header_fields.index(bv_key) if bv_key in header_fields else -1
            sp_idx = header_fields.index(sp_key) if sp_key in header_fields else -1
            sv_idx = header_fields.index(sv_key) if sv_key in header_fields else -1

            bid_prices.append(float(parts[bp_idx]) if bp_idx >= 0 and bp_idx < len(parts) else 0.0)
            bid_volumes.append(float(parts[bv_idx]) if bv_idx >= 0 and bv_idx < len(parts) else 0.0)
            ask_prices.append(float(parts[sp_idx]) if sp_idx >= 0 and sp_idx < len(parts) else 0.0)
            ask_volumes.append(float(parts[sv_idx]) if sv_idx >= 0 and sv_idx < len(parts) else 0.0)

        return KSTickRecord(
            trading_day=trading_day,
            timestamp_ms=timestamp_ms,
            last_price=last_price,
            open=open_p,
            high=high_p,
            low=low_p,
            volume=vol,
            amount=amt,
            openinterest=oi,
            bid_prices=bid_prices,
            bid_volumes=bid_volumes,
            ask_prices=ask_prices,
            ask_volumes=ask_volumes,
        )
    except (ValueError, IndexError) as e:
        return None


def parse_ks_tick_csv_content(content: str) -> List[KSTickRecord]:
    """解析KS tick CSV文件内容

    自动识别1档/5档格式。返回按 timestamp_ms 排序的记录列表。
    """
    ticks = []
    lines = content.strip().split('\n')
    if not lines:
        return ticks

    # 解析表头
    header_line = lines[0].strip()
    header_fields = [f.strip() for f in header_line.split(',')]

    # 推断买卖盘档位数（提前检测，n_levels==0 时发出警告）
    n_levels_detected = 0
    for i in range(1, 11):
        if f'buyprice{i}' in header_fields:
            n_levels_detected = i
        else:
            break
    if n_levels_detected == 0:
        logger.warning(
            "parse_ks_tick_csv_content: 无法识别买卖盘字段 (n_levels=0)，"
            "买卖盘将全部填0。header前10个字段: %s",
            header_fields[:10]
        )

    for i in range(1, len(lines)):
        line = lines[i].strip()
        if not line:
            continue
        parts = line.split(',')
        tick = parse_ks_tick_csv_line(header_fields, parts)
        if tick is not None:
            ticks.append(tick)

    # 按 timestamp 排序
    ticks.sort(key=lambda t: t.timestamp_ms)
    return ticks


class KSDataReader:
    """KS数据读取器 - 支持本地和远程读取"""

    def __init__(self, base_path: str, is_remote: bool = False,
                 remote_host: str = '', remote_user: str = 'root'):
        """
        @base_path  KS数据根目录
        @is_remote  是否远程服务器
        @remote_host 远程主机地址
        @remote_user 远程用户名
        """
        self.base_path = base_path.rstrip('/')
        self.is_remote = is_remote
        self.remote_host = remote_host
        self.remote_user = remote_user

    def _ssh_cmd(self, cmd: str) -> str:
        """执行远程SSH命令"""
        full_cmd = f"ssh {self.remote_user}@{self.remote_host} \"{cmd}\""
        result = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            logger.warning(f"SSH命令失败: {cmd}, stderr: {result.stderr}")
            return ''
        return result.stdout.strip()

    def _ssh_cat(self, remote_path: str) -> Optional[str]:
        """通过SSH读取远程文件内容"""
        full_cmd = f"ssh {self.remote_user}@{self.remote_host} \"cat {remote_path}\""
        result = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return None
        return result.stdout

    def _ssh_tar_list(self, remote_tar_path: str) -> List[str]:
        """列出远程tar.gz中的文件"""
        full_cmd = f"ssh {self.remote_user}@{self.remote_host} \"tar tzf {remote_tar_path}\""
        result = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return []
        return [f.strip() for f in result.stdout.strip().split('\n') if f.strip()]

    def _ssh_tar_extract_csv(self, remote_tar_path: str, csv_name: str) -> Optional[str]:
        """从远程tar.gz中提取指定CSV文件内容"""
        # 使用tar + ssh管道提取
        full_cmd = (
            f"ssh {self.remote_user}@{self.remote_host} "
            f"\"tar xzf {remote_tar_path} -O {csv_name}\""
        )
        result = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return None
        return result.stdout

    def _local_read_file(self, path: str) -> Optional[str]:
        """读取本地文件"""
        try:
            with open(path, 'r', encoding='utf-8-sig') as f:
                return f.read()
        except Exception as e:
            logger.warning(f"读取文件失败: {path}, 错误: {e}")
            return None

    def _local_tar_extract_csv(self, tar_path: str, csv_name: str) -> Optional[str]:
        """从本地tar.gz中提取指定CSV文件内容"""
        try:
            with tarfile.open(tar_path, 'r:gz') as tar:
                member = tar.getmember(csv_name)
                f = tar.extractfile(member)
                if f:
                    return f.read().decode('utf-8-sig')
        except Exception as e:
            logger.warning(f"从tar.gz提取失败: {tar_path}/{csv_name}, 错误: {e}")
        return None

    def list_exchanges(self) -> List[str]:
        """列出所有交易所"""
        if self.is_remote:
            output = self._ssh_cmd(f"ls {self.base_path}/")
            return [e for e in output.split('\n') if e.strip() and e.strip() in EXCHANGES]
        else:
            if not os.path.exists(self.base_path):
                return []
            return [e for e in os.listdir(self.base_path)
                    if os.path.isdir(os.path.join(self.base_path, e)) and e in EXCHANGES]

    def list_products(self, exchange: str) -> List[str]:
        """列出交易所下的所有品种"""
        exchg_path = f"{self.base_path}/{exchange}"
        if self.is_remote:
            output = self._ssh_cmd(f"ls {exchg_path}/")
            return [p for p in output.split('\n') if p.strip()]
        else:
            if not os.path.exists(exchg_path):
                return []
            return [p for p in os.listdir(exchg_path)
                    if os.path.isdir(os.path.join(exchg_path, p))]

    def list_date_dirs(self, exchange: str, product: str, period: str) -> List[int]:
        """列出分钟线的日期目录（YYYYMMDD格式）

        返回排序后的日期列表
        """
        period_path = f"{self.base_path}/{exchange}/{product}/L1/{period}"
        is_day = (period == 'kline_1d')

        if is_day:
            # 日线没有日期目录，直接返回空（用文件列表代替）
            return []

        def _date_from_item(item: str) -> Optional[int]:
            item = item.strip()
            if item.endswith('.part.minio'):
                return None
            if item.endswith('.tar.gz'):
                item = item[:-7]
            if item.isdigit() and len(item) == 8:
                return int(item)
            return None

        if self.is_remote:
            output = self._ssh_cmd(f"ls {period_path}/")
            dates = []
            for item in output.split('\n'):
                date = _date_from_item(item)
                if date is not None:
                    dates.append(date)
            return sorted(set(dates))
        else:
            if not os.path.exists(period_path):
                return []
            dates = []
            for item in os.listdir(period_path):
                full_path = os.path.join(period_path, item)
                date = _date_from_item(item)
                if date is None:
                    continue
                if os.path.isdir(full_path) or item.endswith('.tar.gz'):
                    dates.append(date)
            return sorted(set(dates))

    def read_day_csv(self, exchange: str, product: str, code: str) -> List[KSBarRecord]:
        """读取日线CSV（按合约合并的单文件）

        @code 合约代码如 IF1810
        """
        csv_path = f"{self.base_path}/{exchange}/{product}/L1/kline_1d/kline_1d/{code}.csv"

        if self.is_remote:
            content = self._ssh_cat(csv_path)
        else:
            content = self._local_read_file(csv_path)

        if content is None:
            return []

        return parse_ks_csv_content(content, is_day=True)

    def read_minute_csv(self, exchange: str, product: str, period: str,
                        date: int, code: str = None) -> List[KSBarRecord]:
        """读取分钟线CSV（按日分目录）

        @date  YYYYMMDD
        @code  合约代码，为None时读取该日所有合约
        """
        date_str = str(date)
        period_path = f"{self.base_path}/{exchange}/{product}/L1/{period}/{date_str}"

        all_bars = []

        if self.is_remote:
            # 优先尝试直接CSV文件
            if code:
                csv_name = f"{code}_{date_str}.csv"
                csv_path = f"{period_path}/{csv_name}"
                content = self._ssh_cat(csv_path)
                if content:
                    bars = parse_ks_csv_content(content, is_day=False)
                    for bar in bars:
                        # 确保日期正确
                        if bar.date != date:
                            bar.date = date
                    all_bars.extend(bars)
                    return all_bars

            # 尝试tar.gz
            tar_path = f"{period_path}.tar.gz"
            tar_files = self._ssh_tar_list(tar_path)
            for tf in tar_files:
                if not tf.endswith('.csv'):
                    continue
                if code and not tf.startswith(code):
                    continue
                content = self._ssh_tar_extract_csv(tar_path, tf)
                if content:
                    bars = parse_ks_csv_content(content, is_day=False)
                    for bar in bars:
                        if bar.date != date:
                            bar.date = date
                    all_bars.extend(bars)
        else:
            # 本地读取
            if os.path.exists(period_path):
                for fname in os.listdir(period_path):
                    if not fname.endswith('.csv'):
                        continue
                    if code and not fname.startswith(code):
                        continue
                    fpath = os.path.join(period_path, fname)
                    content = self._local_read_file(fpath)
                    if content:
                        bars = parse_ks_csv_content(content, is_day=False)
                        for bar in bars:
                            if bar.date != date:
                                bar.date = date
                        all_bars.extend(bars)
            else:
                # 尝试tar.gz
                tar_path = f"{period_path}.tar.gz"
                if os.path.exists(tar_path):
                    try:
                        with tarfile.open(tar_path, 'r:gz') as tar:
                            for member in tar.getmembers():
                                if not member.name.endswith('.csv'):
                                    continue
                                if code and not os.path.basename(member.name).startswith(code):
                                    continue
                                f = tar.extractfile(member)
                                if f:
                                    content = f.read().decode('utf-8-sig')
                                    bars = parse_ks_csv_content(content, is_day=False)
                                    for bar in bars:
                                        if bar.date != date:
                                            bar.date = date
                                    all_bars.extend(bars)
                    except Exception as e:
                        logger.warning(f"读取tar.gz失败: {tar_path}, 错误: {e}")

        return all_bars

    def read_all_codes_day(self, exchange: str, product: str) -> Dict[str, List[KSBarRecord]]:
        """读取某品种所有合约的日线数据

        返回: {合约代码: [KSBarRecord, ...]}
        """
        day_dir = f"{self.base_path}/{exchange}/{product}/L1/kline_1d/kline_1d"
        result = {}

        if self.is_remote:
            output = self._ssh_cmd(f"ls {day_dir}/")
            for line in output.split('\n'):
                fname = line.strip()
                if not fname.endswith('.csv'):
                    continue
                code = extract_code_from_filename(fname, is_day=True)
                if code is None:
                    continue
                bars = self.read_day_csv(exchange, product, code)
                if bars:
                    result[code] = bars
                    logger.debug(f"读取日线 {exchange}.{product}.{code}: {len(bars)}条")
        else:
            if not os.path.exists(day_dir):
                return result
            for fname in os.listdir(day_dir):
                if not fname.endswith('.csv'):
                    continue
                code = extract_code_from_filename(fname, is_day=True)
                if code is None:
                    continue
                bars = self.read_day_csv(exchange, product, code)
                if bars:
                    result[code] = bars
                    logger.debug(f"读取日线 {exchange}.{product}.{code}: {len(bars)}条")

        return result

    def read_date_minute(self, exchange: str, product: str, period: str,
                         date: int) -> Dict[str, List[KSBarRecord]]:
        """读取某品种某日所有合约的分钟线数据

        返回: {合约代码: [KSBarRecord, ...]}
        """
        bars = self.read_minute_csv(exchange, product, period, date)
        result = defaultdict(list)
        for bar in bars:
            # 从文件名推断合约代码需要更复杂的逻辑
            # 这里先按日期分组，合约代码在外部处理
            pass

        # 更好的方式：逐文件读取
        date_str = str(date)
        period_path = f"{self.base_path}/{exchange}/{product}/L1/{period}/{date_str}"

        if self.is_remote:
            # 列出日期目录下的CSV文件
            output = self._ssh_cmd(f"ls {period_path}/ 2>/dev/null")
            csv_files = [f.strip() for f in output.split('\n') if f.strip().endswith('.csv')]

            if not csv_files:
                # 尝试tar.gz
                tar_path = f"{period_path}.tar.gz"
                tar_files = self._ssh_tar_list(tar_path)
                csv_files = [f for f in tar_files if f.endswith('.csv')]

                for tf in csv_files:
                    code = extract_code_from_filename(os.path.basename(tf), is_day=False)
                    if code is None:
                        continue
                    content = self._ssh_tar_extract_csv(tar_path, tf)
                    if content:
                        parsed = parse_ks_csv_content(content, is_day=False)
                        for bar in parsed:
                            if bar.date != date:
                                bar.date = date
                        if parsed:
                            result[code].extend(parsed)
            else:
                for fname in csv_files:
                    code = extract_code_from_filename(fname, is_day=False)
                    if code is None:
                        continue
                    content = self._ssh_cat(f"{period_path}/{fname}")
                    if content:
                        parsed = parse_ks_csv_content(content, is_day=False)
                        for bar in parsed:
                            if bar.date != date:
                                bar.date = date
                        if parsed:
                            result[code].extend(parsed)
        else:
            if os.path.exists(period_path):
                for fname in os.listdir(period_path):
                    if not fname.endswith('.csv'):
                        continue
                    code = extract_code_from_filename(fname, is_day=False)
                    if code is None:
                        continue
                    fpath = os.path.join(period_path, fname)
                    content = self._local_read_file(fpath)
                    if content:
                        parsed = parse_ks_csv_content(content, is_day=False)
                        for bar in parsed:
                            if bar.date != date:
                                bar.date = date
                        if parsed:
                            result[code].extend(parsed)
            else:
                tar_path = f"{period_path}.tar.gz"
                if os.path.exists(tar_path):
                    try:
                        with tarfile.open(tar_path, 'r:gz') as tar:
                            for member in tar.getmembers():
                                if not member.name.endswith('.csv'):
                                    continue
                                code = extract_code_from_filename(
                                    os.path.basename(member.name), is_day=False)
                                if code is None:
                                    continue
                                f = tar.extractfile(member)
                                if f:
                                    content = f.read().decode('utf-8-sig')
                                    parsed = parse_ks_csv_content(content, is_day=False)
                                    for bar in parsed:
                                        if bar.date != date:
                                            bar.date = date
                                    if parsed:
                                        result[code].extend(parsed)
                    except Exception as e:
                        logger.warning(f"读取tar.gz失败: {tar_path}, 错误: {e}")

        return dict(result)

    def list_day_codes(self, exchange: str, product: str) -> List[str]:
        """列出日线目录下的所有合约代码"""
        day_dir = f"{self.base_path}/{exchange}/{product}/L1/kline_1d/kline_1d"

        if self.is_remote:
            output = self._ssh_cmd(f"ls {day_dir}/ 2>/dev/null")
            codes = []
            for line in output.split('\n'):
                fname = line.strip()
                if not fname.endswith('.csv'):
                    continue
                code = extract_code_from_filename(fname, is_day=True)
                if code:
                    codes.append(code)
            return sorted(codes)
        else:
            if not os.path.exists(day_dir):
                return []
            codes = []
            for fname in os.listdir(day_dir):
                if not fname.endswith('.csv'):
                    continue
                code = extract_code_from_filename(fname, is_day=True)
                if code:
                    codes.append(code)
            return sorted(codes)

    def is_option_code(self, exchange: str, product: str, code: str) -> bool:
        """判断是否为期权合约代码

        ⚠️ 经 2026-05-29 真实 S3 验证后修正的判定规则
        （与 s3_reader.S3KsReader.is_option_code 保持一致）：

        独立期权 product 目录（整目录均为期权数据）：
        - CFFEX: IO / HO / MO

        混装/共享 product 目录（必须按合约代码模式过滤）：
        - SHFE.op:  双胶纸期货品种(op<YYMM>) + 双胶纸期权(op<YYMM>{C|P}<strike>) 共处
        - SHFE 其它/CZCE/DCE/INE 标的品种: 期权与期货共享 product 目录

        合约代码模式：
          (a) '-C-' / '-P-'（如 IO2509-C-4250、m2609-P-2950）
          (b) <数字><C|P><数字{3+}>（如 AP610C7000、op2601C3900）

        OPTION_PRODUCTS 列表仅用于文档/标识，不参与判定，以免把 AP605/IC1505 等
        含 C/P 字母的期货合约误判为期权。
        """
        import re

        # 仅 CFFEX(IO/HO/MO) 是真正的独立纯期权 product 目录
        if exchange == 'CFFEX' and product in ('IO', 'HO', 'MO'):
            return True
        # 其余全部按合约代码模式判定（包括 SHFE.op 混装目录）
        if '-C-' in code or '-P-' in code:
            return True
        if re.search(r'\d+[CP]\d{3,}', code):
            return True
        return False
