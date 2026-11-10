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
    max_risk_per_mkt: float = 0.02     # 单品种**本笔** 2N 潜亏/权益上限
    # 单品种**累计** 2N 潜亏/权益上限 (2026-10-20)。经典海龟 4 层
    # × 每层 2N ≈ 8%，故默认 8%；旧实现只有"本笔"检查，4 层可做到
    # 8% 而配置里写的 2% 看起来像品种总预算 — 两个口径必须分开。
    max_risk_per_market: float = 0.08
    max_total_risk_frac: float = 0.06  # 组合在途 2N 总潜亏/权益上限
    max_margin_usage: float = 0.10     # 本策略保证金占用/自身权益上限 (方案B按策略收窄)
    margin_rate: float = 0.12          # 保证金率预估
    max_units_per_group: int = 2       # 相关性组同时持仓单位数上限(0=不限)
    max_units_total: int = 4           # 全组合同时持仓单位数上限(0=不限)
    fallback_capital: float = 15000000.0  # 动态权益无效时的回退基准资金
    # 资金分配 (2026-10-22 S1): 共享账户下 12 策略 × fallback 15M = 180M
    # 名义风险资本 vs 账户实际 ~24M, 风险预算是"每策略独占全账户"的虚高
    # 口径。portfolio_capital>0 时启用分配: 策略权益基数 =
    # portfolio_capital × weight/Σweight (alloc_weights 按 code, 缺省 1.0
    # 等权); fallback_capital 退化为"接口失效时的深度兜底"。
    # 0 = 关闭 (默认, 保持旧行为; 开启需先确定组合真实可用资金)。
    portfolio_capital: float = 0.0
    alloc_weights: dict = None           # {code: weight}; None/{} = 等权

    # 共享账户可用资金闸门 (P1, 需交易通道真值源):
    # 9/9 实盘事故: 账户被多策略共用, 策略侧只能看到自己的权益,
    # 资金耗尽后 p 追单 3357 次全被拒。真值源 rtdata.json 的 funds.available
    # 是券商口径可用资金 → 下单前按本笔预估保证金做缩量/否决。
    # off=不检查; warn=只告警不改变手数 (默认, 先观察); veto=缩量优先, 归零否决
    account_gate_mode: str = "warn"
    account_reserve_frac: float = 0.20  # 保留的可用资金比例 (安全垫)
    # 策略层日损闸 (2026-10-20): 相对当日首笔权益的回撤。
    # stop=停止新开/加仓; flatten=全部平仓 (只减不加)。0=关闭。
    # 默认关闭, 因为开启前必须先确定"单策略分配资金"; 否则 15M 基数
    # 下 3% = 45 万/策略, 12 个策略同时触发会远超账户承受力。
    daily_loss_stop_frac: float = 0.0
    daily_loss_flatten_frac: float = 0.0
    # 成交确认窗口超时 (Phase 0, 2026-11-09: 原 600s/1800s 硬编码分档)。
    # enter/add 用 fill_timeout_secs (默认 600=旧行为); 出场 (止损/通道/
    # 日损全平) 用 exit_fill_timeout_secs (默认 120 — 保护性订单延迟是
    # 真实风险, 2026-11-04 pp 止损 646s 才确认; 仅影响告警时机, 不改变
    # 任何成交或确认语义)。fill_confirmed 的 fees 兜底窗口 =
    # fill_timeout_secs (fees0>0) / 3x (免费品种, 旧 1800=600*3 等价)。
    fill_timeout_secs: float = 600.0
    exit_fill_timeout_secs: float = 120.0
    # === Phase 1 资本模型 v2 (2026-11-10, 用户拍板真实可支配 25M) ===
    # unit_var 派生: base.unit_var=0 时 1 单位 1N 风险 = 分配资金 ×
    # unit_risk_frac (默认 1%, 与 max_risk_per_mkt 2%×2N 自洽:
    # 1 unit 的 2N 止损 = 2% = 单笔上限, 不再出现 0.75 unit 截断)。
    # 分配资金 = portfolio_capital × weight/Σweights (alloc_weights)。
    # ⚠️ 观察期 (shadow_capital>0, portfolio_capital=0): 生产预算口径
    # 不变, 只按 25M 口径旁路计算对比手数并日志 — 验证后翻转:
    #   portfolio_capital=25M, base.unit_var=0, shadow_capital=0。
    unit_risk_frac: float = 0.01
    shadow_capital: float = 0.0     # shadow 观察期虚拟总资金 (0=关闭)

    def validate(self):
        for name in ("max_risk_per_mkt", "max_risk_per_market",
                     "max_total_risk_frac", "max_margin_usage"):
            v = getattr(self, name)
            if not 0.0 < v <= 1.0:
                raise ValueError(f"risk.{name}={v} 需在 (0,1] 内")
        if not 0.0 < self.margin_rate <= 1.0:
            raise ValueError(f"risk.margin_rate={self.margin_rate} 非法")
        if self.max_units_per_group < 0 or self.max_units_total < 0:
            raise ValueError("risk.max_units_* 不能为负")
        if self.fallback_capital <= 0:
            raise ValueError("risk.fallback_capital 必须为正")
        if self.portfolio_capital < 0:
            raise ValueError("risk.portfolio_capital 不能为负 (0=关闭)")
        if self.portfolio_capital > 0 and                 self.portfolio_capital < self.fallback_capital * 0.01:
            raise ValueError(
                "risk.portfolio_capital 异常小: 检查单位 (元) 是否写错")
        if self.alloc_weights is not None:
            if not isinstance(self.alloc_weights, dict):
                raise ValueError("risk.alloc_weights 需为 {code: 权重} 映射")
            for k, v in self.alloc_weights.items():
                if not isinstance(v, (int, float)) or isinstance(v, bool) \
                        or v <= 0:
                    raise ValueError(
                        f"risk.alloc_weights[{k!r}] 需为正数, 当前 {v!r}")
        if self.account_gate_mode not in ("off", "warn", "veto"):
            raise ValueError(
                f"risk.account_gate_mode 非法 '{self.account_gate_mode}', "
                "仅支持 off|warn|veto")
        if not 0.0 <= self.account_reserve_frac < 1.0:
            raise ValueError("risk.account_reserve_frac 需在 [0,1) 内")
        for name in ("daily_loss_stop_frac", "daily_loss_flatten_frac"):
            v = getattr(self, name)
            if not 0.0 <= v < 1.0:
                raise ValueError(f"risk.{name}={v} 需在 [0,1) 内")
        if self.daily_loss_flatten_frac and \
                self.daily_loss_flatten_frac < self.daily_loss_stop_frac:
            raise ValueError(
                "risk.daily_loss_flatten_frac 不能小于 "
                "daily_loss_stop_frac (平仓阈值应更宽)")
        for name in ("fill_timeout_secs", "exit_fill_timeout_secs"):
            v = getattr(self, name)
            if not isinstance(v, (int, float)) or isinstance(v, bool) \
                    or not 30.0 <= float(v) <= 86400.0:
                raise ValueError(
                    f"risk.{name}={v!r} 需在 [30, 86400] 秒内")
        if not 0.0 < float(self.unit_risk_frac) <= 0.10:
            raise ValueError(
                f"risk.unit_risk_frac={self.unit_risk_frac} 需在 (0,0.10]")
        if self.shadow_capital < 0:
            raise ValueError("risk.shadow_capital 不能为负 (0=关闭)")


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
    journal_dir: str = ""            # K线旁路日志目录 (空=关闭; 见 core/BarJournal)
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
    #   rebuild= 允许按真值对账重建/收养、账户净增量成交确认及
    #            ghost 自动裁剪 (仅当本策略独占该账户时使用)。
    #   alert 下不使用账户净增量证明本策略成交，也不按绝对净仓自动裁剪；
    #   只接受本通道方向匹配的逐笔成交，异常转人工。
    truth_rebuild_mode: str = "alert"
    # ghost_fill 自愈开关: True 仅在 rebuild/独占账户自动裁剪+告警；
    # alert/共享账户即使 True 也只告警人工处置，防止误裁其他策略仓位。
    ghost_auto_trim: bool = True
    # 信号价格源 (Phase 1, 2026-11-10 用户拍板口径):
    #   hot      = 主力连续拼接序列 (默认, 旧行为零漂移; 换月跳空进窗口)
    #   contract = 移仓后**新合约自身真实序列**重算信号 (无任何价格复权;
    #              指标/通道永远在当前主力合约真实价上; 实盘按 hots.json
    #              当前主力解析订阅, 回测按 common/roll_calendar.json 逐日
    #              切换; 执行/持仓仍是 HOT 由框架换月映射)
    signal_source: str = "hot"
    # 涨跌停锁死检测 (秒, 0=关闭): tick 价格钉住不动超过该时长且
    # 存在未闭合成交窗口时, 判定疑似锁死 → 告警并暂停该品种加仓
    # (出场/止损不受影响; 入场仍允许但由 ghost_fill 自愈兜底)。
    # 有界实现: 只依赖"价格钉住"单一信号, 无 tick 时不触发。
    limit_lock_secs: int = 120
    # --- S4 信号口径 A/B (2026-10-24, 详见 docs/OPTIMIZATION_PLAN
    #     §S4 与 matrix_results/caliber_ab.json) ---
    # S4-1: 非 arm_exclude 时的旧包络开关。next_* 已含刚闭合 bar，
    #       包络通常恒等；arm_exclude=True 时该开关被忽略，防止信号根
    #       被重新加入（2026-10-26 语义修正）。
    trigger_envelope: bool = True
    # S4-1b: True=使用 current 通道（已排除刚闭合信号 bar），并跳过
    #        envelope；False=使用 next_* 次日常规通道（最近 N 根含闭合
    #        bar）。生产与研究通过 next_* 字段统一窗口，不再错一根。
    arm_exclude_signal: bool = True
    # S4-2: close 模式同 bar 出场后允许再入场 (默认旧行为 True;
    #       trigger 模式结构性不允许, 本字段仅作用于 close)
    allow_same_bar_reversal: bool = True
    # S4-3: close 模式引擎层跳空止损 (开盘越过止损位按开盘价成交;
    #       默认 True=诚实口径, False=回退旧"按止损价"行为仅供 A/B)
    gap_stop: bool = True


