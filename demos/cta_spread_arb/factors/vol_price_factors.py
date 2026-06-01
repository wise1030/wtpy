import numpy as np
from FactorBase import FactorBase


class VwapDeviationFactor(FactorBase):
    """
    VWAP偏离度因子

    修复: 平滑逻辑切片错误
    原逻辑: spread[-(period+i):len(spread)-i] 导致不同i值下窗口终点不一致
    修复: 统一使用以当前bar为终点的滚动窗口
    """

    def calculate(self, spread: np.ndarray, **kwargs) -> float:
        period = self._params.get('period', 20)
        smooth = self._params.get('smooth', 5)
        if len(spread) < period:
            return 0.0
        recent = spread[-period:]
        volumes = kwargs.get('volumes', None)
        if volumes is not None:
            vol_list = list(volumes.values())
            if len(vol_list) >= 2:
                v1 = np.array(vol_list[0][-period:])
                v2 = np.array(vol_list[1][-period:])
                total_vol = v1 + v2
                total_vol = np.where(total_vol == 0, 1, total_vol)
                vwap = np.sum(recent * total_vol) / np.sum(total_vol)
            else:
                vwap = np.mean(recent)
        else:
            vwap = np.mean(recent)
        std = np.std(recent)
        if std == 0:
            return 0.0
        deviation = (spread[-1] - vwap) / std

        if smooth > 1 and len(spread) >= period + smooth - 1:
            deviations = []
            for i in range(smooth):
                # 修复: 统一以每个偏移位置的bar为终点，窗口长度固定为period
                end_idx = len(spread) - i
                start_idx = end_idx - period
                if start_idx < 0:
                    deviations.append(0)
                    continue
                sub = spread[start_idx:end_idx]
                s = np.std(sub)
                if s == 0:
                    deviations.append(0)
                else:
                    deviations.append((sub[-1] - np.mean(sub)) / s)
            deviation = np.mean(deviations)
        return self._normalize(deviation, clip_val=3.0)


class VolumePriceDivergenceFactor(FactorBase):
    """
    量价背离因子

    修复: threshold参数语义明确化
    """

    def calculate(self, spread: np.ndarray, **kwargs) -> float:
        lookback = self._params.get('lookback', 30)
        vol_ma_period = self._params.get('vol_ma_period', 20)
        # vol_confirm_ratio: 量能确认比例，低于此比例视为量能不足
        vol_confirm_ratio = self._params.get('vol_confirm_ratio', 0.7)
        if len(spread) < lookback:
            return 0.0
        volumes = kwargs.get('volumes', None)
        if volumes is None:
            return 0.0
        vol_list = list(volumes.values())
        if len(vol_list) < 2:
            return 0.0
        v1 = np.array(vol_list[0])
        v2 = np.array(vol_list[1])
        total_vol = v1 + v2
        recent_spread = spread[-lookback:]
        recent_vol = total_vol[-lookback:]
        if len(recent_vol) < vol_ma_period:
            return 0.0
        spread_high = np.max(recent_spread)
        spread_low = np.min(recent_spread)
        vol_ma = np.mean(recent_vol[-vol_ma_period:])
        current_vol = recent_vol[-1]
        # 修复: 使用绝对最小阈值，避免近零价差时阈值过小
        threshold = max(abs(spread_high) * 0.01, 0.001)
        last_is_new_high = abs(recent_spread[-1] - spread_high) < threshold
        threshold_low = max(abs(spread_low) * 0.01, 0.001)
        last_is_new_low = abs(recent_spread[-1] - spread_low) < threshold_low
        vol_sufficient = current_vol > vol_ma * vol_confirm_ratio
        # 修复: 不再传硬编码常量给_normalize，改为返回实际偏离度走统一归一化
        if last_is_new_high and not vol_sufficient:
            # 新高但量能不足 → 量价背离(看跌)
            raw = -0.8
        elif last_is_new_low and not vol_sufficient:
            # 新低但量能不足 → 量价背离(看涨)
            raw = 0.8
        elif recent_spread[-1] > np.mean(recent_spread):
            # 价差偏高，量能偏离度
            raw = (current_vol / vol_ma - 1) * -0.3
        else:
            # 价差偏低，量能偏离度
            raw = (current_vol / vol_ma - 1) * 0.3
        return self._normalize(raw, clip_val=1.0)


class OBVSpreadFactor(FactorBase):
    """
    OBV价差因子

    修复: 放大系数可配置化
    """

    def calculate(self, spread: np.ndarray, **kwargs) -> float:
        period = self._params.get('period', 20)
        amplify = self._params.get('amplify', 10)
        if len(spread) < period + 1:
            return 0.0
        recent = spread[-(period + 1):]
        volumes = kwargs.get('volumes', None)
        vol_list = list(volumes.values()) if volumes is not None else []
        if len(vol_list) >= 2:
            v1 = np.array(vol_list[0][-(period + 1):])
            v2 = np.array(vol_list[1][-(period + 1):])
            total_vol = v1 + v2
        else:
            total_vol = np.ones(period + 1)
        obv = np.zeros(period + 1)
        for i in range(1, len(recent)):
            if recent[i] > recent[i - 1]:
                obv[i] = obv[i - 1] + total_vol[i]
            elif recent[i] < recent[i - 1]:
                obv[i] = obv[i - 1] - total_vol[i]
            else:
                obv[i] = obv[i - 1]
        obv_change = (obv[-1] - obv[0]) / (np.sum(total_vol[1:]) + 1e-10)
        return self._normalize(obv_change * amplify, clip_val=3.0)


class VolumeRatioFactor(FactorBase):
    """
    量比因子

    优化: 即使量比未超过阈值也输出弱信号，提高信息利用率
    """

    def calculate(self, spread: np.ndarray, **kwargs) -> float:
        period = self._params.get('period', 10)
        threshold = self._params.get('threshold', 2.0)
        volumes = kwargs.get('volumes', None)
        if volumes is None:
            return 0.0
        vol_list = list(volumes.values())
        if len(vol_list) < 2:
            return 0.0
        v1 = np.array(vol_list[0])
        v2 = np.array(vol_list[1])
        total_vol = v1 + v2
        if len(total_vol) < period + 1:
            return 0.0
        current_vol = total_vol[-1]
        avg_vol = np.mean(total_vol[-(period + 1):-1])
        if avg_vol == 0:
            return 0.0
        ratio = current_vol / avg_vol
        # 量比超过阈值: 强信号
        if ratio >= threshold:
            if spread[-1] > spread[-2]:
                raw = (ratio - threshold) * 0.5
            else:
                raw = -(ratio - threshold) * 0.5
        # 量比在1.0~threshold之间: 弱信号（提高信息利用率）
        elif ratio > 1.0:
            weak_signal = (ratio - 1.0) / (threshold - 1.0) * 0.2
            if spread[-1] > spread[-2]:
                raw = weak_signal
            else:
                raw = -weak_signal
        else:
            return 0.0
        # 修复: 统一走_normalize管道，不再弱信号直接返回
        return self._normalize(raw, clip_val=1.0)
