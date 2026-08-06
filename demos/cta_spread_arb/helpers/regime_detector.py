"""
regime_detector.py — 市场状态检测器

普适性设计: 不依赖特定品种对或频率
基于多维度指标综合判断当前regime:
- Hurst指数: H>0.5趋势, H<0.45均值回归, 中间为震荡
- ADF检验: p<0.05平稳(MR), p>0.1非平稳(趋势)
- ADX趋势强度: ADX>25趋势, ADX<20震荡
- 波动率状态: 波动率聚类判断regime切换

输出: RegimeResult(regime, confidence, direction)
- regime: "trend" / "mr" / "range"
- confidence: 0.0-1.0, 多维度一致性越高越自信
- direction: 1/-1/0, 趋势方向(仅trend regime有意义)

有状态: regime记忆避免频繁切换(decay-not-block原则)
"""
import numpy as np
from collections import Counter
from dataclasses import dataclass
from typing import Optional


@dataclass
class RegimeResult:
    """Regime检测结果"""
    regime: str = 'range'        # trend / mr / range
    confidence: float = 0.0      # 0.0-1.0
    direction: int = 0           # 趋势方向(1/-1/0)
    hurst: float = 0.5           # 缓存Hurst值
    adf_p: float = 1.0           # 缓存ADF p值
    adx: float = 0.0             # 缓存ADX值

    def to_dict(self) -> dict:
        return {
            'regime': self.regime,
            'confidence': self.confidence,
            'direction': self.direction,
            'hurst': self.hurst,
            'adf_p': self.adf_p,
            'adx': self.adx,
        }


