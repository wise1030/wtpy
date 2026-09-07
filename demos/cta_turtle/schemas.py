"""cta_turtle 结构化配置定义"""
from dataclasses import dataclass, field


DIRECTION_BOTH = "both"
DIRECTION_LONG = "long"
DIRECTION_SHORT = "short"
VALID_DIRECTIONS = (DIRECTION_BOTH, DIRECTION_LONG, DIRECTION_SHORT)

SYSTEM_S1 = "S1"
SYSTEM_S2 = "S2"
SYSTEM_DUAL = "S1+S2"
VALID_SYSTEMS = (SYSTEM_S1, SYSTEM_S2, SYSTEM_DUAL)

VALID_PERIODS = ("d1", "h1")
# 盘中止损副K周期 (主K切换为该周期, d1/h1 仅作信号源)
VALID_SUB_PERIODS = ("m5", "m1")


@dataclass
class FilterConfig:
    """instruments[].filters[] 项: 信号过滤器声明"""
    type: str                        # 过滤器类型 (filters 注册表键)
    params: dict = field(default_factory=dict)


@dataclass
class RiskConfig:
    """组合级风险预算 (TurtleConfig.risk)"""
    max_risk_per_mkt: float = 0.02     # 单品种本笔 2N 潜亏/权益上限
    max_total_risk_frac: float = 0.06  # 组合在途 2N 总潜亏/权益上限
    max_margin_usage: float = 0.60     # 预估保证金占用/权益上限
    margin_rate: float = 0.12          # 保证金率预估
    max_units_per_group: int = 2       # 相关性组同时持仓单位数上限(0=不限)
    max_units_total: int = 4           # 全组合同时持仓单位数上限(0=不限)
    fallback_capital: float = 5000000.0

    def validate(self):
        for name in ("max_risk_per_mkt", "max_total_risk_frac",
                     "max_margin_usage"):
            v = getattr(self, name)
            if not 0.0 < v <= 1.0:
                raise ValueError(f"risk.{name}={v} 需在 (0,1] 内")
        if not 0.0 < self.margin_rate <= 1.0:
            raise ValueError(f"risk.margin_rate={self.margin_rate} 非法")
        if self.max_units_per_group < 0 or self.max_units_total < 0:
            raise ValueError("risk.max_units_* 不能为负")
        if self.fallback_capital <= 0:
            raise ValueError("risk.fallback_capital 必须为正")


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
    enable_s1_filter: bool = True    # S1 盈利跳过过滤开关(False=裸 20/10)
    recon_patience: int = 3          # 运行时对账: 连续失配 N 根 bar 后重建
    intrabar_period: str = "m5"      # 盘中止损主K周期 (m5|m1, 配合 intrabar_stop)
    intrabar_cnt: int = 300          # 盘中主K订阅数 (覆盖跨日扫描窗口)
    journal_dir: str = ""            # K线旁路日志目录 (空=关闭; 见 helpers/BarJournal)
    entry_mode: str = "close"        # close=收盘确认 | trigger=盘中触发(方案C,
                                     # 经典海龟原生; 回测经条件单按触发价成交)


@dataclass
class InstrumentConfig:
    """单品种配置"""
    code: str                        # e.g. SHFE.rb.HOT
    period: str = "d1"               # d1 | h1
    system: str = SYSTEM_S1          # S1 | S2 | S1+S2 (双系统各半 units 并行)
    units: int = 1                   # 期望交易单位数(每层 VaR = units × unit_var)
    direction: str = DIRECTION_BOTH  # both | long | short
    group: str = ""                  # 相关性组标签(如 black/cheme; 空=不参与组限)
    filters: list = field(default_factory=list)   # list[FilterConfig]
    intrabar_stop: bool = False      # 盘中止损 (主K切换为 base.intrabar_period)

    def allows_direction(self, direction: int) -> bool:
        """direction: 1=多, -1=空"""
        if self.direction == DIRECTION_BOTH:
            return True
        if self.direction == DIRECTION_LONG:
            return direction > 0
        return direction < 0


@dataclass
class NotifyConfig:
    """notify 配置段 (webhook 事件推送)"""
    enabled: bool = False
    type: str = "generic"            # wechat | dingtalk | generic
    urls: list = field(default_factory=list)
    cooldown: float = 300.0          # 同事件 key 冷却秒数


@dataclass
class TurtleConfig:
    """完整策略配置"""
    base: TurtleBaseConfig = field(default_factory=TurtleBaseConfig)
    instruments: list = field(default_factory=list)  # list[InstrumentConfig]
    risk: RiskConfig = field(default_factory=RiskConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
