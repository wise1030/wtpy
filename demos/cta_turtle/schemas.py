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
    # 共享账户可用资金闸门 (P1, 需交易通道真值源):
    # 9/9 实盘事故: 账户被多策略共用, 策略侧只能看到自己的权益,
    # 资金耗尽后 p 追单 3357 次全被拒。真值源 rtdata.json 的 funds.available
    # 是券商口径可用资金 → 下单前按本笔预估保证金做缩量/否决。
    # off=不检查; warn=只告警不改变手数 (默认, 先观察); veto=缩量优先, 归零否决
    account_gate_mode: str = "warn"
    account_reserve_frac: float = 0.20  # 保留的可用资金比例 (安全垫)

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
        if self.account_gate_mode not in ("off", "warn", "veto"):
            raise ValueError(
                f"risk.account_gate_mode 非法 '{self.account_gate_mode}', "
                "仅支持 off|warn|veto")
        if not 0.0 <= self.account_reserve_frac < 1.0:
            raise ValueError("risk.account_reserve_frac 需在 [0,1) 内")


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
    stop_n_mode: str = "ratchet"     # 止损/加仓步长的 N 口径 (P0-2):
                                     # ratchet=当根 N 但只收紧不放宽 (默认;
                                     #   两次加仓之间止损单调向有利方向,
                                     #   加仓时按新单位重锚)
                                     # entry  =入/加仓时点冻结 N (纯经典口径,
                                     #   与风控 2N 记账严格一致)
                                     # current=当根 N (2026-09-14 前行为;
                                     #   N 放大时止损被放宽, 实际止损亏损
                                     #   可超风控记账 2 倍 — 仅供 A/B/回退)
    truth_json: str = ""             # 交易通道真值源 (真实持仓/资金):
                                     # 空=自动探测 generated/traders/<id>/
                                     # rtdata.json; "off"=关闭; 其他=显式
                                     # 路径 (回测/无部署环境自动降级)
    truth_max_age: float = 1800.0    # 真值文件时效阈值(秒): 超过视为陈旧,
                                     # 回退框架口径 (陈旧券商仓位不得触发
                                     # 对账重建/资金闸门)
    # 真值用途 (2026-09-15 生产实测后新增):
    #   alert  = 只观测/告警 (默认)。券商持仓是**账户级、非策略级**,
    #            共享账户下同一合约可能含其他策略仓位, 无法归属 → 不得
    #            用它重建/收养我们自己的持仓 (实测 TA: 账户 112 vs 我们 64)
    #   rebuild= 允许按真值对账重建/收养 (仅当本策略独占该账户时使用)
    truth_rebuild_mode: str = "alert"
    # ghost_fill 自愈 (2026-09-17 CF 34 手幽灵仓事故):
    #   真值源确认"订单未到柜台"后, 自动把信号层从尾部裁剪到与券商
    #   实际持仓同向且不超过它的手数 (只缩量绝不放大, 对共享账户
    #   安全: 只丢弃本策略未确认的层, 不按券商绝对值建仓)。
    #   True=自动裁剪+告警; False=只告警 (旧行为, 人工处置)
    ghost_auto_trim: bool = True
    # 涨跌停锁死检测 (秒, 0=关闭): tick 价格钉住不动超过该时长且
    # 存在未闭合成交窗口时, 判定疑似锁死 → 告警并暂停该品种加仓
    # (出场/止损不受影响; 入场仍允许但由 ghost_fill 自愈兜底)。
    # 有界实现: 只依赖"价格钉住"单一信号, 无 tick 时不触发。
    limit_lock_secs: int = 120


@dataclass
class SpeedConfig:
    """instruments[].speeds[] 项: 一个"速度"(通道窗口组合, 2026-09-16)

    多速度设计 (Man AHL & 文献): 每档速度 = 一个独立子系统, 各自通道窗口,
    共享该品种的风险预算 (share 之和 = 1), 框架层净额化后一次下单。
    """
    name: str                        # 唯一标识 (日志/快照键)
    entry: int                       # 入场通道窗口
    exit: int                        # 出场通道窗口
    share: float = 1.0               # 该速度占品种风险预算的份额
    fs: int = 55                     # fail-safe 通道 (S1 过滤器强制入场)


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
    speeds: list = field(default_factory=list)   # list[SpeedConfig];
                                     # 非空时忽略 system, 按速度列表装配子系统

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
