"""
trend_factors.py — 趋势类因子

普适性设计: 不依赖特定品种对或频率
- MACDSpreadFactor: MACD趋势方向+强度 (momentum)
- ADXSpreadFactor: ADX趋势强度 (momentum)
- ChannelBreakFactor: Donchian通道突破 (momentum)
- TrendStrengthFactor: 多周期趋势一致性 (momentum)

所有因子输出语义:
- 正值: 上升趋势/多头信号
- 负值: 下降趋势/空头信号
- 绝对值: 趋势强度
- direction_type = "momentum": 由FactorEngine根据regime处理方向
"""
import numpy as np
from FactorBase import FactorBase


class MACDSpreadFactor(FactorBase):
    """
    MACD趋势因子

    计算spread的MACD(快EMA-慢EMA), 信号线, 柱状图:
    - MACD > 0 且柱状图放大: 上升趋势加速 → 正值
    - MACD < 0 且柱状图缩小(绝对值): 下降趋势减速 → 正值(反转前兆)
    - 输出: MACD柱状图的z-score, 带方向

    direction_type = momentum: 趋势方向即交易方向
    """
    DIRECTION_TYPE = "momentum"

    def calculate(self, spread: np.ndarray, **kwargs) -> float:
        fast_period = self._params.get('fast_period', 12)
        slow_period = self._params.get('slow_period', 26)
        signal_period = self._params.get('signal_period', 9)
        min_len = slow_period + signal_period
        if len(spread) < min_len:
            return 0.0

        # EMA计算
        fast_ema = self._ema(spread, fast_period)
        slow_ema = self._ema(spread, slow_period)
        macd_line = fast_ema - slow_ema

        # 信号线 = MACD的EMA
        signal_line = self._ema(macd_line, signal_period)
        histogram = macd_line - signal_line

        # 用柱状图的z-score作为信号
        lookback = min(len(histogram), slow_period * 2)
        recent_hist = histogram[-lookback:]
        std = np.std(recent_hist)
        if std == 0:
            return 0.0
        z = histogram[-1] / std
        return self._normalize(z, clip_val=3.0)

    @staticmethod
    def _ema(data: np.ndarray, period: int) -> np.ndarray:
        alpha = 2.0 / (period + 1)
        ema = np.zeros_like(data)
        ema[0] = data[0]
        for i in range(1, len(data)):
            ema[i] = alpha * data[i] + (1 - alpha) * ema[i - 1]
        return ema


