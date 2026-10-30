# -*- coding: utf-8 -*-
"""事件级净目标与执行批次（纯值对象，便于一致性测试）。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class TargetIntent:
    code: str
    before: int
    after: int

    @property
    def delta(self) -> int:
        return self.after - self.before

    @property
    def lots(self) -> int:
        return abs(self.delta)

    @property
    def side(self) -> int:
        return 1 if self.delta > 0 else (-1 if self.delta < 0 else 0)

    @property
    def offset(self) -> str:
        """目标净变化对应的订单性质；跨零反向需拆单，不能直接确认。"""
        if self.delta == 0:
            return "none"
        if self.before * self.after < 0:
            return "mixed"
        if self.before == 0:
            return "open"
        if self.after == 0:
            return "close"
        if self.before > 0:
            return "close" if self.after < self.before else "open"
        return "close" if self.after > self.before else "open"

    def snapshot(self) -> dict:
        return {"code": self.code, "before": self.before, "after": self.after}

    @classmethod
    def restore(cls, data: dict):
        return cls(code=str(data["code"]), before=int(data["before"]),
                   after=int(data["after"]))
