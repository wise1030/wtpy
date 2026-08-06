"""
types.py — 统一数据结构定义

所有模块间传递的数据结构集中定义，消除散落的namedtuple/dict。
新增字段只需修改此处，下游自动适配。
"""
from dataclasses import dataclass, field
import dataclasses
from typing import Any, Dict, List, Optional
import re
import numpy as np


# stdCode 三段式正则: EXCHG.product.contract
# - EXCHG: 大写交易所代码 (SHFE/DCE/CZCE/CFFEX/INE/GFEX)
# - product: 品种字母 (rb/hc/l/pp/IF/IO etc, 大小写都允许)
# - contract: 月份 (2509/2601) 或主连 (HOT/main/MAIN)
_STDCODE_REGEX = re.compile(r'^[A-Z]+\.[A-Za-z]+\.[A-Za-z0-9_\-]+$')


# ============================================================
# 数据层: DataPipe 输出
# ============================================================

@dataclass
class BarData:
    """DataPipe输出 — K线数据+价差+趋势"""
    prices: Dict[str, np.ndarray]       # code -> closes数组
    volumes: Dict[str, np.ndarray]      # code -> volumes数组
    aux_bars: Dict[str, object]          # key -> HbData
    spread: np.ndarray                   # 当前价差序列
    spread_trend_filter: int = 0         # 日线趋势方向(1/-1/0)
    beta: Optional[float] = None         # 协整beta(非coint模式为None)
    min_len: int = 0                     # 最短K线长度


# ============================================================
# 因子层: FactorEngine 输出
# ============================================================

@dataclass
class FactorOutput:
    """FactorEngine输出 — 因子信号+协整信息"""
    total_score: float = 0.0             # 加权总分 [-1, 1]
    factor_scores: Dict[str, float] = field(default_factory=dict)  # 因子名->分数
    direction: int = 0                   # 方向(1/-1/0)
    coint_p: float = 1.0                 # 协整p值
    signal_mode: str = 'adaptive'        # 信号模式
    failed_factors: List[str] = field(default_factory=list)  # 异常因子列表
    # P2-1修复: 附加regime和因子方向类型(原通过dict附加)
    regime: str = 'range'                # 当前regime
    factor_direction_types: Dict[str, str] = field(default_factory=dict)  # 因子名->direction_type

    def to_dict(self) -> dict:
        """兼容旧接口: 转为dict供SignalPipeline使用"""
        return {
            'total_score': self.total_score,
            'factor_scores': self.factor_scores,
            'direction': self.direction,
            'coint_p': self.coint_p,
            'signal_mode': self.signal_mode,
            'failed_factors': self.failed_factors,
            'regime': self.regime,
            'factor_direction_types': self.factor_direction_types,
        }


# ============================================================
# 信号层: SignalGen 输出
# ============================================================

@dataclass
class SignalResult:
    """SignalGen输出 — 交易信号+决策
    
    方向决策统一: direction = sign(entry_signal)
    - entry_signal > 0 → direction=1 → 正向套利(买rb卖i)
    - entry_signal < 0 → direction=-1 → 反向套利(卖rb买i)
    - entry_signal = 0 → direction=0 → 无操作
    不再有major_direction/factor_direction分裂。
    """
    entry_signal: float = 0.0            # 入场信号强度 [-1, 1]
    direction: int = 0                   # 交易方向 = sign(entry_signal)
    should_trade: bool = False           # 是否应开仓
    should_exit: bool = False            # 是否应平仓
    should_reverse: bool = False         # 是否应反转
    regime: str = 'unknown'              # 当前市场状态(trend/mr/range)
    regime_confidence: float = 0.0       # regime置信度
    regime_direction: int = 0            # regime方向(1/-1/0)
    raw_signal: float = 0.0              # 原始因子信号
    strength: float = 0.0                # 信号绝对强度
    trend_blocked: bool = False          # 是否被趋势过滤衰减
    pnl_trailing_exit: bool = False     # v7: 是否由浮盈追踪止盈触发




# ============================================================
# 配置层: 结构化配置
# ============================================================

def _build_from_yaml(cls, cfg: dict):
    """C-2(2026-07): from_yaml 默认值单源化。

    默认值只存在于 dataclass 字段定义, from_yaml 不再逐字段双写
    (原模式改默认值要改两处, 是配置漂移温床)。
    未知 key 静默忽略(与历史行为一致), 覆盖校验由调用方负责
    (SpreadArbStrategy._resolve_sub_cfg 已做未知字段报错)。
    """
    cfg = cfg or {}
    return cls(**{f.name: cfg[f.name]
                  for f in dataclasses.fields(cls) if f.name in cfg})