class ADXSpreadFactor(FactorBase):
    """
    ADX趋势强度因子 (Wilder 1978 标准实现)

    因 spread 是一维序列没有 H/L/C，用滚动窗口合成 OHLC：
    - C_t = spread[t]
    - H_t = max(spread[t-hl_window+1 : t+1])
    - L_t = min(spread[t-hl_window+1 : t+1])
    - 这样保留了波动率信息(H-L是真实波动范围)，比裸 |Δspread| 更接近真 ATR 语义

    标准 Wilder 公式:
    - TR_t = max(H_t-L_t, |H_t-C_{t-1}|, |L_t-C_{t-1}|)
    - +DM_t = up_move > down_move and up_move > 0 ? up_move : 0  (互斥规则)
    - -DM_t = down_move > up_move and down_move > 0 ? down_move : 0
      其中 up_move = H_t - H_{t-1}, down_move = L_{t-1} - L_t
    - ATR/+DM_s/-DM_s 用 Wilder 平滑: x_t = (x_{t-1}*(n-1) + cur_t) / n
    - +DI = 100 * +DM_s / ATR;  -DI = 100 * -DM_s / ATR
    - DX = 100 * |+DI - -DI| / (+DI + -DI)
    - ADX = Wilder 平滑(DX)

    输出: ADX强度(0~1) × 方向(±1)，方向来自 sign(+DI - -DI)
    direction_type = momentum: ADX强时趋势方向即交易方向

    参数:
    - period: ADX 周期 (默认 21)
    - threshold: ADX 趋势/震荡分界 (默认 25)
    - hl_window: 合成 H/L 的滚动窗口长度 (默认 2, 范围 2-5;
      套利价差为均值回归型，hl=2 在震荡场景下输出最干净；hl 越大越平滑但越滞后)
    """
    DIRECTION_TYPE = "momentum"

    def calculate(self, spread: np.ndarray, **kwargs) -> float:
        period = int(self._params.get('period', 21))
        # 兼容旧字段 adx_threshold 与新字段 threshold
        adx_threshold = float(self._params.get('threshold',
                                                self._params.get('adx_threshold', 25.0)))
        hl_window = int(self._params.get('hl_window', 2))

        # 至少要 hl_window + period(初始TR/DM) + period(ADX 平滑) + 余量
        min_len = max(period * 3, hl_window + period * 2 + 1)
        n = len(spread)
        if n < min_len:
            return 0.0

        spread = np.asarray(spread, dtype=np.float64)

        # === Step 1: 合成 OHLC (用滚动窗口的 max/min) ===
        # 对每个 t (t >= hl_window-1) 计算 H_t = max(spread[t-hl_window+1:t+1])
        # 用 numpy 向量化(stride trick 也可，这里用简单循环保证可读性)
        high = np.empty(n)
        low = np.empty(n)
        high[:hl_window - 1] = spread[:hl_window - 1]
        low[:hl_window - 1] = spread[:hl_window - 1]
        for i in range(hl_window - 1, n):
            window = spread[i - hl_window + 1 : i + 1]
            high[i] = window.max()
            low[i] = window.min()
        close = spread  # 当前 bar 收盘 = spread 本身

        # === Step 2: TR / +DM / -DM (从 i=1 开始，需要 t-1) ===
        # 有效计算起点 = hl_window (前 hl_window-1 个点 H/L 不可靠)
        start = hl_window
        m = n - start  # 有效样本数
        if m < period * 2 + 1:
            return 0.0

        tr = np.empty(m)
        plus_dm = np.empty(m)
        minus_dm = np.empty(m)

        for k in range(m):
            i = start + k
            # True Range
            h_l = high[i] - low[i]
            h_pc = abs(high[i] - close[i - 1])
            l_pc = abs(low[i] - close[i - 1])
            tr[k] = max(h_l, h_pc, l_pc)
            # Directional Movement (互斥规则)
            up_move = high[i] - high[i - 1]
            down_move = low[i - 1] - low[i]
            if up_move > down_move and up_move > 0:
                plus_dm[k] = up_move
            else:
                plus_dm[k] = 0.0
            if down_move > up_move and down_move > 0:
                minus_dm[k] = down_move
            else:
                minus_dm[k] = 0.0

        # === Step 3: Wilder 平滑 (前 period 根取 sum，之后递推) ===
        atr = np.zeros(m)
        pdm_s = np.zeros(m)
        mdm_s = np.zeros(m)

        atr[period - 1] = tr[:period].sum()
        pdm_s[period - 1] = plus_dm[:period].sum()
        mdm_s[period - 1] = minus_dm[:period].sum()

        for k in range(period, m):
            # Wilder: x_t = x_{t-1} - x_{t-1}/period + cur_t
            atr[k] = atr[k - 1] - atr[k - 1] / period + tr[k]
            pdm_s[k] = pdm_s[k - 1] - pdm_s[k - 1] / period + plus_dm[k]
            mdm_s[k] = mdm_s[k - 1] - mdm_s[k - 1] / period + minus_dm[k]

        # === Step 4: +DI / -DI / DX ===
        valid = atr > 0
        plus_di = np.zeros(m)
        minus_di = np.zeros(m)
        plus_di[valid] = 100.0 * pdm_s[valid] / atr[valid]
        minus_di[valid] = 100.0 * mdm_s[valid] / atr[valid]

        di_sum = plus_di + minus_di
        dx = np.zeros(m)
        mask = di_sum > 0
        dx[mask] = 100.0 * np.abs(plus_di[mask] - minus_di[mask]) / di_sum[mask]

        # === Step 5: ADX = Wilder 平滑(DX), 从 2*period-1 开始 ===
        adx_start = 2 * period - 1
        if adx_start >= m:
            return 0.0
        adx = np.zeros(m)
        adx[adx_start] = dx[period - 1 : adx_start + 1].mean()
        for k in range(adx_start + 1, m):
            adx[k] = (adx[k - 1] * (period - 1) + dx[k]) / period

        current_adx = adx[-1]
        if not np.isfinite(current_adx) or current_adx <= 0:
            return 0.0

        # === Step 6: 输出 = 方向 × 强度 ===
        direction = 1.0 if plus_di[-1] > minus_di[-1] else -1.0
        # 强度映射: [0, threshold] → [0, 0.5], [threshold, 50] → [0.5, 1.0]
        if current_adx < adx_threshold:
            strength = 0.5 * current_adx / adx_threshold
        else:
            strength = 0.5 + 0.5 * min(1.0, (current_adx - adx_threshold) / (50.0 - adx_threshold))

        return self._normalize(direction * strength, clip_val=1.0)


