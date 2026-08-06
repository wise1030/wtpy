import numpy as np
from scipy.signal import lfilter
from FactorBase import FactorBase


class SpreadMomentumFactor(FactorBase):
    """
    价差动量因子
    
    纯统计因子，不包含方向假设:
    - momentum > 0: 快均线高于慢均线(上升动量)
    - momentum < 0: 快均线低于慢均线(下降动量)
    - |momentum|越大: 趋势越强 → 动量因子(顺势)
    - 方向取反逻辑由FactorEngine根据signal_mode统一处理
    """
    DIRECTION_TYPE = "momentum"

    def calculate(self, spread: np.ndarray, **kwargs) -> float:
        fast_period = self._params.get('fast_period', 10)
        slow_period = self._params.get('slow_period', 30)
        if len(spread) < slow_period:
            return 0.0
        fast_ma = np.mean(spread[-fast_period:])
        slow_ma = np.mean(spread[-slow_period:])
        std = np.std(spread[-slow_period:])
        if std == 0:
            return 0.0
        momentum = (fast_ma - slow_ma) / std
        return self._normalize(momentum, clip_val=3.0)


class RSISpreadFactor(FactorBase):
    """
    RSI价差因子
    
    纯统计因子，不包含方向假设:
    - RSI超买(>overbought) → 正值(偏高)
    - RSI超卖(<oversold) → 负值(偏低)
    - |RSI偏离|越大: 偏离越远 → 反转因子
    - 方向取反逻辑由FactorEngine根据signal_mode统一处理
    """
    DIRECTION_TYPE = "reversal"

    def calculate(self, spread: np.ndarray, **kwargs) -> float:
        period = self._params.get('period', 14)
        overbought = self._params.get('overbought', 70)
        oversold = self._params.get('oversold', 30)
        # Wilder平滑需要更多历史数据
        if len(spread) < period * 3:
            return 0.0

        deltas = np.diff(spread)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)

        # Wilder平滑: 第一个值用SMA，后续用递推
        avg_gain = np.mean(gains[:period])
        avg_loss = np.mean(losses[:period])

        # P-2(2026-07): lfilter 向量化(原纯Python递推循环, mr profile 每bar~200次迭代)
        # 递推 avg = (avg*(period-1) + cur)/period = avg*(1-1/n) + cur/n
        # 与原形式数值等价(浮点舍入差 ~1e-16/步), 只需终值取 [-1]
        if len(gains) > period:
            alpha = 1.0 / period
            g, _ = lfilter([alpha], [1.0, -(1.0 - alpha)], gains[period:],
                           zi=[(1.0 - alpha) * avg_gain])
            l, _ = lfilter([alpha], [1.0, -(1.0 - alpha)], losses[period:],
                           zi=[(1.0 - alpha) * avg_loss])
            avg_gain = g[-1]
            avg_loss = l[-1]

        if avg_loss == 0:
            rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100.0 - (100.0 / (1.0 + rs))

        # 纯统计映射: RSI从oversold到overbought线性映射到[-1, 1]
        # oversold→-1(偏低), mid→0, overbought→+1(偏高)
        # 修复: 不再硬编码返回±1.0绕过_normalize，统一走归一化管道
        if rsi >= overbought:
            raw = 1.0
        elif rsi <= oversold:
            raw = -1.0
        else:
            mid = (overbought + oversold) / 2.0
            raw = (rsi - mid) / (overbought - mid)
        return self._normalize(raw, clip_val=1.0)
