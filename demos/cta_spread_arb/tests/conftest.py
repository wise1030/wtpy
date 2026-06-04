"""tests/conftest.py — 通用 fixtures 与 sys.path 注入"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for sub in ("", "strategies", "helpers", "factors", "core"):
    p = ROOT / sub if sub else ROOT
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


class FakeContext:
    """轻量 CtaContext stub — 满足 ArbExecHelper/StateManager/RiskMgr 测试需要。"""

    def __init__(self):
        self.positions = {}        # code -> qty
        self.profits = {}          # code -> pnl
        self.orders = []           # list[(code, target, tag)]
        self.kv = {}               # user_save_data store
        self.logs = []             # list[(level, text)]

    # ---- 仓位 ----
    def stra_get_position(self, code, *args, **kwargs):
        return self.positions.get(code, 0)

    def stra_set_position(self, code, target, tag=""):
        self.orders.append((code, target, tag))
        # 默认行为: 立即成交, 测试用例可在调用前后重置
        self.positions[code] = target

    def stra_get_position_profit(self, code):
        return self.profits.get(code, 0.0)

    # ---- KV 持久化 ----
    def user_save_data(self, key, val):
        self.kv[key] = str(val) if not isinstance(val, str) else val

    def user_load_data(self, key, default=None, vtype=str):
        raw = self.kv.get(key)
        if raw is None or raw == "":
            return default
        if vtype is int:
            try:
                return int(raw)
            except (ValueError, TypeError):
                return default
        if vtype is float:
            try:
                return float(raw)
            except (ValueError, TypeError):
                return default
        if vtype is bool:
            return raw in ("True", "true", "1")
        return raw

    # ---- 日志 ----
    def stra_log_text(self, text, level=1):
        self.logs.append((level, text))

    def stra_get_time(self):
        return 1430  # HHMM 格式


def make_leg(code, lots=1, multiplier=10, ratio=1):
    from schemas import LegConfig
    return LegConfig(code=code, lots=lots, multiplier=multiplier, ratio=ratio)


def make_sub_config(name="test", legs=None):
    from schemas import ArbCoreConfig
    if legs is None:
        legs = [make_leg("SHFE.rb.2510"), make_leg("DCE.i.2510", ratio=-1)]
    return ArbCoreConfig(
        name=name,
        legs=legs,
        main_period="m5",
        main_bar_cnt=300,
        aux_periods=[],
    )


def make_signal(entry=0.0, should_exit=False, should_reverse=False, direction=None):
    from schemas import SignalResult
    if direction is None:
        if entry > 0:
            direction = 1
        elif entry < 0:
            direction = -1
        else:
            direction = 0
    return SignalResult(
        entry_signal=entry,
        direction=direction,
        should_trade=(entry != 0),
        should_exit=should_exit,
        should_reverse=should_reverse,
    )