class ChannelBreakFactor(FactorBase):
    """
    Donchian通道突破因子

    价格突破N周期最高/最低价:
    - 突破上轨: 正值(多头突破)
    - 突破下轨: 负值(空头突破)
    - 在通道内: 0
    - 突破幅度越大, 信号越强

    direction_type = momentum: 突破方向即交易方向
    """
    DIRECTION_TYPE = "momentum"

    def calculate(self, spread: np.ndarray, **kwargs) -> float:
        channel_period = self._params.get('channel_period', 20)
        if len(spread) < channel_period + 1:
            return 0.0

        recent = spread[-(channel_period + 1):]
        # 排除当前bar的通道
        channel = recent[:-1]
        upper = np.max(channel)
        lower = np.min(channel)
        current = recent[-1]
        channel_range = upper - lower

        if channel_range == 0:
            return 0.0

        # 突破幅度(相对于通道宽度)
        if current > upper:
            breakout = (current - upper) / channel_range
            return self._normalize(min(breakout, 1.0), clip_val=1.0)
        elif current < lower:
            breakout = (lower - current) / channel_range
            return self._normalize(-min(breakout, 1.0), clip_val=1.0)
        else:
            # 在通道内: 用位置比例给弱信号
            position = (current - lower) / channel_range  # [0, 1]
            # 映射到[-0.3, 0.3]: 通道中部=0, 上部=正, 下部=负
            raw = (position - 0.5) * 0.6
            return self._normalize(raw, clip_val=1.0)


class TrendStrengthFactor(FactorBase):
    """
    多周期趋势一致性因子

    检查多个时间窗口的趋势方向是否一致:
    - 短/中/长期趋势同向 → 强趋势 → 大信号
    - 趋势不一致 → 弱趋势/震荡 → 小信号

    direction_type = momentum: 趋势一致方向即交易方向
    """
    DIRECTION_TYPE = "momentum"

    def calculate(self, spread: np.ndarray, **kwargs) -> float:
        short_period = self._params.get('short_period', 5)
        mid_period = self._params.get('mid_period', 20)
        long_period = self._params.get('long_period', 60)
        if len(spread) < long_period:
            return 0.0

        # 各周期趋势方向(用MA斜率)
        std_long = np.std(spread[-long_period:])
        if std_long == 0:
            return 0.0

        short_slope = (np.mean(spread[-short_period:]) - np.mean(spread[-2 * short_period:-short_period])) / std_long
        mid_slope = (np.mean(spread[-mid_period:]) - np.mean(spread[-2 * mid_period:-mid_period])) / std_long
        long_slope = (np.mean(spread[-long_period // 2:]) - np.mean(spread[-long_period:])) / std_long

        # 方向一致性: 3个斜率同号 → 强, 2同1异 → 中, 各异 → 弱
        signs = [np.sign(short_slope), np.sign(mid_slope), np.sign(long_slope)]
        pos_count = sum(1 for s in signs if s > 0)
        neg_count = sum(1 for s in signs if s < 0)

        if pos_count >= 2:
            majority_dir = 1.0
            consistency = pos_count / 3.0
        elif neg_count >= 2:
            majority_dir = -1.0
            consistency = neg_count / 3.0
        else:
            return 0.0  # 方向不一致

        # 用最大斜率绝对值作为强度
        max_slope = max(abs(short_slope), abs(mid_slope), abs(long_slope))
        strength = consistency * min(1.0, max_slope)

        return self._normalize(majority_dir * strength, clip_val=1.0)
