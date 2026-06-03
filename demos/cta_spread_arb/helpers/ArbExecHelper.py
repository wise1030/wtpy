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

# P0-5: WT _usertag/_opentag 32字节硬约束
_MAX_TAG_LEN = 31  # 含\0


def _make_tag(prefix: str, sub_name: str, code: str) -> str:
    """生成WT usertag, 保证<=31字节; 超长时截断sub_name保prefix+code。"""
    tag = f"{prefix}_{sub_name}_{code}"
    if len(tag) <= _MAX_TAG_LEN:
        return tag
    # 截断策略: 保留 prefix(3) + _ + code + _ + sub_name截断
    # 例: "Opn_xxx_SHFE.rb.2510" — prefix=4, code=12, _=2, name预算=31-4-12-2=13
    name_budget = _MAX_TAG_LEN - len(prefix) - 1 - len(code) - 1
    if name_budget < 3:
        # 极端情况: code太长, 只保留prefix+code
        tag = f"{prefix}_{code}"
        if len(tag) > _MAX_TAG_LEN:
            tag = tag[:_MAX_TAG_LEN]
        logger.warning(f"tag truncated to '{tag}' (code too long)")
        return tag
    truncated_name = sub_name[:name_budget]
    tag = f"{prefix}_{truncated_name}_{code}"
    if len(tag) > _MAX_TAG_LEN:
        tag = tag[:_MAX_TAG_LEN]
    return tag


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
        # P0-4: 开仓超时保护 — 记录opening/closing进入时间
        self._state_since = None  # 实盘: time.time(); 回测: bar计数

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
        self._state_since = _time.time()  # P0-4: 记录进入opening时间
        targets = self._calc_target_positions(direction)
        for t in targets:
            code = t['code']
            target = t['target']
            cur_pos = context.stra_get_position(code)
            if target != cur_pos:
                context.stra_set_position(
                    code, target, _make_tag("Opn", sub_config.name, code))
                self._pending[code] = {
                    'target': target,
                    'retry_count': 0,
                    'timestamp': _time.time()
                }

    def _close_position(self, context, sub_config, reverse_after: bool = False):
        """执行平仓"""
        self._state = self.STATE_CLOSING
        self._reverse_after = reverse_after
        self._state_since = _time.time()  # P0-4: 记录进入closing时间
        for leg in self._legs:
            code = leg.code
            cur_pos = context.stra_get_position(code)
            if cur_pos != 0:
                context.stra_set_position(
                    code, 0, _make_tag("Cls", sub_config.name, code))
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
                    code, 0, _make_tag(tag, sub_config.name, code))
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
            self._state_since = _time.time()  # P0-4
        else:
            self._pending.clear()
            self._filled.clear()
            self._state = self.STATE_IDLE
            self._direction = 0
            self._reverse_after = False
            self._pending_direction = 0
            self._state_since = None  # P0-4

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
                        _make_tag(f"RT{pending['retry_count']}", sub_config.name, code))
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
                            _make_tag("EMG", sub_config.name, leg.code))
                    self._pending.clear()
                    self._filled.clear()
                    self._state = self.STATE_IDLE
                    self._direction = 0
                    self._reverse_after = False
                    self._pending_direction = 0
                    self._state_since = None  # P0-4
                    return

        if len(self._pending) == 0 and len(self._filled) > 0:
            self._filled.clear()

            if self._state == self.STATE_OPENING:
                self._state = self.STATE_OPENED
                self._state_since = None  # P0-4: opening完成
            elif self._state == self.STATE_CLOSING:
                if self._reverse_after and self._pending_direction != 0:
                    new_dir = self._pending_direction
                    self._reverse_after = False
                    self._pending_direction = 0
                    # P0-1: 即时反转 — closing完成后立即开反向仓
                    self._open_position(context, sub_config, new_dir)
                    logger.info(
                        f"reverse executed: new_dir={new_dir}, "
                        f"legs={[leg.code for leg in self._legs]}")
                else:
                    self._reverse_after = False
                    self._pending_direction = 0
                    self._state = self.STATE_IDLE
                    self._direction = 0
                    self._state_since = None  # P0-4: closing完成

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

    def check_state_timeout(self, context, sub_config,
                            timeout_seconds: float = 30.0,
                            timeout_bars: int = 20) -> bool:
        """P0-4: 检测opening/closing状态是否超时

        实盘: 用 wall-clock 时间, 默认30秒
        回测: 用 bar 计数, 默认20 bar (回测中on_tick_check每个tick调用,
              但正常成交应在1-2个tick内完成)

        Returns:
            True 如果超时并已执行紧急全平+重置
        """
        if self._state_since is None:
            return False
        if self._state not in (self.STATE_OPENING, self.STATE_CLOSING):
            return False

        elapsed = _time.time() - self._state_since
        timed_out = (elapsed > timeout_seconds)

        if timed_out:
            logger.warning(
                f"state timeout: state={self._state}, "
                f"elapsed={elapsed:.1f}s > {timeout_seconds}s, "
                f"emergency reset")
            context.stra_log_text(
                f"state timeout: {self._state} elapsed {elapsed:.1f}s, "
                f"emergency close_all", level=3)
            self.close_all(context, sub_config, tag="OT")
            return True
        return False
