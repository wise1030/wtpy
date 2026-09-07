#!/usr/bin/env python3
"""CSV → 新格式 DSB 转换脚本 (方案B)

用 WtDataHelper.store_bars 把 CSV K 线转成 wtpy v0.9.10 兼容的 DSB。
输出到 his/{period}/{exchg}/ 目录，文件名 {exchg}.{product}_HOT.dsb。

用法:
  python3 convert_csv_to_dsb.py --csv-dir ./storage/csv --storage-dir ./storage
  python3 convert_csv_to_dsb.py ... --stop-tdate 20260831  # 滤掉>=当日行 (his/rt 分区律)
"""
import argparse
import datetime
import os

# wtpy 相关导入下沉到函数内: 模块保持纯逻辑可导入 (CI 无 C++ 库可测)

# CSV 列: 日线 date,open,... ; 分钟线可带 time 列 ('HH:MM:SS' 或 HHMMSS)
# 文件名: {EXCHG}.{product}.HOT_{period}.csv, period 后缀 d/m5/m1


def parse_hhmm(s: str) -> int:
    """'09:05:00' / '90500' → 905 (HHMM); 非法 → -1"""
    s = s.strip().strip('"')
    if ":" in s:
        parts = s.split(":")
        try:
            hh, mm = int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            return -1
    elif s.isdigit():
        v = int(s)
        if v >= 10000:
            hh, mm = v // 10000, v // 100 % 100
        else:
            hh, mm = v // 100, v % 100
    else:
        return -1
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return -1
    return hh * 100 + mm


def parse_csv_bar_date(ds: str) -> int:
    """CSV date 字符串 → yyyymmdd int"""
    ds = ds.strip()
    for fmt in ('%Y/%m/%d', '%Y-%m-%d', '%Y%m%d'):
        try:
            dt = datetime.datetime.strptime(ds, fmt)
            return int(dt.strftime('%Y%m%d'))
        except ValueError:
            continue
    raise ValueError(f"无法解析日期: {ds}")


def iter_csv_rows(csv_path: str, stop_tdate: int = 0,
                  is_min: bool = False) -> list:
    """纯函数: CSV → [(date, time_int, o,h,l,c, vol,turnover,oi), ...]

    stop_tdate>0 时丢弃 >= 该交易日的行 (his/rt 交易日分区律);
    分钟线带 time 列时解析为 HHMM, 否则回落收盘时间 1500。
    无 wtpy 依赖, 可脱离 C++ 环境单测。
    """
    rows = []
    with open(csv_path, encoding='utf-8-sig') as f:
        lines = f.readlines()
    if len(lines) < 2:
        return rows

    header = lines[0].strip().replace('\r', '').split(',')
    # 映射列名 (处理可能的空格/大小写差异)
    col_map = {}
    for i, col in enumerate(header):
        c = col.strip().lower()
        if c in ('date', '日期'):
            col_map['date'] = i
        elif c in ('time', '时间'):
            col_map['time'] = i
        elif c in ('open', '开盘'):
            col_map['open'] = i
        elif c in ('high', '最高'):
            col_map['high'] = i
        elif c in ('low', '最低'):
            col_map['low'] = i
        elif c in ('close', '收盘'):
            col_map['close'] = i
        elif c in ('volume', 'vol', '成交量'):
            col_map['volume'] = i
        elif c in ('turnover', '成交额'):
            col_map['turnover'] = i
        elif c in ('open_interest', 'oi', '持仓量'):
            col_map['oi'] = i

    for line in lines[1:]:
        parts = line.strip().replace('\r', '').split(',')
        if len(parts) < 5:
            continue
        try:
            date_int = parse_csv_bar_date(parts[col_map['date']])
            if stop_tdate and date_int >= stop_tdate:
                continue                  # his/rt 分区: 滤掉当日行
            hhmm = 1500
            if is_min and 'time' in col_map:
                hhmm = parse_hhmm(parts[col_map['time']])
                if hhmm < 0:
                    continue
            time_int = (date_int - 19900000) * 10000 + hhmm
            rows.append((
                date_int, time_int,
                float(parts[col_map['open']]),
                float(parts[col_map['high']]),
                float(parts[col_map['low']]),
                float(parts[col_map['close']]),
                float(parts[col_map.get('volume', -1)]) if 'volume' in col_map and parts[col_map['volume']] else 0.0,
                float(parts[col_map.get('turnover', -1)]) if 'turnover' in col_map and parts[col_map['turnover']] else 0.0,
                float(parts[col_map.get('oi', -1)]) if 'oi' in col_map and parts[col_map['oi']] else 0.0,
            ))
        except (ValueError, IndexError, KeyError):
            continue
    return rows