@dataclass
class SignalConfig:
    """信号相关配置 — 从strategy_config.yaml的signal节解析"""
    # C-2(2026-07): signal_mode 补入(此前YAML signal节写了但被静默丢弃,
    # FactorEngine 永远走默认 adaptive)
    signal_mode: str = 'adaptive'
    entry_threshold: float = 0.525
    exit_threshold: float = 0.08
    coint_decay: float = 0.8
    momentum_decay: float = 0.8
    # C-2(2026-07): trend_aligned_boost/trend_counter_decay/range_signal_decay
    # 已删除 — P0-2 后 RegimeDispatchStep 不再消费, 属死配置字段
    signal_persist_bars: int = 1         # 连续N bar同向才开仓, 1=不过滤
    short_entry_multiplier: float = 1.0
    trailing_exit_bars: int = 6
    trailing_decay_ratio: float = 0.3
    # v8.1: direction_threshold 已废除（dead 参数 — FactorOutput.direction
    # 仅 ctx.factor_direction 接收，但 SignalPipeline 无任何步骤读取）
    profit_protect_peak: float = 0.65
    profit_protect_floor: float = 0.3
    # trend regime出场参数
    trend_trailing_ratio: float = 0.5
    trend_trailing_bars: int = 3
    # range regime出场参数
    range_exit_threshold: float = 0.3
    # trend regime逆势信号衰减系数(替代硬阻挡)
    trend_counter_decay_factor: float = 0.5
    # v7.2: 时段过滤
    blocked_hours: str = '14'
    # v7: 浮盈追踪止盈参数 (策略参数, 非风控参数 — 不触发冷却期)
    pnl_trailing_threshold: float = 500.0
    pnl_trailing_tight: float = 0.2
    pnl_trailing_medium: float = 0.3
    pnl_trailing_wide: float = 0.4
    # B12修复: 反转额外缓冲倍数(反转风险高于普通出场)
    reverse_buffer_multiplier: float = 1.2
    # C1修复: _mr_exit PnL 分级阈值参数化(原硬编码 300/100/4000)
    mr_profit_tier1: float = 300.0
    mr_profit_tier2: float = 100.0
    mr_loss_scale: float = 4000.0
    # C1修复: _pnl_trailing_check 分级边界参数化(原硬编码 1000/2000)
    pnl_trailing_tier1: float = 1000.0
    pnl_trailing_tier2: float = 2000.0

    @classmethod
    def from_yaml(cls, signal_cfg: dict) -> 'SignalConfig':
        """从YAML配置dict创建(C-2: 默认值单源化)"""
        return _build_from_yaml(cls, signal_cfg)


@dataclass
class RiskConfig:
    """风控相关配置 — 从strategy_config.yaml的risk_control节解析"""
    cooldown_bars: int = 12
    max_hold_bars: int = 120
    max_loss_per_pair: float = 8000
    max_spread_deviation: float = 4.0
    min_hold_bars: int = 6
    # B-1(2026-07): profit_take/profit_drawdown/profit_tier_* 字段已删除 —
    # RiskMgr profit_take 规则与 ExitJudgeStep._pnl_trailing_check 双重止盈,
    # 统一保留信号层软止盈 (SignalConfig.pnl_trailing_*)
    # P1-1修复: 协整质量强平阈值可配置(原硬编码0.10对rb-i过严)
    coint_force_exit_p: float = 0.15   # coint_p>=此值时强平(默认0.15, 原0.10)
    coint_block_entry_p: float = 0.05  # coint_p>=此值时禁止新开仓
    # P2-4修复: 反转豁免冷却期选项(反转本质是"平旧+开新"，是否跳过cooldown)
    reverse_cooldown_exempt: bool = False  # True=反转后不触发冷却期
    # P2-6: quick_stop 三阶段阈值(原硬编码 6/300, 12/500, 300)
    quick_stop_early_bars: int = 6         # 阶段1: 极早期止损 bar 数门槛
    quick_stop_early_loss: float = 300     # 阶段1: 浮亏门槛(>=触发)
    quick_stop_confirm_bars: int = 12      # 阶段2: 早期确认止损 bar 数门槛
    quick_stop_confirm_loss: float = 500   # 阶段2: 浮亏门槛
    quick_stop_pure_loss: float = 300      # 阶段3: 纯亏损(peak_unrealized<=0)止损门槛

    @classmethod
    def from_yaml(cls, risk_cfg: dict) -> 'RiskConfig':
        """从YAML配置dict创建(C-2: 默认值单源化)"""
        return _build_from_yaml(cls, risk_cfg)


