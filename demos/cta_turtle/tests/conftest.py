"""tests/conftest.py — sys.path 注入 + 无 wtpy 环境降级桩

CI (无 C++ 动态库) 场景: 注入最小 wtpy 桩, 使纯逻辑层
(引擎/指标/过滤器/风控/配置/组合合成 + StraTurtle 壳逻辑) 全部可测;
依赖完整 wtpy 运行时的测试 (monitor db 等) 自动跳过。
真实 wtpy 存在时桩与跳过均不生效。
"""
from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]
for sub in ("", "helpers"):
    p = ROOT / sub if sub else ROOT
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# 需要真实 wtpy 运行时 (C++ 动态库/monitor 组件) 的测试模块名片段
_RUNTIME_ONLY = ("test_init_monitor_db",)

WTPY_STUBBED = False

try:
    import wtpy  # noqa: F401
except ImportError:   # CI/纯逻辑环境: 最小桩
    WTPY_STUBBED = True
    _wt = types.ModuleType("wtpy")

    class _NamedObj:
        def __init__(self, name: str = "", *a, **k):
            self._name = name

        def name(self) -> str:
            return self._name

    class BaseCtaStrategy(_NamedObj):
        pass

    class CtaContext:
        pass

    class EngineType:
        ET_CTA = 0

    _wt.BaseCtaStrategy = BaseCtaStrategy
    _wt.CtaContext = CtaContext
    _wt.EngineType = EngineType
    sys.modules["wtpy"] = _wt

    # CsvDataLoader 依赖的最小接口 (真实 ctypes 布局, 验证字段写入口径)
    import ctypes

    class WTSBarStruct(ctypes.Structure):
        _fields_ = [("date", ctypes.c_uint32),
                    ("reserve", ctypes.c_uint32),
                    ("time", ctypes.c_uint64),
                    ("open", ctypes.c_double),
                    ("high", ctypes.c_double),
                    ("low", ctypes.c_double),
                    ("close", ctypes.c_double),
                    ("settle", ctypes.c_double),
                    ("money", ctypes.c_double),
                    ("vol", ctypes.c_double),
                    ("hold", ctypes.c_double),
                    ("diff", ctypes.c_double)]
        _pack_ = 8

    class BaseExtDataLoader:
        pass

    _core = types.ModuleType("wtpy.WtCoreDefs")
    _core.WTSBarStruct = WTSBarStruct
    _ext = types.ModuleType("wtpy.ExtModuleDefs")
    _ext.BaseExtDataLoader = BaseExtDataLoader
    sys.modules["wtpy.WtCoreDefs"] = _core
    sys.modules["wtpy.ExtModuleDefs"] = _ext


def pytest_collection_modifyitems(config, items):
    if not WTPY_STUBBED:
        return
    for item in items:
        fname = getattr(item.fspath, "basename", "") or item.fspath.name
        if any(frag in fname for frag in _RUNTIME_ONLY):
            item.add_marker(pytest.mark.skip(
                reason="需要完整 wtpy 运行时 (CI 纯逻辑模式)"))
