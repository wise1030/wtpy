import pandas as pd
import numpy as np

base = "./outputs_bt/spread_arb"

# ===== FUNDS =====
df = pd.read_csv(f"{base}/funds.csv")
init_capital = 500000
df["equity"] = init_capital + df["dynbalance"]
df["daily_return"] = df["equity"].pct_change()
total_return = (df["equity"].iloc[-1] / init_capital - 1) * 100
total_fee = df["fee"].iloc[-1]
sharpe = df["daily_return"].mean() / df["daily_return"].std() * np.sqrt(240) if df["daily_return"].std() > 0 else 0
df["peak"] = df["equity"].cummax()
df["drawdown"] = (df["equity"] - df["peak"]) / df["peak"]
max_dd = df["drawdown"].min() * 100
trading_days = len(df)

print(f"=== 资金曲线 ===")
print(f"初始资金: {init_capital:,.0f}")
print(f"期末权益: {df['equity'].iloc[-1]:,.2f}")
print(f"总收益率: {total_return:.2f}%")
print(f"总手续费: {total_fee:.2f}")
print(f"Sharpe:   {sharpe:.2f}")
print(f"最大回撤: {max_dd:.2f}%")
print(f"交易天数: {trading_days}")

# ===== CLOSES =====
closes = pd.read_csv(f"{base}/closes.csv")
print(f"\n=== 平仓记录 ===")
print(f"总平仓腿数: {len(closes)}")
print(closes.to_string())

# 按交易分组分析
if len(closes) > 0:
    results = []
    for code in closes["code"].unique():
        sub = closes[closes["code"] == code].copy().reset_index(drop=True)
        sub["trade_net"] = sub["totalprofit"].diff().fillna(sub["totalprofit"].iloc[0])
        results.append(sub)
    all_closes = pd.concat(results, ignore_index=True)
    trades = all_closes.groupby(["opentime", "closetime"]).agg(
        trade_pnl=("trade_net", "sum"),
        direction=("direct", "first")
    ).reset_index()
    
    n_trades = len(trades)
    win_rate = (trades["trade_pnl"] > 0).mean() * 100 if n_trades > 0 else 0
    avg_pnl = trades["trade_pnl"].mean() if n_trades > 0 else 0
    gross_profit = trades[trades["trade_pnl"] > 0]["trade_pnl"].sum()
    gross_loss = abs(trades[trades["trade_pnl"] <= 0]["trade_pnl"].sum())
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    
    print(f"\n=== 交易统计 ===")
    print(f"交易笔数: {n_trades}")
    print(f"胜率:     {win_rate:.1f}%")
    print(f"平均盈亏: {avg_pnl:.2f}")
    print(f"毛利:     {gross_profit:.2f}")
    print(f"毛损:     {gross_loss:.2f}")
    print(f"盈亏比PF: {pf:.3f}")
    
    # 持仓时间
    trades_detail = all_closes.groupby(["opentime", "closetime"]).agg(
        open_bar=("openbarno", "first"),
        close_bar=("closebarno", "first")
    ).reset_index()
    trades_detail["hold_bars"] = trades_detail["close_bar"] - trades_detail["open_bar"]
    print(f"\n持仓bar数: {trades_detail['hold_bars'].tolist()}")
    
    # 方向分析
    for d in trades["direction"].unique():
        sub = trades[trades["direction"] == d]
        wr = (sub["trade_pnl"] > 0).mean() * 100 if len(sub) > 0 else 0
        print(f"  {d}: {len(sub)}笔, 胜率{wr:.1f}%, 平均{sub['trade_pnl'].mean():.2f}")

# ===== SIGNALS =====
signals = pd.read_csv(f"{base}/signals.csv")
print(f"\n=== 信号记录 ===")
print(f"信号总数: {len(signals)}")
print(signals.to_string())

# ===== INDICE - 信号分布 =====
indice = pd.read_csv(f"{base}/indice.csv")
# 查看有哪些index_name和line_name
groups = indice.groupby(["index_name", "line_name"]).size().reset_index(name="count")
print(f"\n=== indice.csv 可用指标 ===")
for _, row in groups.iterrows():
    print(f"  {row['index_name']} / {row['line_name']}: {row['count']}条")

# 找signal line
sig_lines = indice[indice["line_name"] == "signal"]
if len(sig_lines) > 0:
    for idx_name in sig_lines["index_name"].unique():
        sub = sig_lines[sig_lines["index_name"] == idx_name]
        vals = sub["value"].dropna()
        print(f"\n=== 信号分布 [{idx_name}/signal] ===")
        print(f"  样本数: {len(vals)}")
        print(f"  均值:   {vals.mean():.4f}")
        print(f"  标准差: {vals.std():.4f}")
        print(f"  最小:   {vals.min():.4f}")
        print(f"  最大:   {vals.max():.4f}")
        for t in [0.3, 0.5, 0.6, 0.7, 0.8]:
            pos = int((vals > t).sum())
            neg = int((vals < -t).sum())
            print(f"  |signal|>{t}: 正{pos} 负{neg} 总{pos+neg}")
