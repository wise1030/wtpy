# -*- coding: utf-8 -*-
from execution.batch import TargetIntent


def test_net_intent_side_lots_offset_and_snapshot():
    intent = TargetIntent("X", 0, 60)
    assert intent.delta == 60 and intent.lots == 60
    assert intent.side == 1 and intent.offset == "open"
    assert TargetIntent.restore(intent.snapshot()) == intent


def test_net_intent_close_and_cross_zero():
    assert TargetIntent("X", 100, 40).offset == "close"
    assert TargetIntent("X", -100, -40).offset == "close"
    assert TargetIntent("X", 100, -20).offset == "mixed"
    assert TargetIntent("X", 100, 0).offset == "close"
    assert TargetIntent("X", 0, 0).offset == "none"
