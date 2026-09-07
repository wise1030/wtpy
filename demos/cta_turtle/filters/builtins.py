"""内置过滤器实现

- DirectionGateFilter : 方向闸 (自 StraTurtle._execute 硬编码迁移, 行为不变)
- AdxTrendFilter      : ADX 趋势强度下限 (震荡市否决趋势信号)
- VolatilityBandFilter: N(ATR) 处于近窗分位带内才交易 (极端波动回避)
- LiquidityFloorFilter: 成交量/持仓量下限 (流动性不足否决)

所有过滤器 fail-open/fail-closed 策略通过 on_warmup 参数显式声明。
"""
import numpy as np

from filters.base import (SignalFilterBase, register_filter,
                          FilterContext, _finite)
from helpers.indicators import wilder_adx


@register_filter
class DirectionGateFilter(SignalFilterBase):
    """方向闸: 信号方向与配置不符 → 否决。

    由 StraTurtle 隐式前置装配 (来源 instruments[].direction),
    显式配置到 filters: 列表通常冗余, 保留以支持覆盖场景。
    """
    name = "DirectionGate"

    def _setup(self, p: dict):
        self.direction = str(p.pop("direction", "both"))
        if self.direction not in ("both", "long", "short"):
            raise ValueError(
                f"DirectionGate direction 非法 '{self.direction}'")

    def allow_entry(self, ctx: FilterContext) -> tuple:
        d = ctx.sig_dir
        if self.direction == "both":
            return True, ""
        ok = d > 0 if self.direction == "long" else d < 0
        return ok, "" if ok else f"gate={self.direction}"


@register_filter
class AdxTrendFilter(SignalFilterBase):
    """ADX(period) >= threshold 才放行 (趋势强度过滤)"""
    name = "AdxTrend"

    def _setup(self, p: dict):
        self.period = int(p.pop("period", 14))
        self.threshold = float(p.pop("threshold", 25.0))
        self.on_warmup = str(p.pop("on_warmup", "allow"))
        if not 2 <= self.period <= 200:
            raise ValueError(f"AdxTrend period 越界 {self.period}")
        if not 0.0 <= self.threshold <= 100.0:
            raise ValueError(f"AdxTrend threshold 越界 {self.threshold}")
        if self.on_warmup not in ("allow", "deny"):
            raise ValueError(
                f"AdxTrend on_warmup 非法 '{self.on_warmup}'")

    def allow_entry(self, ctx: FilterContext) -> tuple:
        _, _, adx = wilder_adx(ctx.highs, ctx.lows, ctx.closes, self.period)
        v = float(adx[-1]) if adx is not None and len(adx) else float("nan")
        if not np.isfinite(v):
            return (self.on_warmup == "allow"), "adx_warmup"
        if v >= self.threshold:
            return True, ""
        return False, f"adx={v:.1f}<{self.threshold:g}"


@register_filter
class VolatilityBandFilter(SignalFilterBase):
    """最新 N 在其 trailing window 分位 [min_pct, max_pct] 内才放行。

    rank 含当前值本身, 故 rank∈[1/(k+1), 1]; min_pct 取值注意该下界。
    """
    name = "VolatilityBand"

    def _setup(self, p: dict):
        self.window = int(p.pop("window", 250))
        self.min_pct = float(p.pop("min_pct", 0.10))
        self.max_pct = float(p.pop("max_pct", 0.90))
        self.min_samples = int(p.pop("min_samples", 50))
        self.on_warmup = str(p.pop("on_warmup", "allow"))
        if not 20 <= self.window <= 2500:
            raise ValueError(f"VolatilityBand window 越界 {self.window}")
        if not 0.0 <= self.min_pct < self.max_pct <= 1.0:
            raise ValueError("VolatilityBand 分位带非法: "
                             f"[{self.min_pct}, {self.max_pct}]")
        if self.on_warmup not in ("allow", "deny"):
            raise ValueError(
                f"VolatilityBand on_warmup 非法 '{self.on_warmup}'")

    def allow_entry(self, ctx: FilterContext) -> tuple:
        s = ctx.n_series
        cur = ctx.n
        if s is None or not _finite(cur) or len(s) == 0:
            return (self.on_warmup == "allow"), "vol_no_data"
        w = np.asarray(s[-self.window:], dtype=float)
        w = w[np.isfinite(w)]
        if len(w) < max(self.min_samples, 2):
            return (self.on_warmup == "allow"), "vol_warmup"
        rank = float(np.mean(w <= cur))     # 含自身
        if self.min_pct <= rank <= self.max_pct:
            return True, ""
        return False, f"vol_rank={rank:.2f}~[{self.min_pct:g},{self.max_pct:g}]"


@register_filter
class LiquidityFloorFilter(SignalFilterBase):
    """近 k 根均成交量 >= min_volume 且 最新持仓量 >= min_oi 才放行。

    数据缺失(volumes/OI 为 None)时按 on_warmup 策略处理。
    """
    name = "LiquidityFloor"

    def _setup(self, p: dict):
        self.k = int(p.pop("k", 5))
        self.min_volume = float(p.pop("min_volume", 0.0))
        self.min_oi = p.pop("min_oi", None)
        if self.min_oi is not None:
            self.min_oi = float(self.min_oi)
        self.on_warmup = str(p.pop("on_warmup", "allow"))
        if not 1 <= self.k <= 120:
            raise ValueError(f"LiquidityFloor k 越界 {self.k}")
        if self.min_volume < 0 or \
                (self.min_oi is not None and self.min_oi < 0):
            raise ValueError("LiquidityFloor 下限不能为负")

    def allow_entry(self, ctx: FilterContext) -> tuple:
        vols = ctx.volumes
        if vols is None or len(vols) == 0:
            return (self.on_warmup == "allow"), "liq_no_volume"
        tail = np.asarray(vols[-self.k:], dtype=float)
        tail = tail[np.isfinite(tail)]
        if len(tail) < max(1, self.k // 2):
            return (self.on_warmup == "allow"), "liq_warmup"
        avg_vol = float(np.mean(tail))
        if avg_vol < self.min_volume:
            return False, f"vol={avg_vol:.0f}<{self.min_volume:g}"
        if self.min_oi is not None:
            oi = ctx.open_interests
            last_oi = float(oi[-1]) if oi is not None and len(oi) \
                else float("nan")
            if not np.isfinite(last_oi):
                return (self.on_warmup == "allow"), "liq_no_oi"
            if last_oi < self.min_oi:
                return False, f"oi={last_oi:.0f}<{self.min_oi:g}"
        return True, ""
