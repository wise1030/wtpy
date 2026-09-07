"""filters — 可插拔信号过滤器包

用法 (config/turtle_config.yaml):
    instruments:
      - code: SHFE.rb.HOT
        direction: long            # 隐式 DirectionGate 来源
        filters:                   # 显式辅助过滤链 (按序链式判定)
          - type: AdxTrend
            params: {period: 14, threshold: 25}
          - type: VolatilityBand
            params: {window: 250, min_pct: 0.1, max_pct: 0.9}
"""
from filters.base import (FilterContext, SignalFilterBase,
                          register_filter, get_filter_class,
                          known_filters)
from filters import builtins   # noqa: F401  触发内置过滤器注册


def build_chain(inst) -> list:
    """装配单品种过滤链。

    规则: 隐式 DirectionGate(inst.direction) 恒为链首 (保持既有
    instruments[].direction 语义与行为), 显式 filters 列表依次追加。
    """
    chain = [get_filter_class("DirectionGate")(
        {"direction": getattr(inst, "direction", "both")})]
    for f in getattr(inst, "filters", []) or []:
        chain.append(get_filter_class(f.type)(dict(f.params)))
    return chain


__all__ = ["FilterContext", "SignalFilterBase", "register_filter",
           "get_filter_class", "known_filters", "build_chain"]
