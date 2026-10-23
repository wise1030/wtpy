"""m5 → h1 聚合 (2026-10-21 h1 一期, 纯逻辑零框架依赖)

背景: WtDt 数据底盘只从 tick 合成 min1/min5/day, **不写 h1**; his DSB
每小时重建对运行中进程不可见 (WtDataReader 缓存无失效 API, 9/21 事故
教训)。故 h1 在**策略层从 m5 聚合** —— backtest/live 同源同函数,
天然保证口径一致 (与 _session_eval_daily 的 d1-from-m5 合成同一哲学)。

桶口径 (backtest 与 live 必须一致, 改动需双向回归):
- **归属交易日**: time>=200000 (夜盘) 的 bar 日历日 D 属于下一交易日
  (trading_days 提供交易日历); 其余归属当日。post-midnight bar
  (00:05-02:30) 日历日即交易日, 无需映射。
- **桶 label = bar 起始时刻的钟点 ×100**: m5 bar 的 time 是**收盘时刻**
  (10:00 的 bar 覆盖 09:55-10:00), 用起始时刻保证 "10:00 bar 归属
  09 点桶", 即桶 [09:05..10:00] 恰为完整 09:00-10:00 小时。整点
  bar (time%10000==0) 钟点减一; 00:00 兜底归 23 点 (不存在, 防御)。
- **小节中断自然并入钟点桶**: 10:35-11:00 的 bar 起始钟点都是 10
  (11:00 bar 起始 10:55), 与 10:05-10:15 同属 10 点桶。
- **收盘语义**: 桶在其钟点小时的末根 m5 到达后闭合 —— 聚合输出里
  除尾部桶外全部闭合; 尾部桶由会话末 flush (实盘 session_end /
  bt on_session_end) 强制闭合评估, 与 d1 的 F3 处置同构。

aggregate_h1 全量无状态重建 (每次 on_calculate 从滑动 m5 窗口重建,
几千根聚合开销可忽略), 评估锚点 (_h1_key) 由 StraTurtle 持久化,
重启重放不会二次消费。
"""
import bisect


def norm_m5_time(t: int) -> int:
    """WT m5 bar 时间三态归一 → HHMMSS (h1_bucket 的输入口径)。

    - ≥1e6: WT 打包日期时间 `(年-1990)*1e8 + 月*1e6 + 日*1e4 + HHMM`
      (bt DSB / live rt 实测, 如 2904082125 = 2019-04-08 21:25)
      → 取 %10000 得 HHMM, ×100;
    - <1e4: 裸 HHMM (部分 csv/工具链) → ×100;
    - 其余: 已是 HHMMSS, 原样。
    """
    t = int(t)
    if t >= 1000000:
        return (t % 10000) * 100
    if t < 10000:
        return t * 100
    return t


def h1_bucket(date: int, time: int, trading_days) -> tuple:
    """m5 bar → (tdate, label)。trading_days: 升序交易日历 (int yyyymmdd)"""
    tdate = int(date)
    if time >= 200000:
        i = bisect.bisect_right(trading_days, int(date))
        if i < len(trading_days):
            tdate = trading_days[i]
    hh = time // 10000
    if time % 10000 == 0:
        hh -= 1                    # 整点 bar 起始在上一小时
    if hh < 0:
        hh = 23                    # 00:00 防御 (不存在, 归前夜 23 点桶)
    return tdate, hh * 100


def aggregate_h1(rows, trading_days) -> dict:
    """m5 rows → h1 序列 (含尾部未完成桶)。

    rows: [(date, time, o, h, l, c, vol, oi), ...] 按 (date,time) 升序。
    返回 dict(dates, times, opens, highs, lows, closes, volumes, ois,
              keys, n_closed):
      keys[i]  = tdate*10000 + label   (评估锚点, 单调升序)
      n_closed = 确定已闭合的桶数 (除尾部桶外全部闭合; 尾部桶数量<=1)
    """
    buckets = {}        # (tdate, label) → row (保序用列表)
    order = []
    for (d, t, o, h, l, c, v, oi) in rows:
        key = h1_bucket(d, t, trading_days)
        b = buckets.get(key)
        if b is None:
            buckets[key] = {"date": key[0], "time": key[1],
                            "open": float(o), "high": float(h),
                            "low": float(l), "close": float(c),
                            "volume": float(v), "open_interest": float(oi)}
            order.append(key)
        else:
            b["high"] = max(b["high"], float(h))
            b["low"] = min(b["low"], float(l))
            b["close"] = float(c)          # 末根收盘
            b["volume"] += float(v)
            b["open_interest"] = float(oi)  # OI 取最新
    n = len(order)
    n_closed = max(0, n - 1)               # 尾部桶可能未完成
    # bar → 桶映射 (并行于输入 rows); bucket_last_bar[j] = 桶 j 的末根
    # m5 bar 下标 (闭合判定的唯一依据: 末根到达即闭合; 尾部桶除外)
    bar_bucket = []
    for (d, t, _o, _h, _l, _c, _v, _oi) in rows:
        bar_bucket.append(order.index(h1_bucket(d, t, trading_days)))
    bucket_last_bar = [-1] * n
    for i, b in enumerate(bar_bucket):
        bucket_last_bar[b] = i
    out = {"dates": [], "times": [], "opens": [], "highs": [], "lows": [],
           "closes": [], "volumes": [], "ois": [], "keys": [],
           "bar_bucket": bar_bucket,
           "bucket_last_bar": bucket_last_bar,
           "n_closed": n_closed}
    for key in order:
        b = buckets[key]
        out["dates"].append(b["date"])
        out["times"].append(b["time"])
        out["opens"].append(b["open"])
        out["highs"].append(b["high"])
        out["lows"].append(b["low"])
        out["closes"].append(b["close"])
        out["volumes"].append(b["volume"])
        out["ois"].append(b["open_interest"])
        out["keys"].append(key[0] * 10000 + key[1])
    return out
