"""cta_turtle 结构化配置定义"""
from dataclasses import dataclass, field


DIRECTION_BOTH = "both"
DIRECTION_LONG = "long"
DIRECTION_SHORT = "short"
VALID_DIRECTIONS = (DIRECTION_BOTH, DIRECTION_LONG, DIRECTION_SHORT)

SYSTEM_S1 = "S1"
SYSTEM_S2 = "S2"
VALID_SYSTEMS = (SYSTEM_S1, SYSTEM_S2)

VALID_PERIODS = ("d1", "h1")


@dataclass
class TurtleBaseConfig:
    """全局海龟参数"""
    unit_var: float = 200000.0       # 1 个交易单位的 VaR 预算(CNY, 按 1N 口径)
    atr_period: int = 20             # N = ATR(20), Wilder 平滑
    stop_atr_mult: float = 2.0       # 止损 = 最后加仓价 ∓ stop_atr_mult × N
    pyramid_layers: int = 4          # 最大加仓层数, 1 = 不加仓
    pyramid_step_atr: float = 0.5    # 每盈利 pyramid_step_atr × N 加一层
    bar_cnt: int = 120               # 订阅 K 线数量(需 > 55 + atr_period)
    s1_entry: int = 20               # S1 入场通道
    s1_exit: int = 10                # S1 出场通道
    s2_entry: int = 55               # S2 入场通道(兼 S1 fail-safe)
    s2_exit: int = 20                # S2 出场通道


@dataclass
class InstrumentConfig:
    """单品种配置"""
    code: str                        # e.g. SHFE.rb.HOT
    period: str = "d1"               # d1 | h1
    system: str = SYSTEM_S1          # S1 | S2
    units: int = 1                   # 期望交易单位数(每层 VaR = units × unit_var)
    direction: str = DIRECTION_BOTH  # both | long | short

    def allows_direction(self, direction: int) -> bool:
        """direction: 1=多, -1=空"""
        if self.direction == DIRECTION_BOTH:
            return True
        if self.direction == DIRECTION_LONG:
            return direction > 0
        return direction < 0


@dataclass
class TurtleConfig:
    """完整策略配置"""
    base: TurtleBaseConfig = field(default_factory=TurtleBaseConfig)
    instruments: list = field(default_factory=list)  # list[InstrumentConfig]
