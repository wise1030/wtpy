"""
StateManager — 状态持久化管理(v4)

v4修复: 使用CtaContext正确的API:
- user_save_data(key, val) — 保存(自动转str)
- user_load_data(key, defVal, vType) — 加载(需指定类型)

职责:
1. 统一管理策略运行时状态(direction, hold_bars, cooldown, realized_pnl等)
2. 通过WonderTrader的user_save_data/user_load_data实现持久化
3. 重启后自动恢复状态
4. 支持RiskMgr/ArbExecHelper状态的统一快照/恢复
"""
import json


class StateManager:
    """状态持久化管理器"""

    def __init__(self, sub_name: str):
        self._sub_name = sub_name
        self._keys = set()
        # P-5(2026-07): dirty-flag — 每key缓存上次序列化值, 未变化跳过写盘。
        # 原每bar 4次user_save_data ctypes + 2次json.dumps, 无论状态是否变化。
        self._last_serialized = {}

    def save(self, context, key: str, value):
        """保存单个状态值(dirty-flag: 值未变化时跳过)"""
        full_key = f"{self._sub_name}_{key}"
        # user_save_data自动转str，复杂类型先JSON序列化
        if isinstance(value, (int, float, str, bool)):
            serialized = value
        else:
            serialized = json.dumps(value)
        # P-5: 与上次序列化值相同则跳过(浮点直接比较, JSON字符串比较)
        if self._last_serialized.get(key, object()) == serialized:
            return
        context.user_save_data(full_key, serialized)
        self._last_serialized[key] = serialized
        self._keys.add(key)

    def load(self, context, key: str, default=None, vtype=None):
        """加载单个状态值

        v4修复: 使用user_load_data替代不存在的user_read_data
        - vtype=None: 尝试JSON解析(用于dict/list等复杂类型)
        - vtype=int/float/str: 指定简单类型
        """
        full_key = f"{self._sub_name}_{key}"
        if vtype is not None:
            # 简单类型: 直接用vType参数
            return context.user_load_data(full_key, default, vtype)
        else:
            # 复杂类型: 先当str读取，再JSON解析
            val = context.user_load_data(full_key, "", str)
            if val == "" or val is None:
                return default
            # 尝试JSON反序列化
            # CODE-4修复: JSON解析失败时返回default，而非返回原始字符串val
            # 原问题: 返回val(字符串)导致下游代码按dict/list访问时TypeError
            try:
                return json.loads(val)
            except (json.JSONDecodeError, TypeError):
                return default

    def save_risk_state(self, context, risk_mgr):
        """保存RiskMgr完整状态"""
        snapshot = risk_mgr.snapshot()
        self.save(context, 'risk_state', snapshot)

    def load_risk_state(self, context, risk_mgr):
        """恢复RiskMgr状态"""
        state = self.load(context, 'risk_state')
        if state and isinstance(state, dict):
            risk_mgr.restore(state)

    def save_direction(self, context, direction: int):
        """保存交易方向"""
        self.save(context, 'direction', direction)

    def load_direction(self, context, default: int = 0) -> int:
        """加载交易方向"""
        return self.load(context, 'direction', default, vtype=int)

    def save_all(self, context, direction: int, risk_mgr, last_signal_time: int = 0):
        """一次性保存所有状态"""
        self.save_direction(context, direction)
        self.save_risk_state(context, risk_mgr)
        self.save(context, 'last_signal_time', last_signal_time)

    def load_all(self, context, risk_mgr):
        """一次性恢复所有状态"""
        direction = self.load_direction(context)
        self.load_risk_state(context, risk_mgr)
        last_signal_time = self.load(context, 'last_signal_time', 0, vtype=int)
        return direction, last_signal_time

    # P0-9: ArbExecHelper 执行状态持久化
    def save_exec_state(self, context, exec_helper):
        """保存ArbExecHelper执行状态(状态机+pending/filled)"""
        state = {
            'state': exec_helper.state,
            'direction': exec_helper.direction,
            'reverse_after': exec_helper._reverse_after,
            'pending_direction': exec_helper._pending_direction,
            'pending': dict(exec_helper._pending),  # {code: {target,retry_count,timestamp}}
            'filled': dict(exec_helper._filled),    # {code: bool}
        }
        self.save(context, 'exec_state', state)

    def load_exec_state(self, context, exec_helper, diag=None):
        """恢复ArbExecHelper执行状态

        Args:
            context: CtaContext
            exec_helper: ArbExecHelper 实例
            diag: 可选 DiagLogger; 提供时, OPENING/CLOSING 被安全重置时会发 risk 事件,
                  让实盘报警钩子能捕获"潜在丢单"信号 (A4 修复)。

        Returns:
            dict | None: 重置告警 payload（仅在发生 OPENING/CLOSING→IDLE 强制重置时返回，
                         供 ArbCore.on_init 汇总后通过 stra_log_text 醒目输出）。
                         结构: {'sub_name', 'interrupted_state', 'direction',
                                'abandoned_pending'}。无异常返回 None。
        """
        state = self.load(context, 'exec_state')
        if state and isinstance(state, dict):
            exec_helper._state = state.get('state', exec_helper.STATE_IDLE)
            exec_helper._direction = state.get('direction', 0)
            exec_helper._reverse_after = state.get('reverse_after', False)
            exec_helper._pending_direction = state.get('pending_direction', 0)
            # 恢复pending/filled — 重启后pending的挂单大概率已失效,
            # 安全做法: 只恢复direction, 将state重置为IDLE让策略重新检测
            # 但保留原始state供日志诊断
            import logging
            logging.getLogger(__name__).info(
                f"exec_state restored: state={exec_helper._state}, "
                f"direction={exec_helper._direction}")
            # 安全策略: 重启后pending挂单已失效, 强制重置为IDLE
            # 如果有实际仓位, sync_state会在下一个tick中修正
            if exec_helper._state in (exec_helper.STATE_OPENING, exec_helper.STATE_CLOSING):
                interrupted_state = exec_helper._state
                # A4 修复: 从持久化 state 直接读 pending(load 路径未恢复 _pending),
                #         保证告警里能列出被废弃的挂单 code, 实盘可对账。
                pending_codes = list((state.get('pending') or {}).keys())
                exec_helper._state = exec_helper.STATE_IDLE
                exec_helper._pending.clear()
                exec_helper._filled.clear()
                exec_helper._state_since = None
                # A4: 发 risk 事件, 让实盘监控能感知"潜在丢单/孤腿"风险
                if diag is not None:
                    diag.risk(
                        self._sub_name,
                        rule='exec_state_reset',
                        detail=(f"interrupted state={interrupted_state}, "
                                f"abandoned_pending={pending_codes}, "
                                f"direction={exec_helper._direction}; "
                                f"manual position reconciliation recommended"),
                    )
                # R2-A: 返回 EMERGENCY payload, 由 ArbCore.on_init 汇总输出
                return {
                    'sub_name': self._sub_name,
                    'interrupted_state': interrupted_state,
                    'direction': exec_helper._direction,
                    'abandoned_pending': pending_codes,
                }
        return None