class RegimeDetector:
    """
    市场状态检测器

    多维度综合判断:
    1. Hurst指数 — 长记忆性
    2. ADF检验 — 平稳性
    3. ADX — 趋势强度
    4. MA斜率 — 趋势方向

    投票机制: 各维度独立判断, 加权投票决定最终regime
    regime记忆: 防止噪声导致频繁切换(decay-not-block)
    """

    def __init__(self, params: dict = None):
        p = params or {}
        # Hurst参数
        self._hurst_window = p.get('hurst_window', 200)
        self._hurst_trend_thresh = p.get('hurst_trend_thresh', 0.52)
        self._hurst_mr_thresh = p.get('hurst_mr_thresh', 0.48)
        # ADF参数
        self._adf_mr_thresh = p.get('adf_mr_thresh', 0.05)
        self._adf_trend_thresh = p.get('adf_trend_thresh', 0.15)
        # P3-6: adfuller maxlag 参数化, 默认 1 与 stat_factors._ols_coint 对齐
        # statsmodels 默认 maxlag=10 会做 10+ 次 OLS 拟合, 是 detect_regime 主要热点;
        # m5 周期 + spread 序列特性下 maxlag=1 足够, 节省 ~40% adfuller 耗时
        self._adf_maxlag = int(p.get('adf_maxlag', 1))
        # ADX参数
        self._adx_period = p.get('adx_period', 14)
        self._adx_trend_thresh = p.get('adx_trend_thresh', 25)
        self._adx_range_thresh = p.get('adx_range_thresh', 20)
        # MA斜率参数
        self._ma_fast = p.get('ma_fast', 10)
        self._ma_slow = p.get('ma_slow', 30)
        # regime记忆参数
        self._regime_persist_bars = p.get('regime_persist_bars', 5)
        self._regime_min_confidence = p.get('regime_min_confidence', 0.4)
        # 投票权重
        self._weight_hurst = p.get('weight_hurst', 0.3)
        self._weight_adf = p.get('weight_adf', 0.25)
        self._weight_adx = p.get('weight_adx', 0.25)
        self._weight_ma = p.get('weight_ma', 0.2)
        # 内部状态
        self._regime_history = []
        self._last_regime = 'range'
        self._last_direction = 0
        # Hurst/ADF缓存: 统计量变化慢, detect仍每bar执行投票和平滑
        # P3-6: adf_cache_every 默认 24 (m5周期=2小时), 显著降低 adfuller 调用频次;
        # Hurst 计算更轻, 保持 every=1 不变以保证投票时效性。
        # 用户显式配置可覆盖默认值以平衡性能/灵敏度。
        self._hurst_cache_every = max(1, int(p.get('hurst_cache_every', 1)))
        self._adf_cache_every = max(1, int(p.get('adf_cache_every', 24)))
        self._detect_count = 0
        self._cached_hurst = None
        self._cached_adf_p = None
        self._last_hurst_calc_count = -1
        self._last_adf_calc_count = -1

    def detect(self, spread: np.ndarray) -> RegimeResult:
        """
        检测当前regime

        Args:
            spread: 价差序列(至少hurst_window长度)

        Returns:
            RegimeResult
        """
        if len(spread) < max(self._hurst_window, 60):
            return RegimeResult(regime='range', confidence=0.0)

        # 1. Hurst指数
        hurst = self._get_cached_hurst(spread)
        hurst_vote, hurst_conf = self._hurst_vote(hurst)

        # 2. ADF检验
        adf_p = self._get_cached_adf(spread)
        adf_vote, adf_conf = self._adf_vote(adf_p)

        # 3. ADX趋势强度
        adx = self._calc_adx(spread)
        adx_vote, adx_conf = self._adx_vote(adx)

        # 4. MA斜率方向
        ma_dir, ma_vote, ma_conf = self._ma_vote(spread)

        # 加权投票
        votes = {
            'trend': (hurst_vote.get('trend', 0) * self._weight_hurst +
                      adf_vote.get('trend', 0) * self._weight_adf +
                      adx_vote.get('trend', 0) * self._weight_adx +
                      ma_vote.get('trend', 0) * self._weight_ma),
            'mr': (hurst_vote.get('mr', 0) * self._weight_hurst +
                   adf_vote.get('mr', 0) * self._weight_adf +
                   adx_vote.get('mr', 0) * self._weight_adx +
                   ma_vote.get('mr', 0) * self._weight_ma),
            'range': (hurst_vote.get('range', 0) * self._weight_hurst +
                      adf_vote.get('range', 0) * self._weight_adf +
                      adx_vote.get('range', 0) * self._weight_adx +
                      ma_vote.get('range', 0) * self._weight_ma),
        }

        # 综合置信度
        total_conf = (hurst_conf * self._weight_hurst +
                      adf_conf * self._weight_adf +
                      adx_conf * self._weight_adx +
                      ma_conf * self._weight_ma)

        # 选出最高票regime
        best_regime = max(votes, key=votes.get)
        best_score = votes[best_regime]

        # 置信度 = 最高票占比 * 综合置信度
        total_votes = sum(votes.values())
        confidence = (best_score / total_votes * total_conf) if total_votes > 0 else 0.0

        # 趋势方向(仅trend regime有意义)
        direction = ma_dir if best_regime == 'trend' else 0

        # regime记忆: 防止频繁切换
        self._regime_history.append(best_regime)
        if len(self._regime_history) > self._regime_persist_bars:
            self._regime_history = self._regime_history[-self._regime_persist_bars:]

        # 用历史投票平滑regime
        counts = Counter(self._regime_history)
        smoothed_regime, smoothed_count = counts.most_common(1)[0]
        smooth_confidence = confidence * (smoothed_count / len(self._regime_history))

        # 如果平滑后regime与当前不同且置信度不够高, 保持上次regime
        # P1-4修复: regime回退时direction置0(保守处理), 不使用过时的_last_direction
        # 原问题: direction来自旧regime判断, 与当前实际regime矛盾,
        # 可能导致EntryJudgeStep中trend_blocked判断错误
        if smoothed_regime != self._last_regime and smooth_confidence < self._regime_min_confidence:
            smoothed_regime = self._last_regime
            direction = 0  # 保守: regime不确定时不做方向假设

        self._last_regime = smoothed_regime
        self._last_direction = direction
        self._detect_count += 1

        return RegimeResult(
            regime=smoothed_regime,
            confidence=min(1.0, smooth_confidence),
            direction=direction,
            hurst=hurst,
            adf_p=adf_p,
            adx=adx,
        )

    def _calc_hurst(self, spread: np.ndarray) -> float:
        """R/S法计算Hurst指数"""
        window = min(self._hurst_window, len(spread))
        ts = spread[-window:]
        max_lag = min(50, window // 4)
        if max_lag < 5:
            return 0.5
        lags = range(2, max_lag)
        tau = []
        for lag in lags:
            if lag >= len(ts):
                break
            diff = ts[lag:] - ts[:-lag]
            tau.append(np.std(diff))
        tau = [t for t in tau if t > 0]
        if len(tau) < 3:
            return 0.5
        try:
            valid_lags = list(range(2, 2 + len(tau)))
            poly = np.polyfit(np.log(valid_lags), np.log(tau), 1)
            return float(np.clip(poly[0], 0.3, 0.8))
        except Exception:
            return 0.5

    def _calc_adf(self, spread: np.ndarray) -> float:
        """ADF检验p值

        P3-6: maxlag 由 self._adf_maxlag 控制 (默认 1, 可 yaml 覆盖),
        替代原硬编码 maxlag=10。maxlag=10 时 adfuller 内部需做 ~11 次
        OLS 拟合 + autolag 模型选择, 是 detect_regime 主要瓶颈。
        """
        window = min(self._hurst_window, len(spread))
        ts = spread[-window:]
        try:
            from statsmodels.tsa.stattools import adfuller
            result = adfuller(ts, maxlag=self._adf_maxlag)
            return float(result[1])
        except Exception:
            return 1.0

    def _get_cached_hurst(self, spread: np.ndarray) -> float:
        """按bar间隔缓存Hurst指数，避免每bar重复做log-polyfit。"""
        if (self._cached_hurst is None or
                self._detect_count - self._last_hurst_calc_count >= self._hurst_cache_every):
            self._cached_hurst = self._calc_hurst(spread)
            self._last_hurst_calc_count = self._detect_count
        return float(self._cached_hurst)

    def _get_cached_adf(self, spread: np.ndarray) -> float:
        """按bar间隔缓存ADF p值，避免每bar重复调用statsmodels adfuller。"""
        if (self._cached_adf_p is None or
                self._detect_count - self._last_adf_calc_count >= self._adf_cache_every):
            self._cached_adf_p = self._calc_adf(spread)
            self._last_adf_calc_count = self._detect_count
        return float(self._cached_adf_p)

    def _calc_adx(self, spread: np.ndarray) -> float:
        """简化ADX计算"""
        period = self._adx_period
        if len(spread) < period * 3:
            return 0.0
        deltas = np.diff(spread[-period * 3:])
        up = np.where(deltas > 0, deltas, 0.0)
        down = np.where(deltas < 0, -deltas, 0.0)
        atr = np.mean(np.abs(deltas))
        if atr == 0:
            return 0.0
        plus_dm = np.mean(up)
        minus_dm = np.mean(down)
        plus_di = 100 * plus_dm / atr
        minus_di = 100 * minus_dm / atr
        di_sum = plus_di + minus_di
        if di_sum == 0:
            return 0.0
        dx = 100 * abs(plus_di - minus_di) / di_sum
        return dx

    def _hurst_vote(self, h: float) -> tuple:
        """Hurst投票: {regime: score}, confidence"""
        votes = {'trend': 0.0, 'mr': 0.0, 'range': 0.0}
        if h > self._hurst_trend_thresh:
            votes['trend'] = (h - 0.5) / 0.3  # H=0.8→1.0
            conf = min(1.0, (h - self._hurst_trend_thresh) / 0.1)
        elif h < self._hurst_mr_thresh:
            votes['mr'] = (0.5 - h) / 0.2  # H=0.3→1.0
            conf = min(1.0, (self._hurst_mr_thresh - h) / 0.1)
        else:
            votes['range'] = 1.0 - abs(h - 0.5) / 0.1
            conf = 0.5
        return votes, conf

    def _adf_vote(self, p: float) -> tuple:
        """ADF投票: p<0.05→MR, p>0.15→趋势, 中间→range"""
        votes = {'trend': 0.0, 'mr': 0.0, 'range': 0.0}
        if p < self._adf_mr_thresh:
            votes['mr'] = 1.0
            conf = min(1.0, (self._adf_mr_thresh - p) / self._adf_mr_thresh)
        elif p > self._adf_trend_thresh:
            votes['trend'] = 1.0
            conf = min(1.0, (p - self._adf_trend_thresh) / 0.2)
        else:
            votes['range'] = 1.0
            conf = 0.3
        return votes, conf

    def _adx_vote(self, adx: float) -> tuple:
        """ADX投票: ADX>25→趋势, ADX<20→range, 中间过渡"""
        votes = {'trend': 0.0, 'mr': 0.0, 'range': 0.0}
        if adx > self._adx_trend_thresh:
            votes['trend'] = min(1.0, (adx - self._adx_trend_thresh) / 15.0)
            conf = min(1.0, (adx - self._adx_trend_thresh) / 10.0)
        elif adx < self._adx_range_thresh:
            votes['range'] = 1.0
            votes['mr'] = 0.3  # 低ADX也可能是MR
            conf = 0.5
        else:
            # 过渡区
            t_frac = (adx - self._adx_range_thresh) / (self._adx_trend_thresh - self._adx_range_thresh)
            votes['trend'] = t_frac * 0.5
            votes['range'] = (1 - t_frac) * 0.5
            conf = 0.3
        return votes, conf

    def _ma_vote(self, spread: np.ndarray) -> tuple:
        """MA斜率投票: 方向+强度"""
        if len(spread) < self._ma_slow:
            return 0, {'trend': 0.0, 'mr': 0.0, 'range': 0.0}, 0.0
        fast_ma = np.mean(spread[-self._ma_fast:])
        slow_ma = np.mean(spread[-self._ma_slow:])
        std = np.std(spread[-self._ma_slow:])
        if std == 0:
            return 0, {'trend': 0.0, 'mr': 0.0, 'range': 0.5}, 0.0
        slope_z = (fast_ma - slow_ma) / std
        direction = 1 if slope_z > 0.3 else (-1 if slope_z < -0.3 else 0)
        abs_slope = abs(slope_z)
        # 强斜率→趋势, 弱斜率→range
        if abs_slope > 0.8:
            votes = {'trend': min(1.0, abs_slope), 'mr': 0.0, 'range': 0.0}
            conf = min(1.0, abs_slope / 2.0)
        elif abs_slope < 0.3:
            votes = {'trend': 0.0, 'mr': 0.2, 'range': 0.8}
            conf = 0.3
        else:
            votes = {'trend': abs_slope * 0.5, 'mr': 0.0, 'range': 0.3}
            conf = 0.3
        return direction, votes, conf

    def reset(self):
        """重置状态(供on_session_begin调用)"""
        self._regime_history.clear()
        self._last_regime = 'range'
        self._last_direction = 0
        self._detect_count = 0
        self._cached_hurst = None
        self._cached_adf_p = None
        self._last_hurst_calc_count = -1
        self._last_adf_calc_count = -1


def create_regime_detector(regime_cfg) -> RegimeDetector:
    """从配置创建RegimeDetector(原RegimeConfig.py合并至此)

    P2-3修复: 支持RegimeConfig dataclass和dict两种输入

    Args:
        regime_cfg: RegimeConfig dataclass或dict(来自strategy_config.yaml的regime节)
    Returns:
        RegimeDetector实例
    """
    # P2-3修复: 统一dataclass和dict访问
    def _get(cfg, key, default):
        if hasattr(cfg, key):
            return getattr(cfg, key, default)
        elif isinstance(cfg, dict):
            return cfg.get(key, default)
        return default

    # 映射YAML字段名到RegimeDetector内部参数名
    # C-2(2026-07): 补映射 adf_maxlag/cache_every/投票权重(此前只能改代码默认值)
    params = {
        'hurst_window': _get(regime_cfg, 'hurst_window', 100),
        'hurst_trend_thresh': _get(regime_cfg, 'trend_hurst_threshold', 0.55),
        'hurst_mr_thresh': _get(regime_cfg, 'mr_hurst_threshold', 0.45),
        'adx_period': _get(regime_cfg, 'adx_window', 14),
        'adx_trend_thresh': _get(regime_cfg, 'trend_adx_threshold', 25.0),
        'regime_persist_bars': _get(regime_cfg, 'regime_memory_bars', 5),
        'regime_min_confidence': _get(regime_cfg, 'regime_switch_cost', 0.4),
        'adf_maxlag': _get(regime_cfg, 'adf_maxlag', 1),
        'adf_cache_every': _get(regime_cfg, 'adf_cache_every', 24),
        'hurst_cache_every': _get(regime_cfg, 'hurst_cache_every', 1),
        'weight_hurst': _get(regime_cfg, 'weight_hurst', 0.3),
        'weight_adf': _get(regime_cfg, 'weight_adf', 0.25),
        'weight_adx': _get(regime_cfg, 'weight_adx', 0.25),
        'weight_ma': _get(regime_cfg, 'weight_ma', 0.2),
    }
    return RegimeDetector(params=params)
