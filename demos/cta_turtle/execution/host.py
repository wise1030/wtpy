# -*- coding: utf-8 -*-
"""A1 尾项: 显式依赖面 (StrategyHost).

四步拆分 (A1-1~A1-4) 后, execution/ 各模块持有策略回指 ``self.s`` 作为
过渡态 —— 模块可以任意抓策略的私有属性, 依赖面隐式且不受约束
(2026-09 教训: ``import os`` / ``_GROUP_DIR`` 漏迁移时被 try/except 静默
吞掉, 真值/成交证据链静默降级, 正是"隐式依赖不受审计"的活证).

本模块把回指收敛为**声明式依赖面**:

- 模块不再持有策略对象, 只持有 ``StrategyHost``;
- 每个模块在类级 ``DEPS`` 元组显式声明其全部依赖 (读+写);
- 访问未声明的依赖立即抛 ``AttributeError`` ( loud failure,
  测试套件会在第一时间捕获), 而不是静默工作 or 静默吞掉;
- 已声明依赖在运行时解析到策略的同一活对象, 语义零漂移
  (懒解析: on_init 才赋值的 ``_inst/_volscale`` 等天然兼容).

注意: 这是"显式声明 + 强制审计", 不是值对象注入 —— 真正的
ExecutionModel (撮合/证据/时钟接口, 值语义) 是 A2-v2, 按方案留待
h1 组合二期边界稳定后定义.
"""
from __future__ import annotations


class StrategyHost:
    """策略显式依赖面: 白名单代理.

    ``__getattr__/__setattr__`` 只对声明过的依赖名放行, 且读写都落到
    策略的同一属性上 (模块间共享的可变状态如 ``_truth/_fill_pending``
    因此保持单一事实源). 内部槽位 (``_stra/_deps``) 用 ``object.__setattr__``
    写入实例 dict, 不经过重写的方法, 无递归.
    """

    def __init__(self, stra, *deps: str):
        object.__setattr__(self, "_stra", stra)
        object.__setattr__(self, "_deps", frozenset(deps))

    def __getattr__(self, item: str):
        deps = object.__getattribute__(self, "_deps")
        if item in deps:
            return getattr(object.__getattribute__(self, "_stra"), item)
        raise AttributeError(
            f"StrategyHost: 未声明的依赖 '{item}' —— 请在 {self._owner_name()} "
            f"的 DEPS 中显式声明 (隐式抓策略私有属性已被禁止)"
        )

    def __setattr__(self, item: str, value):
        deps = object.__getattribute__(self, "_deps")
        if item in deps:
            setattr(object.__getattribute__(self, "_stra"), item, value)
            return
        raise AttributeError(
            f"StrategyHost: 未声明的依赖 '{item}' (写) —— 请在 "
            f"{self._owner_name()} 的 DEPS 中显式声明"
        )

    def _owner_name(self) -> str:
        # 仅用于报错文案; host 不知晓消费方, 由模块 DEPS 文档说明
        return "对应模块"
