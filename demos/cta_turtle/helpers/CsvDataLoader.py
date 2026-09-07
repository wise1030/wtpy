"""CSV 数据加载器 — 从 storage/csv/ 读取 CSV K 线喂给 C++ 引擎

用途: wtpy 实盘引擎的 libWtDataStorage.so 读取 DSB 时存在 segfault bug,
通过 BaseExtDataLoader 绕过, 直接从 CSV 读取历史数据。
纯 csv 模块实现 (避免 pandas 在 C++ 回调线程中的 GIL/segfault 问题)。

CSV 文件名格式: {EXCHG}.{product}.HOT_{period}.csv (如 SHFE.rb.HOT_d.csv)
CSV 列: 日线 date,open,high,low,close,volume,turnover,open_interest
        分钟线可带 time 列 (date,time,open,...; 'HH:MM:SS' 或 HHMMSS 整数)

his/rt 交易日分区律 (DATA_OPS.md §0 F5): csv 属 his 侧, 当日 bar 由 rt 提供,
暖机加载必须丢弃 >= 当日交易日的行 (stop_tdate), 否则与 rt 重复。
"""
import csv
import datetime
import os

from wtpy.ExtModuleDefs import BaseExtDataLoader
from wtpy.WtCoreDefs import WTSBarStruct

_PERIOD_MAP = {
    "d1": "d", "d": "d",
    "m1": "m1", "m5": "m5",
}


def _parse_hhmm(s: str) -> int:
    """'09:05:00' / '90500' / '090500' → 905 (HHMM); 非法 → -1"""
    s = s.strip().strip('"')
    if ":" in s:
        parts = s.split(":")
        try:
            hh, mm = int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            return -1
    elif s.isdigit():
        v = int(s)
        if v >= 10000:          # HHMMSS → 取 HHMM
            hh, mm = v // 10000, v // 100 % 100
        else:                   # 已是 HHMM
            hh, mm = v // 100, v % 100
    else:
        return -1
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return -1
    return hh * 100 + mm


def parse_csv_rows(csv_path: str, suffix: str,
                   stop_tdate: int = 0) -> list:
    """纯函数: CSV → [(date, time_int, o,h,l,c, vol,turnover,oi), ...]

    suffix='d' 恒用收盘时间 1500; 分钟后缀带 time 列时解析为 HHMM。
    stop_tdate>0 丢弃 >= 该交易日的行 (his/rt 分区律)。
    无 wtpy 依赖, 可脱离 C++ 环境单测。
    """
    is_min = suffix in ("m1", "m5")
    rows = []
    with open(csv_path, encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        fields = [c.strip().lower() for c in (reader.fieldnames or [])]
        has_time = "time" in fields
        for r in reader:
            try:
                dt = datetime.datetime.strptime(r['date'].strip(),
                                                '%Y/%m/%d')
                date_int = int(dt.strftime('%Y%m%d'))
            except ValueError:
                try:
                    date_int = int(r['date'].strip().replace('-', ''))
                except ValueError:
                    continue
            if stop_tdate and date_int >= stop_tdate:
                continue                      # his/rt 分区: 滤掉当日行
            hhmm = 1500                       # 日线收盘时间 (默认口径)
            if is_min and has_time:
                hhmm = _parse_hhmm(r.get('time', ''))
                if hhmm < 0:
                    continue
            time_int = (date_int - 19900000) * 10000 + hhmm
            try:
                rows.append((
                    date_int,
                    time_int,
                    float(r['open']),
                    float(r['high']),
                    float(r['low']),
                    float(r['close']),
                    float(r.get('volume', 0) or 0),
                    float(r.get('turnover', 0) or 0),
                    float(r.get('open_interest', 0) or 0),
                ))
            except (ValueError, TypeError):
                continue
    return rows


class CsvDataLoader(BaseExtDataLoader):
    def __init__(self, csv_dir: str, stop_tdate: int = 0):
        self._csv_dir = csv_dir
        # >0: 丢弃 >= 该交易日(yyyymmdd)的行 — csv 暖机不含当日, 当日走 rt
        self._stop = int(stop_tdate or 0)

    def load_final_his_bars(self, stdCode: str, period: str, feeder) -> bool:
        suffix = _PERIOD_MAP.get(period)
        if suffix is None:
            return False

        csv_name = "%s_%s.csv" % (stdCode, suffix)
        csv_path = os.path.join(self._csv_dir, csv_name)
        if not os.path.exists(csv_path):
            return False

        rows = parse_csv_rows(csv_path, suffix, stop_tdate=self._stop)
        if not rows:
            return False

        BUFFER = WTSBarStruct * len(rows)
        buffer = BUFFER()
        for i, (dt, tm, o, h, l, c, vol, turnover, oi) in enumerate(rows):
            bar = buffer[i]
            bar.date = dt
            bar.time = tm
            bar.open = o
            bar.high = h
            bar.low = l
            bar.close = c
            # 注意: 结构体字段名为 vol/money/hold (非 volume/turnover/oi),
            # 误用会被 ctypes 吞成 Python 实例属性 → C++ 侧恒 0
            bar.vol = vol
            bar.money = turnover
            bar.hold = oi

        from ctypes import cast, POINTER as _POINTER
        feeder(cast(buffer, _POINTER(WTSBarStruct)), len(rows))
        return True

    def load_raw_his_bars(self, stdCode: str, period: str, feeder) -> bool:
        return self.load_final_his_bars(stdCode, period, feeder)

    def load_his_ticks(self, stdCode: str, uDate: int, feeder) -> bool:
        return False