@dataclass
class SpeedConfig:
    """instruments[].speeds[] 项: 一个"速度"(通道窗口组合, 2026-09-16)

    多速度设计 (Man AHL & 文献): 每档速度 = 一个独立子系统, 各自通道窗口,
    共享该品种的风险预算 (share 之和 = 1), 框架层净额化后一次下单。
    """
    name: str                        # 唯一标识 (日志/快照键; 多档 YAML 必填)
    entry: int                       # 入场通道窗口
    exit: int                        # 出场通道窗口
    share: float = 1.0               # 该速度占品种风险预算的份额
    fs: int = 55                     # fail-safe 通道 (仅 S1 语义使用;
                                     # YAML 缺省跟随 base.s2_entry,
                                     # 直接 dataclass 实例化默认仍为 55)
    # 引擎语义 (2026-10-20): name 只作展示/快照键, 不再直接当
    # TurtleEngine.system 用 — 否则 speeds 取名 fast/slow 会触发
    # `assert system in ("S1","S2")`。system 决定 S1 过滤器/fail-safe
    # 语义；多档 YAML 必填，单档/dataclass 缺省仍为 S1。
    system: str = "S1"               # S1 | S2


@dataclass
class ProfileConfig:
    """instruments[].profiles[] 项: 统一的信号 profile 入口
    (2026-10-30 配置统一, docs/PROFILE_CONFIG_UNIFICATION.md)。

    语义上 profile 只有三个正交维度: 窗口(entry/exit/fs) × filter
    (S1 入场状态机) × share(品种预算份额)。`preset: S1|S2` 只是这三组
    维度的预设简写 —— S1 = base.s1_entry/s1_exit + filter(缺省跟随
    base.enable_s1_filter); S2 = base.s2_entry/s2_exit + filter=off。
    显式字段永远覆盖预设缺省值, 因此 "S2 语义 + 自定义窗口"、
    "S1 窗口 + 关过滤器" 等组合都可表达。

    多档必须显式 name (快照/日志身份); 单档 name 可缺省 (键=预设名)。
    与 system/speeds 互斥 —— 后两者是 deprecated 别名, 由
    core.profiles.resolve_profiles 展开为同一 ProfileSpec 序列。
    """
    name: str = ""                   # 快照/日志键; 多档必填且唯一
    preset: str = ""                 # S1 | S2; 空 = 窗口全显式自定义
    entry: int = None                # 入场通道窗口 (None=预设缺省)
    exit: int = None                 # 出场通道窗口 (None=预设缺省)
    fs: int = None                   # fail-safe 通道 (None=base.s2_entry)
    filter: bool = None              # S1 过滤器语义; None=预设缺省
    share: float = None              # 品种风险/保证金预算份额 (单档缺省 1.0)


