#!/usr/bin/env python3
"""
KS → WonderTrader 标准化语义工具。

本模块只做无副作用的字段语义转换，供 CSV/LMDB/DSB/manifest 复用，避免各 writer
各自实现一套 date/time/stdCode 规则导致漂移。
"""

from typing import Any, Dict


def std_code(exchange: str, code: str) -> str:
    """WonderTrader 标准合约代码: EXCHG.CODE。"""
    return f"{exchange}.{code}"


def minute_bar_time(date: int, time: int) -> int:
    """WonderTrader 分钟线 bar time: (YYYYMMDD - 19900000) * 10000 + HHMM。"""
    return (int(date) - 19900000) * 10000 + int(time)


def wt_day_time(date: int) -> int:
    """WonderTrader 日线 bar time: YYYYMMDD。"""
    return int(date)


def bar_time_key(bar: Any, is_day: bool = False) -> int:
    """从 KSBarRecord-like 对象计算 WT bar time。"""
    if is_day:
        return wt_day_time(bar.date)
    return minute_bar_time(bar.date, bar.time)


def normalize_bar(exchange: str, code: str, bar: Any, is_day: bool = False) -> Dict[str, Any]:
    """返回用于审计/验证的标准化 bar 摘要，不修改原始 bar。"""
    return {
        "std_code": std_code(exchange, code),
        "date": int(bar.date),
        "time": int(getattr(bar, "time", 0)),
        "bar_time": bar_time_key(bar, is_day=is_day),
        "open": float(bar.open),
        "high": float(bar.high),
        "low": float(bar.low),
        "close": float(bar.close),
        "volume": float(bar.volume),
        "turnover": float(bar.amount),
        "open_interest": float(bar.openinterest),
    }
