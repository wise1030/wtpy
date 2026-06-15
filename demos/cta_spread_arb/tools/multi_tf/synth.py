"""
multi_tf.synth — 1m K线 → 多周期 resample 工具(离线分析专用)

⚠️ 仅离线特征分析使用,严禁用于回测信号生成。
   回测仍走 KS S3 原生 kline_1m/kline_5m/kline_1d。

输入: ks2wtpy CSV 输出 (kline_1m)
     列: date,time,open,high,low,close,volume,turnover,settle,diff_settle,open_interest,diff_interest
     date=YYYYMMDD(int), time=HHMM(int, 0-2359)
输出: dict[tf -> pd.DataFrame] 按 trading_date 重采样

设计要点:
1. 按 trading_date(夜盘归属次日) 分组 resample,避免跨日合成
2. TF 支持 {m5, m15, m30, h1, h2}
3. 不补 NaN bar(不交易时段直接跳过)
4. 输出附加 trading_date 列便于后续按日分析
"""
from __future__ import annotations
import pandas as pd
import numpy as np
from pathlib import Path
from typing import Dict, List, Optional


# pandas resample rule 映射
TF_RULES = {
    'm5':  '5T',
    'm15': '15T',
    'm30': '30T',
    'h1':  '60T',
    'h2':  '120T',
}


def load_ks_1m_csv(csv_path: str | Path) -> pd.DataFrame:
    """加载 ks2wtpy 导出的 1m CSV (实际 schema: date=YYYY/MM/DD, time=HH:MM:SS)"""
    df = pd.read_csv(csv_path, encoding='utf-8-sig')  # 处理 BOM
    df['dt'] = pd.to_datetime(
        df['date'].astype(str) + ' ' + df['time'].astype(str),
        errors='coerce'
    )
    df = df.dropna(subset=['dt']).sort_values('dt').reset_index(drop=True)
    return df


def infer_trading_date(dt: pd.Series) -> pd.Series:
    """
    推断交易日(夜盘归属次日)
    简化规则: hour >= 21 → 次日; hour < 5 → 当日(凌晨延续昨夜); 其它 → 当日
    精确版需要节假日表,这里足够离线分析用
    """
    td = dt.dt.date.copy()
    night_mask = dt.dt.hour >= 21
    td[night_mask] = (dt[night_mask] + pd.Timedelta(days=1)).dt.date
    return pd.to_datetime(td)


def resample_to_tf(df_1m: pd.DataFrame, tf: str) -> pd.DataFrame:
    """
    按 trading_date 分组 resample 到目标 TF
    避免跨日(夜盘 23:00 与次日 09:00 之间的空白被插值)
    """
    if tf not in TF_RULES:
        raise ValueError(f"Unsupported TF: {tf}, supported: {list(TF_RULES.keys())}")

    rule = TF_RULES[tf]
    df = df_1m.copy()
    df['trading_date'] = infer_trading_date(df['dt'])
    df = df.set_index('dt')

    agg = {
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last',
        'volume': 'sum', 'turnover': 'sum',
        'open_interest': 'last', 'diff_interest': 'sum',
    }
    # 只保留聚合需要的列
    use_cols = [c for c in agg if c in df.columns]
    agg = {c: agg[c] for c in use_cols}

    out_chunks = []
    for td, grp in df.groupby('trading_date'):
        rs = grp[use_cols].resample(rule, label='right', closed='right').agg(agg)
        rs = rs.dropna(subset=['close'])  # 不交易时段直接丢
        if rs.empty:
            continue
        rs['trading_date'] = td
        out_chunks.append(rs)

    if not out_chunks:
        return pd.DataFrame()
    out = pd.concat(out_chunks).reset_index().rename(columns={'dt': 'datetime'})
    return out


def synth_all_tfs(csv_path: str | Path,
                  tfs: Optional[List[str]] = None) -> Dict[str, pd.DataFrame]:
    """一次性合成所有目标 TF"""
    tfs = tfs or list(TF_RULES.keys())
    df_1m = load_ks_1m_csv(csv_path)
    return {tf: resample_to_tf(df_1m, tf) for tf in tfs}


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print("Usage: python synth.py <1m_csv_path> [tf]")
        sys.exit(1)
    path = sys.argv[1]
    tfs = [sys.argv[2]] if len(sys.argv) > 2 else None
    out = synth_all_tfs(path, tfs)
    for tf, df in out.items():
        print(f"=== {tf} === rows={len(df)} dates={df['trading_date'].nunique() if not df.empty else 0}")
        if not df.empty:
            print(df.head(3).to_string())
            print('...')
            print(df.tail(2).to_string())
