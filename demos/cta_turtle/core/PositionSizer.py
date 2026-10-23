"""VaR 仓位反算: lots = floor(units × unit_var / (N × volscale))

口径: 1 手 1N 不利波动 = N × volscale CNY,
每层预算 units × unit_var, 故每层手数使 1N 波动 ≈ 预算 VaR。
2N 止损触发时单层实际最大亏损 ≈ 2 × units × unit_var。
"""
import math


class PositionSizer:
    def __init__(self, unit_var: float):
        self._unit_var = unit_var

    def layer_lots(self, n: float, volscale: float, units: int) -> int:
        """计算单层手数。

        Args:
            n: 当前 ATR (价格单位)
            volscale: 合约乘数 (stra_get_comminfo().volscale)
            units: 交易单位数 (每层 VaR = units × unit_var)

        Returns:
            手数; N 无效(<=0/NaN)或预算不足 1 手时返回 0 (调用方放弃入场)
        """
        if n is None or not math.isfinite(n) or n <= 0:
            return 0
        if volscale <= 0 or units <= 0:
            return 0
        budget = units * self._unit_var
        lots = int(budget // (n * volscale))
        return max(0, lots)
