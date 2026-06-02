"""
ArbExecHelper — 套利执行协调器

完整状态机实现:
    idle -> opening -> opened -> closing -> idle

P1-2优化: LEG_TIMEOUT/MAX_RETRY从ExecConfig配置读取，不再硬编码。

v10: 去除emit_intents/TargetIntent/PositionRouter依赖, 去除_pos_getter间接层。
仓位读取统一走context.stra_get_position。
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
            leg_timeout: 单腿超时(秒/回测tick数)，默认5
            max_retry: 最大追单次数，默认3
        """
        self._legs = legs
        self._pending = {}
        self._filled = {}
        self._state = self.STATE_IDLE
        self._direction = 0  # 当前持仓方向: 1=正向, -1=反向, 0=无
        self._reverse_after = False
        self._pending_direction = 0
        self._is_backtest = is_backtest
        self._leg_timeout = leg_timeout
        self._max_retry = max_retry

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
            sub_config: ArbCoreConfig — 结构化配置
            signal: SignalResult — 结构化信号
        """
        if signal.entry_signal > 0:
            direction = 1
        elif signal.entry_signal < 0:
            direction = -1
        else:
            direction = 0

        if self._state == self.STATE_IDLE:
            if direction != 0:
                self._open_position(context, sub_config, direction)

        elif self._state == self.STATE_OPENED:
            should_exit = signal.should_exit
            should_reverse = signal.should_reverse

            if should_reverse and direction != self._direction:
                self._close_position(context, sub_config, reverse_after=True)
                self._pending_direction = direction
            elif should_exit:
                self._close_position(context, sub_config, reverse_after=False)

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
                    code, target, f"Opn_{sub_config.name}_{code}")
                self._pending[code] = {
                    'target': target,
                    'retry_count': 0,
                    'timestamp': _time.time()
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
                    code, 0, f"Cls_{sub_config.name}_{code}")
                self._pending[code] = {
                    'target': 0,
                    'retry_count': 0,
                    'timestamp': _time.time()
                }

    def close_all(self, context, sub_config, tag: str = "RC"):
        """紧急全平（风控调用）

        tag prefix budget: 31 - len(sub_config.name) - 1 - len(code).
        Aliases: RC=RiskClose, BEC=BacktestEndClose, EMG=Emergency, RT=Retry.
        """
        has_position = False
        for leg in self._legs:
            code = leg.code
            cur_pos = context.stra_get_position(code)
            if cur_pos != 0:
                context.stra_set_position(
                    code, 0, f"{tag}_{sub_config.name}_{code}")
                self._pending[code] = {
                    'target': 0,
                    'retry_count': 0,
                    'timestamp': _time.time()
                }
                has_position = True

        if has_position:
            self._state = self.STATE_CLOSING
            self._reverse_after = False
            self._pending_direction = 0
        else:
            self._pending.clear()
            self._filled.clear()
            self._state = self.STATE_IDLE
            self._direction = 0
            self._reverse_after = False
            self._pending_direction = 0

    def restore_state(self, direction: int):
        """恢复持久化状态"""
        self._direction = direction
        self._state = self.STATE_OPENED
        self._pending.clear()
        self._filled.clear()
        self._reverse_after = False
        self._pending_direction = 0

    def reset(self):
        """公共重置方法"""
        self._state = self.STATE_IDLE
        self._direction = 0
        self._pending.clear()
        self._filled.clear()
        self._reverse_after = False
        self._pending_direction = 0

    def on_tick_check(self, context, sub_config):
        """tick回调中检查成交状态和追单"""
        for code, pending in list(self._pending.items()):
            cur_pos = context.stra_get_position(code)
            if cur_pos == pending['target']:
                self._filled[code] = pending
                del self._pending[code]
                continue

            should_retry = False
            if self._is_backtest:
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
                        f"RT_{sub_config.name}_{code}_{pending['retry_count']}")
                    pending['retry_count'] += 1
                    pending['timestamp'] = _time.time()
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
                            f"EMG_{sub_config.name}_{leg.code}")
                    self._pending.clear()
                    self._filled.clear()
                    self._state = self.STATE_IDLE
                    self._direction = 0
                    self._reverse_after = False
                    self._pending_direction = 0
                    return

        if len(self._pending) == 0 and len(self._filled) > 0:
            self._filled.clear()

            if self._state == self.STATE_OPENING:
                self._state = self.STATE_OPENED
            elif self._state == self.STATE_CLOSING:
                if self._reverse_after:
                    new_dir = self._pending_direction
                    self._reverse_after = False
                    self._pending_direction = 0
                    if new_dir != 0:
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
        """计算各腿目标仓位"""
        targets = []
        for leg in self._legs:
            if leg.ratio > 0:
                target = direction * leg.lots
            else:
                target = -direction * leg.lots
            targets.append({'code': leg.code, 'target': target})
        return targets

    def infer_direction(self, context) -> int:
        """根据各腿持仓推断当前套利方向"""
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
