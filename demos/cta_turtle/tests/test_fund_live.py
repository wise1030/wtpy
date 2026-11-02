"""fund_live 旁路回归 (2026-11-02 生产事故修复).

事故: A1 迁移 (224997b) 把 _get_fees 改为薄转发 ``(self, context)``
但遗留 ``@staticmethod`` 装饰器 → 调用时 context 被绑到 self 形参,
TypeError, fund_live.csv 自 2026-10-30 部署起静默失效, Runner 日志
每秒刷 "fund_live dump failed: missing 1 required positional
argument: 'context'" (生产 2026-11-02 审计发现)。

口径: _dump_fund_live 必须真实写出 fee 列 (>0 场景), 且失败路径
只记日志不抛异常。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
sys.path.insert(0, os.path.dirname(__file__))

from test_stra_trigger import TrigCtx   # noqa: E402

import execution.telemetry as telemetry  # noqa: E402

from schemas import InstrumentConfig, TurtleBaseConfig  # noqa: E402
from StraTurtle import StraTurtle  # noqa: E402


class _FundCtx(TrigCtx):
    def __init__(self):
        super().__init__()
        self.funds = {0: 123.0, 3: 45.0}

    def stra_get_fund_data(self, flag=0):
        return self.funds.get(flag)


def _make():
    inst = InstrumentConfig(code="SHFE.rb.HOT", units=1)
    return StraTurtle("tf", inst, TurtleBaseConfig()), _FundCtx()


def test_fund_live_dump_writes_fee(tmp_path, monkeypatch):
    """薄转发 _get_fees 后 fund_live.csv 必须真实落盘且 fee 列非空"""
    monkeypatch.setattr(telemetry, "_GROUP_DIR", str(tmp_path))
    stra, ctx = _make()
    stra._dump_fund_live(ctx)
    path = tmp_path / "generated" / "outputs" / "tf" / "fund_live.csv"
    assert path.is_file()
    lines = path.read_text().strip().splitlines()
    assert lines[0] == "date,time,net_pnl,fee,position,price"
    row = lines[1].split(",")
    assert row[2] == "123.0"          # net_pnl
    assert row[3] == "45.0"           # fee ← 事故前恒为空 (TypeError)
    assert not any("fund_live dump failed" in t[1] for t in ctx.logs)


def test_fund_live_failure_is_log_only(tmp_path, monkeypatch):
    """写失败只记日志 (旁路绝不阻断交易)"""
    monkeypatch.setattr(telemetry, "_GROUP_DIR", str(tmp_path))
    stra, ctx = _make()
    # 制造写失败: 同路径已存在目录占位
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated" / "outputs").write_text("block")
    stra._dump_fund_live(ctx)
    assert any("fund_live dump failed" in t[1] for t in ctx.logs)


def test_get_fees_forwarder_signature():
    """回归事故本体: _get_fees 是普通方法, (context) 调用直达 FillTracker"""
    stra, ctx = _make()
    assert stra._get_fees(ctx) == 45.0
