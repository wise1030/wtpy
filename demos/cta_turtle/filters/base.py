"""可插拔信号过滤器框架 (设计目标 P0-1)

定位: 在 TurtleEngine 产生 DEC_ENTER 决策之后、下单之前链式调用,
实现"可配置的辅助业务逻辑信号过滤"。

约定 (AGENTS.md 开发约定 3):
- 过滤器只读: bars(OHLCV)/指标序列/引擎只读快照, 禁止修改引擎状态;
- 只返回 (是否放行, 否决原因), 否决原因由策略层分级记录;
- 过滤器应无状态或自持状态(每 bar 由输入数据重算), 不依赖框架对象;
  因此回测与实盘行为天然一致, 且无需持久化。
"""
import numpy as np


class FilterContext:
    """单 bar 只读数据上下文 (传给每个过滤器的入参)。

    字段可能为 None (数据不可用时过滤器须自行 fail-open/fail-closed,
    并在否决原因中注明), 数组均为最新在尾部的 numpy 序列。
    """
    __slots__ = ("code", "sig_dir", "price", "n", "n_series",
                 "highs", "lows", "closes", "volumes", "open_interests")

    def __init__(self, code: str = "", sig_dir: int = 0,
                 price: float = float("nan"), n: float = float("nan"),
                 n_series=None, highs=None, lows=None, closes=None,
                 volumes=None, open_interests=None):
        self.code = code
        self.sig_dir = sig_dir          # 本次信号方向 +1/-1
        self.price = price              # 信号价 (bar close)
        self.n = n                      # 最新 ATR/N
        self.n_series = n_series        # ATR 序列
        self.highs = highs
        self.lows = lows
        self.closes = closes
        self.volumes = volumes
        self.open_interests = open_interests   # 无 OI 数据时为 None

    def with_direction(self, sig_dir: int) -> "FilterContext":
        """同 bar 多方向复用 (引擎同 bar 可能先 exit 再反向 enter)"""
        self.sig_dir = sig_dir
        return self


class SignalFilterBase:
    """过滤器协议。子类须设置类属性 name 并注册 (@register_filter)。"""
    name = "base"

    def __init__(self, params: dict = None):
        p = dict(params or {})
        self._setup(p)
        if p:
            raise ValueError(
                f"filter {self.name} 含未知参数: {sorted(p)}")

    def _setup(self, p: dict):
        """解析并校验参数, 从 p 中 pop 已消费键; 剩余键报错"""
        raise NotImplementedError

    def allow_entry(self, ctx: FilterContext) -> tuple:
        """判定是否放行本次入场信号。

        Returns:
            (allow: bool, reason: str) — 放行时 reason 为 "",
            否决时 reason 为简短英文标识(可含数值, 进日志)。
        """
        raise NotImplementedError

    def snapshot(self):
        """持久化快照; 默认无状态返回 None"""
        return None


_FILTER_REGISTRY = {}


def register_filter(cls):
    """注册装饰器: 按 cls.name 注册到全局表"""
    if not getattr(cls, "name", None) or cls.name == "base":
        raise ValueError(f"{cls.__name__} 缺有效 name")
    _FILTER_REGISTRY[cls.name] = cls
    return cls


def get_filter_class(name: str):
    if name not in _FILTER_REGISTRY:
        raise ValueError(
            f"未知过滤器类型 '{name}', 可用: {sorted(_FILTER_REGISTRY)}")
    return _FILTER_REGISTRY[name]


def known_filters() -> list:
    return sorted(_FILTER_REGISTRY)


def _finite(x) -> bool:
    return x is not None and np.isfinite(x)
