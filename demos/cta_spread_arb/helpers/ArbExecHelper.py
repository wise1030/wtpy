"""
ArbExecHelper — 套利执行协调器

完整状态机实现:
    idle -> opening -> opened -> closing -> idle

P1-2优化: LEG_TIMEOUT/MAX_RETRY从ExecConfig配置读取，不再硬编码。
"""
import numpy as np
import time as _time
import logging

logger = logging.getLogger(__name__)


class ArbExecHelper:
    """
    套利执行协调器 - 完整状态机实现

    状态流转:
        idle -> opening -> opened -> closing -> idle

    关键修复:
    1. 时间比较使用系统时间(time.time())而非stra_get_time()的HHMM格式
    2. 完整状态机，opening/closing状态下不会误触发平仓
    3. 所有腿成交后正确转换到opened状态
    4. 支持出场信号(信号衰减)和反转开仓
    """

    # 状态常量
    STATE_IDLE = "idle"
    STATE_OPENING = "opening"
    STATE_OPENED = "opened"
    STATE_CLOSING = "closing"

    def __init__(self, legs: list, is_backtest: bool = False,
                 leg_timeout: int = 5, max_retry: int = 3):
        """
        Args:
            legs: list[LegConfig] — 结构化腿配置
            is_backtest: 是否回测模式
            leg_timeout: 单腿超时(秒/回测tick数)，默认30
            max_retry: 最大追单次数，默认3
        """
        self._legs = legs
        self._pending = {}
        self._filled = {}
        self._state = self.STATE_IDLE
        self._direction = 0  # 当前持仓方向: 1=正向, -1=反向, 0=无
        self._reverse_after = False
        self._pending_direction = 0
        self._is_backtest = is_backtest  # 回测模式下跳过超时追单
        self._leg_timeout = leg_timeout  # P1-2: 配置化超时
        self._max_retry = max_retry      # P1-2: 配置化重试

    @classmethod
    def from_config(cls, legs: list, exec_cfg, is_backtest: bool = False):
        """从ExecConfig创建实例(P1-2)"""
        return cls(
            legs=legs,
            is_backtest=is_backtest,
            leg_timeout=exec_cfg.leg_timeout,
            max_retry=exec_cfg.max_retry,
        )

    def execute(self, context, sub_config, signal):
        """
        根据信号和当前状态执行交易决策

        Args:
            context: CtaContext
            sub_config: SubStrategyConfig — 结构化子策略配置
            signal: SignalResult — 结构化信号

        逻辑:
        - idle: 无仓时开仓
        - opened: 检查是否需要平仓(信号衰减/反转)
        - opening/closing: 状态守卫，不重复触发
        """
        # 修复: entry_signal=0时direction应为0(不开仓)，而非-1
        if signal.entry_signal > 0:
            direction = 1
        elif signal.entry_signal < 0:
            direction = -1
        else:
            direction = 0

        if self._state == self.STATE_IDLE:
            # 无仓无挂单 -> 开仓 (direction=0时不执行)
            if direction != 0:
                self._open_position(context, sub_config, direction)

        elif self._state == self.STATE_OPENED:
            # 已有持仓，检查是否需要平仓
            should_exit = signal.should_exit
            should_reverse = signal.should_reverse

            if should_reverse and direction != self._direction:
                # 信号反转 -> 先平仓，标记待反转
                self._close_position(context, sub_config, reverse_after=True)
                self._pending_direction = direction
            elif should_exit:
                # 信号衰减 -> 平仓
                self._close_position(context, sub_config, reverse_after=False)
            # else: 持仓方向一致，继续持有

        # opening/closing 状态下不做任何操作，等待on_tick_check处理

    def _open_position(self, context, sub_config, direction):
        """执行开仓"""
        self._state = self.STATE_OPENING
        self._direction = direction
        targets = self._calc_target_positions(direction)
        for t in targets:
            code = t['code']
            target = t['target']
            cur_pos = context.stra_get_position(code)
            if target != cur_pos:
                context.stra_set_position(
                    code, target, f"Open_{sub_config.name}_{code}")
                self._pending[code] = {
                    'target': target,
                    'retry_count': 0,
                    'timestamp': _time.time()  # 使用系统时间戳(秒)
                }

    def _close_position(self, context, sub_config, reverse_after: bool = False):
        """执行平仓"""
        self._state = self.STATE_CLOSING
        self._reverse_after = reverse_after
        for leg in self._legs:
            code = leg.code
            cur_pos = context.stra_get_position(code)
            if cur_pos != 0:
                context.stra_set_position(
                    code, 0, f"Close_{sub_config.name}_{code}")
                self._pending[code] = {
                    'target': 0,
                    'retry_count': 0,
                    'timestamp': _time.time()
                }

    def close_all(self, context, sub_config, tag: str = "RiskClose"):
        """紧急全平（风控调用）"""
        # 先检查是否有仓位需要平
        has_position = False
        for leg in self._legs:
            code = leg.code
            cur_pos = context.stra_get_position(code)
            if cur_pos != 0:
                context.stra_set_position(code, 0, f"{tag}_{sub_config.name}_{code}")
                self._pending[code] = {
                    'target': 0,
                    'retry_count': 0,
                    'timestamp': _time.time()
                }
                has_position = True
        
        if has_position:
            # 有仓位需要平 → 设置CLOSING状态，等on_tick_check确认成交
            self._state = self.STATE_CLOSING
            self._reverse_after = False
            self._pending_direction = 0
            # 注意：不重置_direction！等on_tick_check确认成交后再重置
        else:
            # 无仓位 → 直接重置
            self._pending.clear()
            self._filled.clear()
            self._state = self.STATE_IDLE
            self._direction = 0
            self._reverse_after = False
            self._pending_direction = 0

    def restore_state(self, direction: int):
        """恢复持久化状态（供on_init调用，不绕过状态机）"""
        self._direction = direction
        self._state = self.STATE_OPENED
        self._pending.clear()
        self._filled.clear()
        self._reverse_after = False
        self._pending_direction = 0

    def reset(self):
        """公共重置方法 — 替代外部直接访问私有属性"""
        self._state = self.STATE_IDLE
        self._direction = 0
        self._pending.clear()
        self._filled.clear()
        self._reverse_after = False
        self._pending_direction = 0

    def on_tick_check(self, context, sub_config):
        """
        tick回调中检查成交状态和追单

        Args:
            context: CtaContext
            sub_config: SubStrategyConfig — 结构化子策略配置

        修复: 使用time.time()系统时间而非stra_get_time()的HHMM格式
        """
        for code, pending in list(self._pending.items()):
            cur_pos = context.stra_get_position(code)
            if cur_pos == pending['target']:
                # 该腿已成交
                self._filled[code] = pending
                del self._pending[code]
                continue

            # 超时追单逻辑
            # 回测模式: time.time()不推进，改用bar计数方式
            # 实盘模式: 使用系统时间计算超时(秒)
            should_retry = False
            if self._is_backtest:
                # 回测中: 每次on_tick_check调用视为1个时间单位
                elapsed_ticks = pending.get('tick_count', 0)
                pending['tick_count'] = elapsed_ticks + 1
                if elapsed_ticks >= self._leg_timeout:
                    should_retry = True
            else:
                elapsed = _time.time() - pending['timestamp']
                if elapsed > self._leg_timeout:
                    should_retry = True

            if should_retry:
                if pending['retry_count'] < self._max_retry:
                    context.stra_set_position(
                        code, pending['target'],
                        f"Retry_{sub_config.name}_{code}_{pending['retry_count']}")
                    pending['retry_count'] += 1
                    pending['timestamp'] = _time.time()
                    # 修复: 回测模式下重置tick_count
                    if self._is_backtest:
                        pending['tick_count'] = 0
                else:
                    logger.warning(
                        f"leg {code} retry failed ({self._max_retry}x), "
                        f"emergency close all legs")
                    context.stra_log_text(
                        f"leg {code} retry failed, emergency close all legs",
                        level=3)
                    for leg in self._legs:
                        context.stra_set_position(
                            leg.code, 0,
                            f"Emergency_{sub_config.name}_{leg.code}")
                    self._pending.clear()
                    self._filled.clear()
                    self._state = self.STATE_IDLE
                    self._direction = 0
                    # 修复: 紧急平仓时也重置反转相关状态
                    self._reverse_after = False
                    self._pending_direction = 0
                    return

        # 检查是否所有挂单都已成交
        if len(self._pending) == 0 and len(self._filled) > 0:
            self._filled.clear()

            if self._state == self.STATE_OPENING:
                self._state = self.STATE_OPENED
            elif self._state == self.STATE_CLOSING:
                # 平仓完成
                if self._reverse_after:
                    # BUG-4修复: 反转开仓不再裸开，需检查冷却期和协整门控
                    new_dir = self._pending_direction
                    self._reverse_after = False
                    self._pending_direction = 0
                    if new_dir != 0:
                        # 反转开仓: 先回到IDLE，由上层信号驱动重新入场
                        # 不再直接开仓，让下一bar的信号流程自然处理
                        self._state = self.STATE_IDLE
                        self._direction = 0
                    else:
                        self._state = self.STATE_IDLE
                        self._direction = 0
                else:
                    self._state = self.STATE_IDLE
                    self._direction = 0

    def get_status(self) -> dict:
        return {
            "state": self._state,
            "direction": self._direction,
            "pending_legs": list(self._pending.keys()),
            "filled_legs": list(self._filled.keys())
        }

    @property
    def state(self) -> str:
        return self._state

    @property
    def direction(self) -> int:
        return self._direction

    def _calc_target_positions(self, direction: int) -> list:
        """
        计算各腿目标仓位
        
        Args:
            direction: 套利方向(1=正向, -1=反向)
        
        CODE-5修复: 移除未用beta参数。原beta参数用于动态调整lots，
        但实际代码中从未使用(固定lots配置)，且beta对冲在乘数差异大时不可行。
        """
        targets = []
        for leg in self._legs:
            if leg.ratio > 0:
                target = direction * leg.lots
            else:
                target = -direction * leg.lots
            targets.append({'code': leg.code, 'target': target})
        return targets

    def infer_direction(self, context) -> int:
        """
        根据各腿持仓推断当前套利方向(公共接口)

        逻辑: 对每腿，ratio>0的腿持仓方向应与direction一致，
        ratio<0的腿持仓方向应与direction相反。
        修复: 所有腿都有仓位时才返回非零，避免部分腿成交误判
        """
        all_nonzero = True
        pos_sum = 0
        for leg in self._legs:
            pos = context.stra_get_position(leg.code)
            if pos == 0:
                all_nonzero = False
            lots = leg.lots
            if leg.ratio > 0:
                pos_sum += pos * lots
            else:
                pos_sum -= pos * lots
        if not all_nonzero:
            return 0
        if pos_sum > 0:
            return 1
        elif pos_sum < 0:
            return -1
        return 0