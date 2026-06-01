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
                 # v7: 浮盈追踪止盈参数
                 pnl_trailing_threshold: float = 500.0,
                 pnl_trailing_tight: float = 0.2,
                 pnl_trailing_medium: float = 0.3,
                 pnl_trailing_wide: float = 0.4):
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
        self._peak_pnl = 0.0

    def process(self, ctx: SignalContext) -> SignalContext:
        # P0-3修复: regime切换时重置peak追踪
        if ctx.regime != self._last_regime:
            self._reset_peak()
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
            # 核心思路: 基于实际浮盈金额的追踪止盈，而非信号强度
            pnl_trailing_hit = self._pnl_trailing_check(ctx)

            # 反转判断(所有regime通用)
            # BUG-7修复: 反转门槛与EntryJudgeStep入场门槛一致
            reverse_threshold = self._entry_threshold
            if signal_dir < 0:  # 反转做空时用做空门槛
                reverse_threshold *= self._short_entry_multiplier
            if signal_dir != ctx.current_direction and signal_strength > reverse_threshold:
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
                # 逻辑: 信号说"继续持仓"但利润在流失 → 保护性出场
                # 不与信号出场竞争，只在信号未说"出场"时兜底
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
        
        v7.2优化: 盈利时降低出场阈值(更容易出场保护利润)
        - 浮盈>300: exit_threshold * 3.0 (积极锁利)
        - 浮盈>100: exit_threshold * 2.0 (适度锁利)  
        - 浮盈<0: exit_threshold * (1+loss/4000) (亏损时也更容易出场)
        - 浮盈0-100: exit_threshold (正常)
        """
        adjusted_exit_threshold = self._exit_threshold
        
        # 盈利时更积极出场(核心优化: 提高利润捕获率)
        if ctx.unrealized_pnl > 300:
            adjusted_exit_threshold = self._exit_threshold * 3.0
        elif ctx.unrealized_pnl > 100:
            adjusted_exit_threshold = self._exit_threshold * 2.0
        # 亏损时也更容易出场(减少亏损暴露)
        elif ctx.unrealized_pnl < 0:
            loss_ratio = min(abs(ctx.unrealized_pnl) / 4000.0, 1.0)
            adjusted_exit_threshold = self._exit_threshold * (1.0 + loss_ratio)

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
        """
        v7: 基于浮盈金额的分级追踪止盈
        
        核心逻辑:
        - 追踪持仓期间的浮盈峰值
        - 浮盈达到门槛后，根据峰值分级设置回撤容忍度:
          peak < 1000: 紧追踪(回撤20%即平仓)
          peak 1000-2000: 中等追踪(回撤30%)
          peak > 2000: 宽追踪(回撤40%)
        - 反转信号优先级高于追踪止盈(已在外层处理)
        """
        unrealized = ctx.unrealized_pnl
        
        # 更新浮盈峰值
        if unrealized > self._peak_pnl:
            self._peak_pnl = unrealized
        
        peak = self._peak_pnl
        
        # 未达门槛不触发
        if peak < self._pnl_trailing_threshold:
            return False
        
        # 分级回撤比例
        if peak < 1000:
            drawdown_ratio = self._pnl_trailing_tight
        elif peak < 2000:
            drawdown_ratio = self._pnl_trailing_medium
        else:
            drawdown_ratio = self._pnl_trailing_wide
        
        # 当前浮盈回撤超过分级比例时触发
        # 即: unrealized < peak * (1 - drawdown_ratio)
        trigger_line = peak * (1 - drawdown_ratio)
        if unrealized < trigger_line:
            return True
        
        return False