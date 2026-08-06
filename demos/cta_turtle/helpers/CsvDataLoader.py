"""CSV 数据加载器 — 从 storage/csv/ 读取 CSV K 线喂给 C++ 引擎

用途: wtpy 实盘引擎的 libWtDataStorage.so 读取 DSB 时存在 segfault bug,
通过 BaseExtDataLoader 绕过, 直接从 CSV 读取历史数据。
纯 csv 模块实现 (避免 pandas 在 C++ 回调线程中的 GIL/segfault 问题)。

CSV 文件名格式: {EXCHG}.{product}.HOT_{period}.csv (如 SHFE.rb.HOT_d.csv)
CSV 列: date,open,high,low,close,volume,turnover,open_interest
"""
import csv
import datetime
import os
from ctypes import POINTER

from wtpy.ExtModuleDefs import BaseExtDataLoader
from wtpy.WtCoreDefs import WTSBarStruct

_PERIOD_MAP = {
    "d1": "d", "d": "d",
    "m1": "m1", "m5": "m5",
}


class CsvDataLoader(BaseExtDataLoader):
    def __init__(self, csv_dir: str):
        self._csv_dir = csv_dir

    def load_final_his_bars(self, stdCode: str, period: str, feeder) -> bool:
        suffix = _PERIOD_MAP.get(period)
        if suffix is None:
            return False

        csv_name = "%s_%s.csv" % (stdCode, suffix)
        csv_path = os.path.join(self._csv_dir, csv_name)
        if not os.path.exists(csv_path):
            return False

        rows = []
        with open(csv_path, encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for r in reader:
                try:
                    dt = datetime.datetime.strptime(r['date'].strip(), '%Y/%m/%d')
                    date_int = int(dt.strftime('%Y%m%d'))
                except ValueError:
                    try:
                        date_int = int(r['date'].strip().replace('-', ''))
                    except ValueError:
                        continue
                time_int = (date_int - 19900000) * 10000 + 1500
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
            bar.volume = vol
            bar.turnover = turnover
            bar.open_interest = oi

        from ctypes import cast, POINTER as _POINTER
        feeder(cast(buffer, _POINTER(WTSBarStruct)), len(rows))
        return True

    def load_raw_his_bars(self, stdCode: str, period: str, feeder) -> bool:
        return self.load_final_his_bars(stdCode, period, feeder)

    def load_his_ticks(self, stdCode: str, uDate: int, feeder) -> bool:
        return False
