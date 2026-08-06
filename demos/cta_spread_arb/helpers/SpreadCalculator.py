import numpy as np


class SpreadCalculator:
    """
    价差计算器 — 统一入口(v3)
    
    架构升级:
    1. calc_spread()为唯一价差计算入口，支持price/coint两种模式
    2. 删除calc_coint_spread()中的OLS回退(由CointResidualFactor负责)
    3. 日线价差也通过calc_spread()计算，消除不一致
    4. beta/c为None时回退到price模式而非自行OLS
    """

    def __init__(self, legs_config: list):
        """
        Args:
            legs_config: list[LegConfig] — 结构化腿配置
        """
        self._legs = legs_config
        self.upper = 0.0
        self.lower = 0.0
        self.mean = 0.0
        self.std = 0.0

        # === PnL系数: lots × multiplier × sign(ratio) ===
        self._pnl_coeffs = {}
        for leg in self._legs:
            lots = leg.lots
            mult = leg.multiplier
            ratio = leg.ratio
            coeff = lots * mult * (1 if ratio > 0 else -1)
            self._pnl_coeffs[leg.code] = coeff

        # === 归一化系数 v2: 按名义价值比例归一化 ===
        total_abs = sum(abs(c) for c in self._pnl_coeffs.values())
        if total_abs > 0:
            self._norm_coeffs = {}
            for leg in self._legs:
                code = leg.code
                self._norm_coeffs[code] = self._pnl_coeffs[code] / total_abs
        else:
            self._norm_coeffs = dict(self._pnl_coeffs)

        self._pnl_scale = total_abs

    def calc_spread(self, prices: dict, mode: str = 'price',
                    beta: float = None, c: float = None,
                    timestamps: dict = None) -> np.ndarray:
        """
        统一价差计算入口

        Args:
            prices: dict code -> 价格序列
            mode: 'price'(价格价差) 或 'coint'(协整价差)
            beta: 协整beta(coint模式必须提供)
            c: 协整截距(coint模式可选)
            timestamps: dict code -> 时间戳序列(bar时间)，用于精确对齐。
                        为None时回退到尾端截断(向后兼容，不推荐实盘使用)。

        Returns:
            价差序列，数据不足时返回None
        """
        aligned_prices = self._align_series(prices, timestamps)
        if aligned_prices is None or len(aligned_prices) == 0:
            return None

        codes = [leg.code for leg in self._legs]
        if len(codes) < 2:
            return None

        # 计算归一化后的各腿价格
        norm_prices = {}
        for code in codes:
            coeff = self._norm_coeffs.get(code, 1)
            if code in aligned_prices:
                norm_prices[code] = coeff * np.array(aligned_prices[code])

        if codes[0] not in norm_prices or codes[1] not in norm_prices:
            return None

        p1 = norm_prices[codes[0]]
        p2 = norm_prices[codes[1]]

        if mode == 'coint' and beta is not None:
            # 协整价差: p1 - beta * p2 - c
            spread = p1 - beta * p2
            if c is not None:
                spread = spread - c
            return spread
        else:
            # 价格价差: p1 + p2 (p2已带负号系数)
            return p1 + p2

    def calc_price_spread(self, prices: dict, volumes: dict = None,
                          timestamps: dict = None) -> np.ndarray:
        """
        计算加权价差(名义价值比例归一化)
        保留向后兼容，内部调用calc_spread
        """
        result = self.calc_spread(prices, mode='price', timestamps=timestamps)
        if result is None:
            return np.zeros(1)
        return result

    def calc_coint_spread(self, prices: dict, beta: float = None,
                          c: float = None,
                          timestamps: dict = None) -> np.ndarray:
        """
        计算协整价差(名义价值比例归一化)
        保留向后兼容，内部调用calc_spread
        beta/c为None时回退到price模式(不再自行OLS)
        """
        if beta is None:
            result = self.calc_spread(prices, mode='price', timestamps=timestamps)
        else:
            result = self.calc_spread(prices, mode='coint', beta=beta, c=c,
                                      timestamps=timestamps)
        if result is None:
            return np.zeros(1)
        return result

    def update_bands(self, spread: np.ndarray, lookback: int = 60,
                     threshold: float = 2.0):
        """更新布林带"""
        if len(spread) < lookback:
            return
        recent = spread[-lookback:]
        self.mean = float(np.mean(recent))
        self.std = float(np.std(recent))
        self.upper = self.mean + threshold * self.std
        self.lower = self.mean - threshold * self.std

    def align_series(self, prices: dict, timestamps: dict = None) -> dict:
        """对齐所有腿的数据，确保同一时间戳配对。

        timestamps 非空时按时间戳 join 对齐(实盘必需，两腿可能缺失不同 bar)，
        为 None 时回退到尾端截断(向后兼容，回测中两腿通常同步)。

        P-1(2026-07): timestamps 路径向量化 — np.intersect1d + searchsorted
        替代 set/dict/tolist 的 Python 级哈希(原每bar ~1500次Python操作)。
        bartimes 单调递增且唯一, searchsorted 直接定位, 正确性等价。
        """
        if not prices:
            return None
        codes = list(prices.keys())
        lengths = [len(prices[c]) for c in codes]
        min_len = min(lengths)
        if min_len < 2:
            return None

        if timestamps is None:
            aligned = {}
            for code in codes:
                aligned[code] = np.array(prices[code][-min_len:])
            return aligned

        ts_map = {}
        for code in codes:
            ts = np.asarray(timestamps.get(code, []))
            if len(ts) != len(prices[code]):
                # 部分腿缺timestamps(如mock/数据源异常)时降级为尾端截断,
                # 而非返回None导致整bar不交易
                aligned = {}
                for c in codes:
                    aligned[c] = np.array(prices[c][-min_len:])
                return aligned
            ts_map[code] = ts

        # 逐腿求交集(intersect1d 返回排序去重结果)
        common_ts = None
        for code in codes:
            if common_ts is None:
                common_ts = ts_map[code]
            else:
                common_ts = np.intersect1d(common_ts, ts_map[code])
                if len(common_ts) < 2:
                    return None

        result = {}
        for code in codes:
            # ts 单调递增且 common ⊆ ts → searchsorted 即精确下标
            idx = np.searchsorted(ts_map[code], common_ts)
            result[code] = np.asarray(prices[code])[idx]
        return result

    # 向后兼容别名(历史私有名)
    _align_series = align_series

    def get_norm_coeffs(self) -> dict:
        """获取归一化系数"""
        return self._norm_coeffs

    def get_pnl_coeffs(self) -> dict:
        """获取PnL系数"""
        return self._pnl_coeffs

    @property
    def pnl_scale(self) -> float:
        """总PnL缩放系数"""
        return self._pnl_scale
