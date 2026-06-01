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

    def save(self, context, key: str, value):
        """保存单个状态值"""
        full_key = f"{self._sub_name}_{key}"
        # user_save_data自动转str，复杂类型先JSON序列化
        if isinstance(value, (int, float, str, bool)):
            context.user_save_data(full_key, value)
        else:
            context.user_save_data(full_key, json.dumps(value))
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