@dataclass
class InstrumentConfig:
    """单品种配置"""
    code: str                        # e.g. SHFE.rb.HOT
    period: str = "d1"               # d1 | h1
    system: str = SYSTEM_S1          # [deprecated] S1 | S2 | S1+S2, 用 profiles 替代
    units: int = 1                   # 期望交易单位数(每层 VaR = units × unit_var)
    direction: str = DIRECTION_BOTH  # both | long | short
    group: str = ""                  # 相关性组标签(如 black/cheme; 空=不参与组限)
    filters: list = field(default_factory=list)   # list[FilterConfig]
    intrabar_stop: bool = False      # 盘中止损 (主K切换为 base.intrabar_period)
    speeds: list = field(default_factory=list)   # [deprecated] list[SpeedConfig]
                                     # 用 profiles 替代
    profiles: list = field(default_factory=list)  # list[ProfileConfig];
                                     # 非空时唯一入口, 与 system/speeds 互斥,
                                     # 按 core.profiles 展开
    # Phase 1 信号源逐品种覆盖 (空=继承 base.signal_source):
    # 12 品种全部具备 contract 数据源 (9 品种 sina 逐合约 + CZCE 三品种
    # ks2wtpy/KS S3 源; 见 scripts/build_contract_signals.py --ks-csv-dir)。
    # 默认仍 hot (生产零漂移), 切 contract 需逐品种/全局限配 + live 种子。
    signal_source: str = ""

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
