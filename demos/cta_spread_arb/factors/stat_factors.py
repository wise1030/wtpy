import numpy as np
from FactorBase import FactorBase


class ZScoreFactor(FactorBase):
    """
    滚动Z标准化因子
    
    纯统计因子，不包含方向假设:
    - z > 0: spread高于均值(偏高)
    - z < 0: spread低于均值(偏低)
    - |z|越大: 偏离越远 → 反转因子(偏离远→回归概率高)
    - 方向取反逻辑由FactorEngine根据signal_mode统一处理
    """
    DIRECTION_TYPE = "reversal"

    def calculate(self, spread: np.ndarray, **kwargs) -> float:
        lookback = self._params.get('lookback', 20)  # P3-3: 默认20，与YAML配置对齐
        # 最小数据量: 需要至少5根才能计算z-score
        min_required = max(5, lookback // 2)
        if len(spread) < min_required:
            return 0.0
        actual_lookback = min(lookback, len(spread))
        recent = spread[-actual_lookback:]
        mean = np.mean(recent)
        std = np.std(recent)
        if std == 0:
            return 0.0
        z = (spread[-1] - mean) / std
        return self._normalize(z, clip_val=3.0)


class CointResidualFactor(FactorBase):
    """
    协整残差因子
    
    纯统计因子，不包含方向假设:
    - 残差偏高→正值, 残差偏低→负值
    - |残差z|越大: 偏离越远 → 反转因子
    - 方向取反逻辑由FactorEngine根据signal_mode统一处理
    
    修复:
    1. 移除未使用的statsmodels导入
    2. 增加reset_refit方法供外部调用（替代直接操作_last_refit_day）
    3. 增加协整检验（coint_p_threshold），p值不达标时因子输出0
    """
    DIRECTION_TYPE = "reversal"

    def __init__(self, name: str, params: dict):
        super().__init__(name, params)
        self._beta = None
        self._c = None
        self._last_refit_day = -999
        self._coint_p = 1.0  # 缓存协整p值，默认1.0(不显著)
        self._refit_fail_count = 0  # 修复: 协整失败计数，用于缩短重试间隔

    def _apply_norm_coeffs(self, prices, norm_coeffs):
        """应用归一化系数到价格序列(公共逻辑提取)"""
        codes = list(prices.keys())
        price_list = list(prices.values())
        if norm_coeffs is not None:
            p1 = norm_coeffs.get(codes[0], 1) * np.array(price_list[0])
            p2 = norm_coeffs.get(codes[1], -1) * np.array(price_list[1])
        else:
            p1 = np.array(price_list[0])
            p2 = np.array(price_list[1])
        return p1, p2

    def _ols_coint(self, p1, p2, min_len, coint_p_threshold):
        """OLS回归+协整检验(公共逻辑提取)"""
        y = p1[-min_len:]
        x = p2[-min_len:]
        matrix = np.vstack([x, np.ones(len(x))]).T
        result = np.linalg.lstsq(matrix, y, rcond=None)
        new_beta = result[0][0]
        new_c = result[0][1]
        coint_valid = True
        if coint_p_threshold > 0:
            resid = y - new_beta * x - new_c
            try:
                from statsmodels.tsa.stattools import adfuller
                adf_result = adfuller(resid, maxlag=1)
                self._coint_p = adf_result[1]
                if self._coint_p > coint_p_threshold:
                    coint_valid = False
            except (ImportError, Exception):
                pass
        return new_beta, new_c, coint_valid

    def refit(self, spread: np.ndarray, **kwargs):
        """仅执行协整refit，不计算残差信号(避免_rank_window污染)"""
        lookback = self._params.get('lookback', 20)  # P3-3: 默认20，与YAML配置对齐
        refit_interval = self._params.get('refit_interval', 5)
        coint_p_threshold = self._params.get('coint_p_threshold', 0.05)
        prices = kwargs.get('prices', None)
        norm_coeffs = kwargs.get('norm_coeffs', None)
        if prices is None or len(prices) < 2:
            return
        p1, p2 = self._apply_norm_coeffs(prices, norm_coeffs)
        min_len = min(len(p1), len(p2), lookback)
        # 最小数据量: 需要至少20根才能做OLS回归+ADF检验
        # 原值30对lookback=20太严格，改为max(20, lookback//2)
        min_required = max(20, lookback // 2)
        if min_len < min_required:
            # P1-4修复: 数据不足时清除旧beta/c，避免残留过期参数
            self._beta = None
            self._c = None
            return
        cur_date = kwargs.get('cur_date', 0)
        effective_interval = max(1, refit_interval // (2 ** self._refit_fail_count))
        if self._beta is not None and (cur_date - self._last_refit_day) < effective_interval:
            return  # 不需要refit
        try:
            new_beta, new_c, coint_valid = self._ols_coint(
                p1, p2, min_len, coint_p_threshold)
            if coint_valid:
                self._beta = new_beta
                self._c = new_c
                self._last_refit_day = cur_date
                self._refit_fail_count = 0
            else:
                self._beta = None
                self._c = None
                self._last_refit_day = cur_date
                self._refit_fail_count += 1
        except Exception:
            pass

    def calculate(self, spread: np.ndarray, **kwargs) -> float:
        """计算协整残差信号 — 只使用已缓存的beta/c, 不做refit

        v2修复: 协整不显著(beta=None)时回退到price spread z-score,
        而非输出0.0。这样在协整弱化期仍能提供方向信号，
        由CointGateStep通过coint_p衰减权重来控制信号强度。
        
        refit由DataPipe._calc_coint_spread()在fetch阶段显式调用refit()方法。
        calculate()只负责信号计算, 职责单一。
        """
        lookback = self._params.get('lookback', 20)  # P3-3: 默认20，与YAML配置对齐
        prices = kwargs.get('prices', None)
        norm_coeffs = kwargs.get('norm_coeffs', None)
        if prices is None or len(prices) < 2:
            return 0.0

        p1, p2 = self._apply_norm_coeffs(prices, norm_coeffs)
        min_len = min(len(p1), len(p2), lookback)
        # 最小数据量: 需要至少20根才能做残差计算
        # 原值30对lookback=20太严格，改为max(20, lookback//2)
        min_required = max(20, lookback // 2)
        if min_len < min_required:
            return 0.0

        if self._beta is not None:
            # 协整有效: 用coint residual z-score
            resid = p1[-min_len:] - self._beta * p2[-min_len:] - self._c
        else:
            # 协整不显著: 回退到price spread z-score(降级而非输出0)
            resid = p1[-min_len:] - p2[-min_len:]

        mean = np.mean(resid)
        std = np.std(resid)
        if std == 0:
            return 0.0
        current_resid = resid[-1]
        z = (current_resid - mean) / std
        # 纯统计因子: 残差偏高→正值, 残差偏低→负值
        # 方向取反逻辑由FactorEngine根据signal_mode统一处理
        return self._normalize(z, clip_val=3.0)

    def reset_refit(self):
        """重置refit状态，供on_session_begin调用"""
        self._last_refit_day = -999
        self._refit_fail_count = 0

    def get_coint_params(self) -> tuple:
        """获取缓存的协整参数(beta, c, coint_p)，供SpreadCalculator和门控使用"""
        return (self._beta, self._c, self._coint_p)

    def get_coint_p(self) -> float:
        """获取协整p值(公共接口，供FactorEngine使用)"""
        return self._coint_p


class HalfLifeFactor(FactorBase):
    """
    半衰期因子
    
    纯统计因子，不包含方向假设:
    - 输出均值回归强度 * 价差偏离方向
    - 偏离方向: spread高于均值→正值(偏高), spread低于均值→负值(偏低)
    - 半衰期短→MR强度大 → 反转因子(回归概率高)
    - 方向取反逻辑由FactorEngine根据signal_mode统一处理
    """
    DIRECTION_TYPE = "reversal"

    def calculate(self, spread: np.ndarray, **kwargs) -> float:
        lookback = self._params.get('lookback', 20)  # P3-3: 默认20，与YAML配置对齐
        min_hl = self._params.get('min_half_life', 1)
        max_hl = self._params.get('max_half_life', 30)
        # 最小数据量: 需要至少10根才能做OLS回归
        # 原值lookback对lookback=20太严格(需要20根才开始输出)
        min_required = max(10, lookback // 2)
        if len(spread) < min_required:
            return 0.0
        recent = spread[-lookback:] if len(spread) >= lookback else spread[-min_required:]
        dy = np.diff(recent)
        y_lag = recent[:-1]
        if np.std(y_lag) == 0:
            return 0.0
        matrix = np.vstack([y_lag, np.ones(len(y_lag))]).T
        try:
            result = np.linalg.lstsq(matrix, dy, rcond=None)
            beta = result[0][0]
        except Exception:
            return 0.0
        if beta >= 0:
            # 非均值回归 -> 输出0
            return 0.0
        half_life = -np.log(2) / beta

        # 计算均值回归强度 [0, 1]
        if half_life < min_hl:
            strength = 1.0
        elif half_life > max_hl:
            return 0.0
        else:
            strength = 1.0 - (half_life - min_hl) / (max_hl - min_hl)

        # 用价差偏离方向赋予符号，输出[-1, 1]
        spread_mean = np.mean(recent)
        spread_std = np.std(recent)
        if spread_std == 0:
            return 0.0
        deviation_sign = np.sign(spread[-1] - spread_mean)

        raw = float(deviation_sign * strength)
        # 走_normalize管道: 与其他因子一致的排名归一化
        return self._normalize(raw, clip_val=1.0)
