"""ExitJudgeStep — Step 5: 出场判断

regime驱动的出场策略:
- trend regime: 追踪止损为主(信号从峰值回撤X%平仓)
- mr regime: 信号衰竭为主(信号低于exit_threshold平仓)
- range regime: 快速出场(信号低于较高阈值)
"""
import numpy as np
from .context import SignalContext


class ExitJudgeStep:

    def __init__(self, entry_threshold: float = 0.5,
                 exit_threshold: float = 0.1,
                 trailing_exit_bars: int = 6,
                 trailing_decay_ratio: float = 0.3,
                 profit_protect_peak: float = 0.7,
                 profit_protect_floor: float = 0.35,
                 # trend regime专用参数
                 trend_trailing_ratio: float = 0.5,
                 trend_trailing_bars: int = 3,
                 # range regime专用参数
                 range_exit_threshold: float = 0.4,
                 # BUG-7修复: 反转门槛需与EntryJudgeStep一致
                 short_entry_multiplier: float = 1.0,
                 # B12修复: 反转额外缓冲倍数(反转=平仓+开反向仓, 风险高于普通出场)
                 reverse_buffer_multiplier: float = 1.2,
                 # v7: 浮盈追踪止盈参数 (策略参数, 非风控参数 - 不触发冷却期)
                 pnl_trailing_threshold: float = 500.0,
                 pnl_trailing_tight: float = 0.2,
                 pnl_trailing_medium: float = 0.3,
                 pnl_trailing_wide: float = 0.4,
                 # C1修复: _mr_exit PnL 分级阈值参数化(原硬编码 300/100/4000)
                 mr_profit_tier1: float = 300.0,
                 mr_profit_tier2: float = 100.0,
                 mr_loss_scale: float = 4000.0,
                 # C1修复: _pnl_trailing_check 分级边界参数化(原硬编码 1000/2000)
                 pnl_trailing_tier1: float = 1000.0,
                 pnl_trailing_tier2: float = 2000.0,
                 # B-3(2026-07): 反转的新开仓侧补走入场的时段过滤
                 blocked_hours: str = ''):
        self._entry_threshold = entry_threshold
        self._exit_threshold = exit_threshold
        self._trailing_exit_bars = trailing_exit_bars
        self._trailing_decay_ratio = trailing_decay_ratio
        self._profit_protect_peak = profit_protect_peak
        self._profit_protect_floor = profit_protect_floor
        self._trend_trailing_ratio = trend_trailing_ratio
        self._trend_trailing_bars = trend_trailing_bars
        self._range_exit_threshold = range_exit_threshold
        # BUG-7修复: 反转门槛与入场一致
        self._short_entry_multiplier = min(short_entry_multiplier, 1.0)
        # B12修复: 反转门槛额外缓冲
        self._reverse_buffer_multiplier = reverse_buffer_multiplier
        # 追踪止损状态
        self._bars_since_peak = 0
        self._peak_signal = 0.0
        # P0-3修复: 追踪上一bar的regime，切换时重置peak
        self._last_regime = 'range'
        # v7: 浮盈追踪止盈
        self._pnl_trailing_threshold = pnl_trailing_threshold
        self._pnl_trailing_tight = pnl_trailing_tight
        self._pnl_trailing_medium = pnl_trailing_medium
        self._pnl_trailing_wide = pnl_trailing_wide
        # C1修复: _pnl_trailing_check 分级边界参数化
        self._pnl_trailing_tier1 = pnl_trailing_tier1
        self._pnl_trailing_tier2 = pnl_trailing_tier2
        # C1修复: _mr_exit PnL 阈值参数化
        self._mr_profit_tier1 = mr_profit_tier1
        self._mr_profit_tier2 = mr_profit_tier2
        self._mr_loss_scale = mr_loss_scale
        # B6修复: _peak_pnl 独立于 regime, 不随 regime 切换重置
        self._peak_pnl = 0.0
        # B-3: 时段过滤(与 EntryJudgeStep 同规则), 用于反转 veto
        self._blocked_hours = set()
        if blocked_hours:
            for h in blocked_hours.split(','):
                h = h.strip()
                if h.isdigit():
                    self._blocked_hours.add(int(h))

    def process(self, ctx: SignalContext) -> SignalContext:
        # P0-3修复: regime切换时重置信号峰值追踪
        # B6修复: 只重置 _peak_signal(信号峰值), 不重置 _peak_pnl(浮盈峰值)
        # 原问题: regime 切换清零 _peak_pnl 导致追踪止盈保护真空,
        #         频繁切换时需重新积累浮盈到 threshold 才能再触发
        if ctx.regime != self._last_regime:
            self._bars_since_peak = 0
            self._peak_signal = 0.0
            self._last_regime = ctx.regime

        # 出场判断(根据regime选择策略)
        if ctx.current_direction != 0:
            signal_dir = np.sign(ctx.final_signal)
            signal_strength = abs(ctx.final_signal)

            # 更新信号峰值追踪
            if signal_strength > self._peak_signal:
                self._peak_signal = signal_strength
                self._bars_since_peak = 0
            else:
                self._bars_since_peak += 1

            # v7: 浮盈追踪止盈 — 优先级最高(在信号判断之前)
            pnl_trailing_hit = self._pnl_trailing_check(ctx)

            # 反转判断(所有regime通用)
            # B12修复: 反转门槛 = entry_threshold * reverse_buffer_multiplier
            # 反转涉及"平仓+立即开反向仓"，风险高于普通出场，需额外缓冲
            reverse_threshold = self._entry_threshold * self._reverse_buffer_multiplier
            if signal_dir < 0:
                reverse_threshold *= self._short_entry_multiplier
            reverse_signal = (signal_dir != ctx.current_direction
                              and signal_strength > reverse_threshold)
            # B-3(2026-07): 反转的新开仓侧补走入场校验(持续性+时段过滤)。
            # 原问题: should_reverse 由 ExitJudge 判定后直接 execute,
            #         绕过 EntryJudge 的 persist filter / blocked_hours。
            # veto 时不反转, 降级走普通出场评估(只平不开)。
            reverse_vetoed = reverse_signal and (
                not ctx.signal_persistent
                or ctx.current_hour in self._blocked_hours)
            if reverse_signal and not reverse_vetoed:
                ctx.should_reverse = True
            else:
                # 根据regime选择出场策略(信号出场优先)
                if ctx.regime == 'trend':
                    self._trend_exit(ctx, signal_strength)
                elif ctx.regime == 'mr':
                    self._mr_exit(ctx, signal_strength)
                else:
                    self._range_exit(ctx, signal_strength)

                # v7: 浮盈追踪止盈 — 安全网(仅在信号未触发出场时生效)
                if not ctx.should_exit and pnl_trailing_hit:
                    ctx.should_exit = True
                    ctx.pnl_trailing_exit = True
        else:
            self._bars_since_peak = 0
            self._peak_signal = 0.0
            self._peak_pnl = 0.0

        return ctx

    def _trend_exit(self, ctx: SignalContext, signal_strength: float):
        """趋势regime出场: 追踪止损为主"""
        if (self._peak_signal > 0 and
            signal_strength < self._peak_signal * self._trend_trailing_ratio and
            self._bars_since_peak >= self._trend_trailing_bars):
            ctx.should_exit = True
            self._reset_peak()
        elif (self._peak_signal > self._profit_protect_peak and
              signal_strength < self._profit_protect_floor):
            ctx.should_exit = True
            self._reset_peak()
        elif signal_strength < self._exit_threshold * 0.5:
            ctx.should_exit = True
            self._reset_peak()

    def _mr_exit(self, ctx: SignalContext, signal_strength: float):
        """MR regime出场: 信号衰竭为主 + 盈亏维度动态调整

        出场条件: signal_strength < adjusted_exit_threshold
        (阈值越低越难触发=持仓越久, 阈值越高越容易触发=越早出场)

        B5(行为确认2026-07): 浮亏时降低出场阈值 -> 持仓更久等待均值回归。
        这是MR哲学下的有意设计, 非bug: 浮亏=价差偏离更大=回归空间更大,
        软出场让位于回归; 硬止损由RiskMgr quick_stop/max_loss兜底。
        原逻辑(浮亏提阈)会在偏离最大、回归价值最高时提前割肉。
        C1修复: PnL 分级阈值参数化(原硬编码 300/100/4000)。
        """
        adjusted_exit_threshold = self._exit_threshold

        if ctx.unrealized_pnl > self._mr_profit_tier1:
            adjusted_exit_threshold = self._exit_threshold * 3.0
        elif ctx.unrealized_pnl > self._mr_profit_tier2:
            adjusted_exit_threshold = self._exit_threshold * 2.0
        elif ctx.unrealized_pnl < 0:
            # B5(行为确认): 浮亏时降低阈值 -> 更难触发出场 -> 持仓等待均值回归
            # 注意方向: signal_strength < threshold 才出场, 阈值降=条件更难满足
            loss_ratio = min(abs(ctx.unrealized_pnl) / self._mr_loss_scale, 1.0)
            adjusted_exit_threshold = self._exit_threshold * (1.0 - 0.5 * loss_ratio)

        if signal_strength < adjusted_exit_threshold:
            ctx.should_exit = True
            self._reset_peak()
        elif (self._peak_signal > self._profit_protect_peak and
              signal_strength < self._profit_protect_floor):
            ctx.should_exit = True
            self._reset_peak()
        elif (self._peak_signal > 0 and
              signal_strength < self._peak_signal * self._trailing_decay_ratio and
              self._bars_since_peak >= self._trailing_exit_bars):
            ctx.should_exit = True
            self._reset_peak()

    def _range_exit(self, ctx: SignalContext, signal_strength: float):
        """range regime出场: 快速出场"""
        if signal_strength < self._range_exit_threshold:
            ctx.should_exit = True
            self._reset_peak()

    def _reset_peak(self):
        self._bars_since_peak = 0
        self._peak_signal = 0.0
        self._peak_pnl = 0.0

    def reset_trailing(self):
        self._bars_since_peak = 0
        self._peak_signal = 0.0
        self._peak_pnl = 0.0

    def _pnl_trailing_check(self, ctx: SignalContext) -> bool:
        """v7: 基于浮盈金额的分级追踪止盈 (策略参数, 不触发冷却期)"""
        unrealized = ctx.unrealized_pnl

        if unrealized > self._peak_pnl:
            self._peak_pnl = unrealized

        peak = self._peak_pnl

        if peak < self._pnl_trailing_threshold:
            return False

        if peak < self._pnl_trailing_tier1:
            drawdown_ratio = self._pnl_trailing_tight
        elif peak < self._pnl_trailing_tier2:
            drawdown_ratio = self._pnl_trailing_medium
        else:
            drawdown_ratio = self._pnl_trailing_wide

        trigger_line = peak * (1 - drawdown_ratio)
        if unrealized < trigger_line:
            return True

        return False