def load_csv_to_bars(csv_path: str, stop_tdate: int = 0,
                     is_min: bool = False):
    """读取 CSV → WTSBarStruct 数组 (POINTER) + count (需 wtpy 运行时)"""
    from ctypes import POINTER, cast
    from wtpy.WtCoreDefs import WTSBarStruct

    rows = iter_csv_rows(csv_path, stop_tdate=stop_tdate, is_min=is_min)
    n = len(rows)
    if n == 0:
        return None, 0

    BUFFER = WTSBarStruct * n
    buffer = BUFFER()
    for i in range(n):
        dt, tm, o, h, l, c, vol, turnover, oi = rows[i]
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

    ptr = cast(buffer, POINTER(WTSBarStruct))
    # 保持 buffer 引用防止 GC
    return (ptr, buffer), n


def convert(csv_dir: str, storage_dir: str, periods: list,
            stop_tdate: int = 0):
    from wtpy.wrapper.WtDtHelper import WtDataHelper
    helper = WtDataHelper()
    converted = 0

    for period in periods:
        period_suffix_map = {'d1': 'd', 'd': 'd', 'm5': 'm5', 'm1': 'm1'}
        csv_suffix = period_suffix_map.get(period, period)
        wt_period = csv_suffix  # store_bars 用 d/m5/m1

        # 扫描 CSV 目录
        csv_files = [f for f in os.listdir(csv_dir)
                     if f.endswith(f'_{csv_suffix}.csv')]

        for csv_file in sorted(csv_files):
            csv_path = os.path.join(csv_dir, csv_file)
            # 文件名: SHFE.rb.HOT_d.csv → stdCode=SHFE.rb.HOT, exchg=SHFE
            std_code = csv_file[:-(len(csv_suffix) + 5)]  # 去掉 _d.csv
            parts = std_code.split('.')
            if len(parts) < 2:
                continue
            exchg = parts[0]
            product = parts[1] if len(parts) > 1 else ''

            # 输出路径: storage/his/{period_dir}/{exchg}/{exchg}.{product}_HOT.dsb
            period_dir = {'d': 'day', 'm5': 'min5', 'm1': 'min1'}.get(wt_period, wt_period)
            out_dir = os.path.join(storage_dir, 'his', period_dir, exchg)
            os.makedirs(out_dir, exist_ok=True)

            # 文件名格式: {exchg}.{product}_HOT.dsb (WT 实盘引擎约定)
            if 'HOT' in std_code:
                out_name = f"{exchg}.{product}_HOT.dsb"
            else:
                out_name = f"{std_code.replace('.', '_')}.dsb"
            out_path = os.path.join(out_dir, out_name)

            (result), count = load_csv_to_bars(
                csv_path, stop_tdate=stop_tdate,
                is_min=(wt_period in ('m1', 'm5')))
            if result is None or count == 0:
                print(f"  SKIP {csv_file}: 0 bars")
                continue
            ptr, _buf = result

            ok = helper.store_bars(out_path, ptr, count, wt_period)
            if ok:
                print(f"  OK   {csv_file} → {out_path} ({count} bars)")
                converted += 1
            else:
                print(f"  FAIL {csv_file} → {out_path}")

    print(f"\n转换完成: {converted} 个文件")
    return converted


def main():
    ap = argparse.ArgumentParser(description='CSV → DSB 转换 (wtpy v0.9.10 兼容)')
    ap.add_argument('--csv-dir', type=str, required=True)
    ap.add_argument('--storage-dir', type=str, required=True)
    ap.add_argument('--periods', type=str, nargs='+', default=['d', 'm5'])
    ap.add_argument('--stop-tdate', type=int, default=0,
                    help='滤掉 >= 该交易日(yyyymmdd)的行 (his/rt 分区律, '
                         '默认当日: 传0关闭)')
    args = ap.parse_args()

    print(f"CSV 目录: {args.csv_dir}")
    print(f"Storage 目录: {args.storage_dir}")
    print(f"周期: {args.periods}")
    if args.stop_tdate:
        print(f"分区截断: >= {args.stop_tdate} 的行被丢弃\n")

    convert(args.csv_dir, args.storage_dir, args.periods,
            stop_tdate=args.stop_tdate)


if __name__ == '__main__':
    main()
