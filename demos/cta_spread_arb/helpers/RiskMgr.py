"""
RiskMgr — 风控管理模块

从SpreadArbStrategy中提取，封装:
1. 风控规则检查(价差偏离/最大亏损/时间止损)
2. 持仓时间跟踪(hold_bars)
3. 冷却期管理(cooldown)
4. 已实现盈亏追踪(realized_pnl)
5. 插件化风控规则注册

优化:
- 直接接收RiskConfig对象，不再isinstance(dict)兼容
- print()改为logging模块
"""
import logging
import numpy as np

logger = logging.getLogger(__name__)


class RiskMgr:
    """
    风控管理器: 单一职责 — 风控检查与状态跟踪

    不做交易决策，只返回风控检查结果
    """

    def __init__(self, risk_cfg: 'RiskConfig'):
        """
        Args:
            risk_cfg: RiskConfig结构化对象(不再兼容dict)
        """
        self._risk_cfg = risk_cfg
        self._risk_rules = {
            'spread_deviation': self._builtin_spread_deviation_rule,
            'quick_stop': self._builtin_quick_stop_rule,  # v7.2: 快速止损(优先级高)
            'max_loss': self._builtin_max_loss_rule,
            'profit_take': self._builtin_profit_take_rule,
            'time_stop': self._builtin_time_stop_rule,
            'min_hold': self._builtin_min_hold_rule,
            'coint_quality': self._builtin_coint_quality_rule,
        }
        # 状态跟踪
        self._hold_bars = 0       # 持仓bar数
        self._cooldown = 0        # 冷却期剩余bar数
        self._closing = False     # 是否正在风控平仓中
        self._realized_pnl = 0.0  # 累计已实现盈亏
        self._unrealized_at_close = 0.0  # 平仓前缓存的未实现盈亏
        self._peak_unrealized = 0.0  # 持仓期间最大未实现盈利

    @property
    def hold_bars(self) -> int:
        return self._hold_bars

    @property
    def cooldown(self) -> int:
        return self._cooldown

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl

    def update_hold_bars(self, current_direction: int):
        """更新持仓时间跟踪"""
        if current_direction != 0:
            self._hold_bars += 1
        else:
            self._hold_bars = 0
            self._peak_unrealized = 0.0  # 仓时重置

    def cache_unrealized(self, unrealized: float):
        """缓存当前未实现盈亏(平仓前必须调用)"""
        self._unrealized_at_close = unrealized
        # 更新持仓期间最大未实现盈利
        if unrealized > self._peak_unrealized:
            self._peak_unrealized = unrealized

    def on_risk_close(self):
        """风控平仓触发: 累加realized_pnl + 设置冷却期"""
        self._realized_pnl += self._unrealized_at_close
        self._closing = True
        self._cooldown = self._risk_cfg.cooldown_bars

    def on_signal_close(self):
        """信号平仓触发: 累加realized_pnl"""
        self._realized_pnl += self._unrealized_at_close

    def on_position_cleared(self):
        """仓位已清空(由外部检测到实际仓位归零后调用)"""
        self._closing = False
        self._hold_bars = 0
        self._peak_unrealized = 0.0

    def tick_cooldown(self):
        """冷却期递减1 bar"""
        if self._cooldown > 0:
            self._cooldown -= 1

    def is_in_cooldown(self) -> bool:
        """是否在冷却期内"""
        return self._cooldown > 0

    def check(self, context, sub_config, spread, spread_calc,
              coint_p: float = 1.0, current_direction: int = None) -> str:
        """
        执行风控检查

        Args:
            context: CtaContext
            sub_config: ArbCoreConfig — 结构化核心配置
            spread: 价差序列
            spread_calc: SpreadCalculator实例
            coint_p: 协整p值(供coint_quality规则使用)
            current_direction: 外部传入的当前持仓方向(优先使用)

        Returns:
            风控触发时返回描述字符串，否则返回None
        """
        # 空仓时不检查
        positions = {}
        pnl = {}
        for leg in sub_config.legs:
            code = leg.code
            positions[code] = context.stra_get_position(code)
            pnl[code] = context.stra_get_position_profit(code)

        if all(v == 0 for v in positions.values()):
            return None

        # P1-5修复: current_direction优先从外部传入
        if current_direction is None:
            current_direction = 0
            for code, pos in positions.items():
                if pos != 0:
                    current_direction = 1 if pos > 0 else -1
                    break

        from schemas import RiskContext
        risk_ctx = RiskContext(
            sub_config=sub_config,
            spread=spread,
            spread_calc=spread_calc,
            positions=positions,
            pnl=pnl,
            risk_cfg=self._risk_cfg,
            hold_bars=self._hold_bars,
            coint_p=coint_p,
            current_direction=current_direction,
        )

        # 执行所有注册的风控规则
        for rule_name, rule_fn in self._risk_rules.items():
            try:
                result = rule_fn(risk_ctx)
                if result is not None:
                    return result
            except Exception as e:
                logger.warning(f"[RISK] Rule {rule_name} error: {e}")

        return None

    def register_rule(self, name: str, rule_fn):
        """注册自定义风控规则

        rule_fn接收RiskContext对象，返回字符串则触发风控，返回None则通过
        """
        self._risk_rules[name] = rule_fn

    def restore(self, state_dict: dict):
        """从持久化数据恢复状态"""
        self._hold_bars = state_dict.get('hold_bars', 0)
        self._cooldown = state_dict.get('cooldown', 0)
        self._realized_pnl = state_dict.get('realized_pnl', 0.0)
        self._closing = state_dict.get('closing', False)

    def snapshot(self) -> dict:
        """导出当前状态供持久化"""
        return {
            'hold_bars': self._hold_bars,
            'cooldown': self._cooldown,
            'realized_pnl': self._realized_pnl,
            'closing': self._closing,
        }

    # === 内置风控规则 ===

    def _builtin_spread_deviation_rule(self, ctx):
        """价差偏离度超限"""
        max_dev = ctx.risk_cfg.max_spread_deviation
        spread_calc = ctx.spread_calc
        spread = ctx.spread
        if spread_calc.std > 0:
            dev = abs(spread[-1] - spread_calc.mean) / spread_calc.std
            if dev > max_dev:
                return f"spread_deviation: {dev:.2f} > {max_dev}"
        return None

    def _builtin_quick_stop_rule(self, ctx):
        """v7.2: 快速止损 — 早期亏损加速退出
        
        核心逻辑:
        - 持仓<=6bars(30min)且浮亏>=300: 立即止损(行情与预期严重不符)
        - 持仓<=12bars(1h)且浮亏>=500: 立即止损(信号已证实错误)
        - 浮亏从未>0(纯亏损): 降低max_loss阈值加速退出
        """
        total_pnl = sum(ctx.pnl.values())
        hold_bars = ctx.hold_bars
        
        # 阶段1: 极早期快速止损
        if hold_bars <= 6 and total_pnl <= -300:
            return f"quick_stop_early: pnl={total_pnl:.0f} at {hold_bars} bars"
        
        # 阶段2: 早期确认止损
        if hold_bars <= 12 and total_pnl <= -500:
            return f"quick_stop_confirm: pnl={total_pnl:.0f} at {hold_bars} bars"
        
        # 阶段3: 纯亏损(从未盈利)加速止损
        # peak_unrealized为0说明从未有过正向浮盈
        if self._peak_unrealized <= 0 and total_pnl <= -300:
            return f"quick_stop_pure_loss: pnl={total_pnl:.0f}, never profitable"
        
        return None

    def _builtin_max_loss_rule(self, ctx):
        """单组合最大亏损"""
        max_loss = ctx.risk_cfg.max_loss_per_pair
        total_pnl = sum(ctx.pnl.values())
        if total_pnl < -max_loss:
            return f"max_loss: {total_pnl:.0f} < -{max_loss}"
        return None

    def _builtin_profit_take_rule(self, ctx):
        """
        分级追踪止盈(v7): 根据浮盈大小动态调整回撤容忍度
        
        核心思路: 小盈紧追踪(保住小利润), 大盈宽追踪(让利润跑)
        - 浮盈 < 1000: 回撤20%即锁利
        - 浮盈 1000-2000: 回撤30%锁利
        - 浮盈 > 2000: 回撤40%锁利
        
        profit_take_threshold: 最低触发追踪止盈的浮盈门槛
        profit_drawdown_ratio: 基础回撤比例(小盈档位)
        """
        profit_take_threshold = ctx.risk_cfg.profit_take_threshold
        base_drawdown_ratio = ctx.risk_cfg.profit_drawdown_ratio
        total_pnl = sum(ctx.pnl.values())
        
        # 每次check都更新peak
        if total_pnl > self._peak_unrealized:
            self._peak_unrealized = total_pnl
        
        peak = self._peak_unrealized
        
        # 盈利未达门槛，不触发
        if peak < profit_take_threshold:
            return None
        
        # 分级回撤比例: 小盈紧追踪, 大盈宽追踪
        if peak < 1000:
            drawdown_ratio = base_drawdown_ratio  # 0.2 (20%)
        elif peak < 2000:
            drawdown_ratio = base_drawdown_ratio + 0.1  # 0.3 (30%)
        else:
            drawdown_ratio = base_drawdown_ratio + 0.2  # 0.4 (40%)
        
        # 当前回撤超过分级比例时触发
        if total_pnl < peak * (1 - drawdown_ratio):
            return f"profit_take: peak={peak:.0f}, current={total_pnl:.0f}, drawdown={(peak-total_pnl)/peak:.0%}, tier_ratio={drawdown_ratio:.0%}"
        return None

    def _builtin_min_hold_rule(self, ctx):
        """最小持仓时间: 仅做状态记录，不触发风控平仓"""
        return None

    def _builtin_time_stop_rule(self, ctx):
        """时间止损: 持仓超时无条件平仓"""
        max_hold_bars = ctx.risk_cfg.max_hold_bars
        if self._hold_bars > max_hold_bars:
            total_pnl = sum(ctx.pnl.values())
            return f"time_stop: held {self._hold_bars} bars (>{max_hold_bars}) with pnl={total_pnl:.0f}"
        return None

    def _builtin_coint_quality_rule(self, ctx):
        """协整质量门控: 协整p值恶化时限制新开仓(衰减而非禁止)

        P1-1修复: 阈值从RiskConfig读取，不再硬编码0.10/0.05
        默认值: coint_force_exit_p=0.15(原0.10对rb-i过严), coint_block_entry_p=0.05
        """
        coint_p = ctx.coint_p
        current_direction = ctx.current_direction
        force_exit_p = self._risk_cfg.coint_force_exit_p if hasattr(self._risk_cfg, 'coint_force_exit_p') else 0.15
        block_entry_p = self._risk_cfg.coint_block_entry_p if hasattr(self._risk_cfg, 'coint_block_entry_p') else 0.05

        if coint_p >= force_exit_p and current_direction != 0:
            return f"coint_quality: coint_p={coint_p:.4f}>={force_exit_p}, force exit"
        if coint_p >= block_entry_p and current_direction == 0:
            return f"coint_quality: coint_p={coint_p:.4f}>={block_entry_p}, block new entry"
        return None