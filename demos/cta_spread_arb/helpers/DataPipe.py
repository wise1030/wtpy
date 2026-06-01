"""
DataPipe — 数据获取与价差计算模块

从SpreadArbStrategy._process_sub中提取，封装:
1. K线数据获取(bars_data, aux_bars)
2. 价差计算(price_spread / coint_spread)
3. 日线趋势过滤(spread_trend_filter)
4. 返回结构化BarData供下游使用

优化:
- min_len硬编码30→从SubStrategyConfig.min_bar_length读取(默认20)
- update_bands冗余调用清理: 只在fetch()末尾调用一次
- bollinger参数(lookback/threshold)从SubStrategyConfig读取
"""
import numpy as np
from schemas import BarData


class DataPipe:
    """
    数据管道: 从context获取K线 → 计算价差 → 判断趋势

    职责单一: 只做数据获取和预处理，不做交易决策
    """

    def __init__(self, sub_config, spread_calc, factor_engine=None, exec_cfg=None):
        """
        Args:
            sub_config: SubStrategyConfig — 结构化子策略配置
            spread_calc: SpreadCalculator实例
            factor_engine: FactorEngine实例(coint模式需要)
            exec_cfg: ExecConfig — 执行参数配置(P1-2)
        """
        self._config = sub_config
        self._spread_calc = spread_calc
        self._factor_engine = factor_engine
        # P1-2: 从ExecConfig读取bollinger/min_bar_length参数
        if exec_cfg is not None:
            self._bollinger_lookback = exec_cfg.bollinger_lookback
            self._bollinger_threshold = exec_cfg.bollinger_threshold
            self._min_bar_length = exec_cfg.min_bar_length
        else:
            # 回退: 从sub_config读取(兼容旧调用方式)
            self._bollinger_lookback = getattr(sub_config, 'bollinger_lookback', 60)
            self._bollinger_threshold = getattr(sub_config, 'bollinger_threshold', 2.0)
            self._min_bar_length = getattr(sub_config, 'min_bar_length', 20)

    def fetch(self, context, cur_date: int) -> BarData:
        """
        获取并预处理数据

        Returns:
            BarData结构化对象，数据不足时返回None
        """
        sub = self._config
        sub_name = sub.name

        # 1. 获取主周期K线
        bars_data = {}
        for leg in sub.legs:
            bars = context.stra_get_bars(
                leg.code, sub.main_period,
                sub.main_bar_cnt, isMain=False)
            if bars is None or len(bars) == 0:
                context.stra_log_text(
                    f"{sub_name} no bars for {leg.code}@{sub.main_period}, "
                    f"count={0 if bars is None else len(bars)}")
                return None
            bars_data[leg.code] = bars

        # 2. 获取辅助周期K线
        aux_bars = {}
        for aux in sub.aux_periods:
            for leg in sub.legs:
                bars = context.stra_get_bars(
                    leg.code, aux['period'],
                    aux['bar_cnt'], isMain=False)
                if bars is not None and len(bars) > 0:
                    key = f"{leg.code}#{aux['period']}"
                    aux_bars[key] = bars

        # 3. 提取价格和成交量
        prices = {}
        volumes = {}
        min_len = None
        for code, bars in bars_data.items():
            closes = bars.closes
            vols = bars.volumes
            prices[code] = closes
            volumes[code] = vols
            if min_len is None or len(closes) < min_len:
                min_len = len(closes)

        min_required = self._min_bar_length
        if min_len is None or min_len < min_required:
            bar_info = {code: len(bars_data[code].closes) for code in bars_data}
            context.stra_log_text(
                f"{sub_name} bars insufficient: {bar_info}, min={min_len}, "
                f"need>={min_required}")
            return None

        # 4. 计算价差
        spread_calc = self._spread_calc
        spread_mode = sub.spread_mode
        beta = None

        if spread_mode == 'coint':
            spread, beta = self._calc_coint_spread(
                prices, volumes, aux_bars, cur_date, spread_calc)
        else:
            spread = spread_calc.calc_price_spread(prices, volumes)

        if spread is None or len(spread) < 2:
            return None

        # 5. 唯一的update_bands调用 — 移除_calc_coint_spread中的冗余调用
        spread_calc.update_bands(spread, lookback=self._bollinger_lookback,
                                 threshold=self._bollinger_threshold)

        # 6. 计算日线趋势过滤
        spread_trend_filter = self._calc_trend_filter(
            spread, aux_bars, spread_calc, beta)

        return BarData(
            prices=prices,
            volumes=volumes,
            aux_bars=aux_bars,
            spread=spread,
            spread_trend_filter=spread_trend_filter,
            beta=beta,
            min_len=min_len,
        )

    def _calc_coint_spread(self, prices, volumes,
                           aux_bars, cur_date, spread_calc):
        """
        coint模式价差计算 — 已修复双重计算+冗余update_bands

        流程: refit_coint(仅触发OLS+ADF) → 获取beta/c → 计算coint_spread
        不再调用fe.calculate()两次，避免_rank_window污染
        不再在中间路径调用update_bands(统一由fetch()末尾调用)
        """
        fe = self._factor_engine

        price_spread = spread_calc.calc_price_spread(prices, volumes)
        if price_spread is None or len(price_spread) < 2:
            return None, None

        # 仅触发协整refit(不计算因子，不污染_rank_window)
        fe.refit_coint(
            price_spread, prices=prices, volumes=volumes,
            aux_bars=aux_bars, cur_date=cur_date,
            norm_coeffs=spread_calc.get_norm_coeffs())

        # 获取协整参数
        beta, c, _ = fe.get_coint_params()
        if beta is None or c is None:
            # 回退到price spread, beta=None标识非协整
            # 不再调用update_bands — 由fetch()末尾统一调用
            return price_spread, None

        # 计算协整价差
        coint_spread = spread_calc.calc_coint_spread(prices, beta=beta, c=c)

        if coint_spread is not None and len(coint_spread) >= 2:
            # 不再调用update_bands — 由fetch()末尾统一调用
            return coint_spread, beta

        # 回退到price spread
        # 不再调用update_bands — 由fetch()末尾统一调用
        return price_spread, None

    def _calc_trend_filter(self, spread, aux_bars, spread_calc, beta):
        """
        计算趋势过滤方向 — 日线MA10判断

        逻辑: 前一日K线收盘价在日线MA10之上 → 长期看涨(+1)
              前一日K线收盘价在日线MA10之下 → 长期看空(-1)
              前一日K线收盘价等于MA10 → 无方向(0)

        用价差的日线数据计算MA10, 前一日价差在MA10之上为看涨。
        无日线价差数据时回退到5分钟价差MA判断。
        """
        spread_trend_filter = 0

        # 方法1: 用价差的日线数据计算MA10
        d1_spread = self._extract_aux_spread(aux_bars, '#d1', spread_calc)
        if d1_spread is not None and len(d1_spread) >= 11:
            ma10 = np.mean(d1_spread[-10:])
            # 前一日价差收盘 = d1_spread倒数第2根(倒数第1根可能是当日未完成)
            prev_close = d1_spread[-2] if len(d1_spread) >= 12 else d1_spread[-1]
            if prev_close > ma10:
                spread_trend_filter = 1
            elif prev_close < ma10:
                spread_trend_filter = -1
            return spread_trend_filter

        # 回退: 无日线价差数据时用5分钟价差MA
        if len(spread) >= 40:
            ma10_m5 = np.mean(spread[-10:])
            ma40_m5 = np.mean(spread[-40:])
            std40 = np.std(spread[-40:])
            if std40 > 0:
                slope_z = (ma10_m5 - ma40_m5) / std40
                if slope_z > 1.0:
                    spread_trend_filter = 1
                elif slope_z < -1.0:
                    spread_trend_filter = -1

        return spread_trend_filter

    def _extract_aux_spread(self, aux_bars, period_tag, spread_calc):
        """从aux_bars中提取指定周期的价差序列

        Args:
            aux_bars: 辅助K线数据dict
            period_tag: 周期标记(如'#h1', '#d1')
            spread_calc: SpreadCalculator实例

        Returns:
            价差序列或None
        """
        closes = {}
        for key, ab in aux_bars.items():
            if period_tag in key:
                code = key.split('#')[0]
                closes[code] = ab.closes

        if len(closes) >= 2:
            spread = spread_calc.calc_spread(closes, mode='price')
            if spread is not None and len(spread) >= 20:
                return spread
        return None