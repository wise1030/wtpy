"""
BacktestAnalyzer — 回测分析工具

功能:
1. 参数敏感性分析 — 单参数扫描+热力图
2. 滚动回测 — 按时间窗滚动评估稳定性
3. Walk-forward验证 — 训练/测试分离防过拟合
4. 批量品种回测 — 多组合并行评估
"""
import numpy as np
from collections import defaultdict


class BacktestAnalyzer:
    """回测分析器"""

    def __init__(self, trades: list = None, equity_curve: np.ndarray = None):
        """
        Args:
            trades: 交易记录列表, 每条 {entry_time, exit_time, direction, pnl, ...}
            equity_curve: 权益曲线
        """
        self._trades = trades or []
        self._equity = equity_curve

    # ── 基础统计 ──

    def summary(self) -> dict:
        """基础绩效统计"""
        if not self._trades:
            return {"total_trades": 0}

        pnls = [t['pnl'] for t in self._trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        total = sum(pnls)
        win_rate = len(wins) / len(pnls) if pnls else 0
        avg_win = np.mean(wins) if wins else 0
        avg_loss = np.mean(losses) if losses else 0
        profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float('inf')

        # 最大回撤
        cum = np.cumsum(pnls)
        running_max = np.maximum.accumulate(cum)
        drawdowns = cum - running_max
        max_dd = float(np.min(drawdowns)) if len(drawdowns) > 0 else 0

        # Sharpe(年化, 假设每bar=5min, 48bars/day, 250days/year)
        if len(pnls) > 1:
            sharpe = np.mean(pnls) / (np.std(pnls) + 1e-9) * np.sqrt(48 * 250)
        else:
            sharpe = 0.0

        return {
            "total_trades": len(pnls),
            "total_pnl": total,
            "win_rate": win_rate,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_factor": profit_factor,
            "max_drawdown": max_dd,
            "sharpe_annual": sharpe,
        }

    # ── 参数敏感性 ──

    @staticmethod
    def param_sweep(results_by_param: dict) -> list:
        """
        单参数扫描分析

        Args:
            results_by_param: {param_value: summary_dict, ...}

        Returns:
            按total_pnl降序排列的结果列表
        """
        rows = []
        for val, stats in results_by_param.items():
            rows.append({
                "param_value": val,
                "total_pnl": stats.get("total_pnl", 0),
                "win_rate": stats.get("win_rate", 0),
                "profit_factor": stats.get("profit_factor", 0),
                "max_drawdown": stats.get("max_drawdown", 0),
                "total_trades": stats.get("total_trades", 0),
            })
        rows.sort(key=lambda x: -x["total_pnl"])
        return rows

    @staticmethod
    def param_heatmap(results_2d: dict) -> np.ndarray:
        """
        双参数热力图

        Args:
            results_2d: {(param1_val, param2_val): total_pnl, ...}

        Returns:
            2D numpy数组 + 轴标签
        """
        keys = list(results_2d.keys())
        x_vals = sorted(set(k[0] for k in keys))
        y_vals = sorted(set(k[1] for k in keys))
        x_idx = {v: i for i, v in enumerate(x_vals)}
        y_idx = {v: i for i, v in enumerate(y_vals)}

        matrix = np.full((len(y_vals), len(x_vals)), np.nan)
        for (xv, yv), pnl in results_2d.items():
            matrix[y_idx[yv], x_idx[xv]] = pnl

        return matrix, x_vals, y_vals

    # ── 滚动回测 ──

    def rolling_performance(self, window: int = 50, step: int = 10) -> list:
        """
        滚动窗口绩效评估

        Args:
            window: 窗口大小(交易笔数)
            step: 滑动步长

        Returns:
            [{window_start, window_end, total_pnl, win_rate, pf}, ...]
        """
        if len(self._trades) < window:
            return []

        results = []
        for i in range(0, len(self._trades) - window + 1, step):
            subset = self._trades[i:i + window]
            pnls = [t['pnl'] for t in subset]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p < 0]
            wr = len(wins) / len(pnls) if pnls else 0
            pf = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float('inf')
            results.append({
                "window_start": i,
                "window_end": i + window,
                "total_pnl": sum(pnls),
                "win_rate": wr,
                "profit_factor": pf,
            })
        return results

    # ── Walk-forward ──

    @staticmethod
    def walk_forward_split(trades: list, n_splits: int = 5,
                           train_ratio: float = 0.7) -> list:
        """
        Walk-forward验证: 训练/测试分离

        Args:
            trades: 完整交易记录
            n_splits: 分割数
            train_ratio: 训练集比例

        Returns:
            [{fold, train_trades, test_trades, train_pnl, test_pnl}, ...]
        """
        n = len(trades)
        fold_size = n // (n_splits + 1)
        results = []

        for i in range(n_splits):
            train_end = fold_size * (i + 2)
            test_end = min(fold_size * (i + 3), n)
            if test_end <= train_end:
                break

            train_start = max(0, train_end - int(fold_size * train_ratio / (1 - train_ratio)))
            train = trades[train_start:train_end]
            test = trades[train_end:test_end]

            results.append({
                "fold": i,
                "train_trades": len(train),
                "test_trades": len(test),
                "train_pnl": sum(t['pnl'] for t in train),
                "test_pnl": sum(t['pnl'] for t in test),
            })
        return results

    # ── 报告输出 ──

    @staticmethod
    def print_summary(stats: dict):
        """打印绩效摘要"""
        if stats.get("total_trades", 0) == 0:
            print("无交易记录")
            return
        print(f"交易笔数: {stats['total_trades']}")
        print(f"总盈亏:   {stats['total_pnl']:.0f}")
        print(f"胜率:     {stats['win_rate']:.1%}")
        print(f"盈亏比:   {stats['profit_factor']:.2f}")
        print(f"最大回撤: {stats['max_drawdown']:.0f}")
        print(f"年化Sharpe: {stats['sharpe_annual']:.2f}")

    @staticmethod
    def print_sweep(rows: list, param_name: str = "param"):
        """打印参数扫描结果"""
        print(f"\n{'='*65}")
        print(f"参数敏感性: {param_name}")
        print(f"{'-'*65}")
        print(f"{'参数值':>10} {'盈亏':>10} {'胜率':>8} {'PF':>6} {'回撤':>10} {'笔数':>6}")
        print(f"{'-'*65}")
        for r in rows:
            print(f"{r['param_value']:>10} {r['total_pnl']:>10.0f} "
                  f"{r['win_rate']:>8.1%} {r['profit_factor']:>6.2f} "
                  f"{r['max_drawdown']:>10.0f} {r['total_trades']:>6}")
        print(f"{'='*65}")