@dataclass
class RegimeConfig:
    """Regime检测配置 — 从strategy_config.yaml的regime节解析"""
    # RegimeDetector参数
    hurst_window: int = 100              # Hurst指数计算窗口
    adx_window: int = 14                 # ADX窗口
    vol_window: int = 30                 # 波动率窗口
    # regime判断阈值
    trend_hurst_threshold: float = 0.55  # Hurst>此值认为有趋势
    mr_hurst_threshold: float = 0.45     # Hurst<此值认为有均值回归
    trend_adx_threshold: float = 25.0    # ADX>此值认为趋势强
    vol_regime_z: float = 1.0            # 波动率z-score阈值
    # 状态记忆(避免频繁切换)
    regime_memory_bars: int = 5          # regime记忆bar数
    regime_switch_cost: float = 0.3      # regime切换代价(0-1, 越大越不容易切换)
    # 权重映射(REGIME_WEIGHT_MAP)
    trend_momentum_weight: float = 1.5
    trend_reversal_weight: float = 0.5
    mr_momentum_weight: float = 0.5
    mr_reversal_weight: float = 1.5
    range_momentum_weight: float = 0.3
    range_reversal_weight: float = 0.3
    range_neutral_weight: float = 0.5
    # C-2(2026-07): 补充 RegimeDetector 性能/投票参数(此前 YAML 无法调节,
    # create_regime_detector 只映射 7 个参数, 其余只能改代码默认值)
    adf_maxlag: int = 1                  # adfuller maxlag(性能热点, 默认1足够)
    adf_cache_every: int = 24            # ADF 缓存间隔 bar 数(m5=2小时)
    hurst_cache_every: int = 1           # Hurst 缓存间隔 bar 数(1=每bar重算)
    weight_hurst: float = 0.3            # 投票权重: Hurst
    weight_adf: float = 0.25             # 投票权重: ADF
    weight_adx: float = 0.25             # 投票权重: ADX
    weight_ma: float = 0.2               # 投票权重: MA斜率

    @classmethod
    def from_yaml(cls, regime_cfg: dict) -> 'RegimeConfig':
        """从YAML配置dict创建(C-2: 默认值单源化)"""
        return _build_from_yaml(cls, regime_cfg)


@dataclass
class ExecConfig:
    """执行相关配置 — 从strategy_config.yaml的execution节解析
    
    P1-2: 将ArbExecHelper硬编码参数配置化
    """
    leg_timeout: int = 5           # 单腿超时(秒/回测tick数)
    max_retry: int = 3             # 最大追单次数
    bollinger_lookback: int = 60   # DataPipe布林带回看窗口
    bollinger_threshold: float = 2.0  # DataPipe布林带倍数
    min_bar_length: int = 20       # DataPipe最小K线长度

    @classmethod
    def from_yaml(cls, exec_cfg: dict) -> 'ExecConfig':
        """从YAML配置dict创建(C-2: 默认值单源化)"""
        return _build_from_yaml(cls, exec_cfg)


@dataclass
class LegConfig:
    """单腿配置

    code 必须是严格三段式 stdCode: EXCHG.product.contract
    例: DCE.l.2609 / SHFE.rb.HOT / CZCE.SR.501
    错误格式 (l2609 / DCE.l2609 / l.2609) 会被 __post_init__ 抛出 ValueError。
    """
    code: str
    lots: int
    multiplier: int
    ratio: int = 1

    def __post_init__(self):
        if not isinstance(self.code, str) or not _STDCODE_REGEX.match(self.code):
            raise ValueError(
                f"LegConfig.code 必须是三段式 stdCode 'EXCHG.product.contract', "
                f"实际收到 {self.code!r}. "
                f"正确示例: 'DCE.l.2609', 'SHFE.rb.HOT', 'CZCE.SR.501'."
            )


@dataclass
class ArbCoreConfig:
    """套利核心配置 — 单品种组合完整配置
    
    v10: 从SubStrategyConfig重命名, 去除多sub概念
    """
    name: str
    legs: List[LegConfig]
    main_period: str
    main_bar_cnt: int
    aux_periods: List[Dict[str, Any]]
    signal_mode: str = 'adaptive'
    spread_mode: str = 'coint'
    type: str = 'cross_commodity'

    @classmethod
    def from_yaml(cls, sub_cfg: dict) -> 'ArbCoreConfig':
        """从YAML配置dict创建"""
        legs = [LegConfig(**leg) for leg in sub_cfg['legs']]
        periods = sub_cfg.get('periods', {})
        return cls(
            name=sub_cfg['name'],
            legs=legs,
            main_period=periods.get('main_period', 'm5'),
            main_bar_cnt=periods.get('main_bar_cnt', 300),
            aux_periods=periods.get('aux_periods', []),
            signal_mode=sub_cfg.get('signal_mode', 'adaptive'),
            spread_mode=sub_cfg.get('spread_mode', 'coint'),
            type=sub_cfg.get('type', 'cross_commodity'),
        )




@dataclass
class RiskContext:
    """风控检查上下文 — RiskMgr.check()的参数打包"""
    sub_config: ArbCoreConfig
    spread: np.ndarray
    spread_calc: object  # SpreadCalculator
    positions: Dict[str, int]
    pnl: Dict[str, float]
    risk_cfg: RiskConfig  # ARCH-2修复: 从dict改为RiskConfig结构化对象
    hold_bars: int = 0
    coint_p: float = 1.0
    current_direction: int = 0

    @property
    def sub_name(self) -> str:
        return self.sub_config.name


# ============================================================
# 决策层: ArbCore 内部计算决策结果 (A8修复: 从 ArbCore.py 移至 schemas.py)
# ============================================================

@dataclass
class ArbDecision:
    """单个品种组合的计算决策结果"""
    bar_data: object = None
    signal: object = None
    factor_signal: object = None
    current_direction: int = 0
    unrealized_pnl: float = 0.0  # D2修复: 缓存未实现盈亏避免重复计算
    # C-3(2026-07): 同bar持仓/盈亏dict缓存, 供RiskMgr.check/indicators复用
    positions: dict = None
    pnl: dict = None
