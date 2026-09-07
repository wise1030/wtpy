"""Webhook 推送适配器测试 (打桩 _post, 不发真实请求)"""
import helpers.notifier as nf
from helpers.notifier import WebhookNotifier, NullNotifier


def test_disabled_noop():
    n = WebhookNotifier(urls=["http://x"], enabled=False)
    assert n.send("k", "t") is False
    assert NullNotifier().send("k", "t") is False


def test_empty_urls_disables():
    assert WebhookNotifier(urls=[]).enabled is False


def test_send_posts_wechat_payload(monkeypatch):
    calls = []

    def fake_post(url, payload, timeout):
        calls.append((url, payload, timeout))
        return True
    monkeypatch.setattr(nf, "_post", fake_post)
    n = WebhookNotifier(urls=["u1", "u2"], wtype="wechat", cooldown=0)
    assert n.send("reconcile", "rebuilt 10@3500.00") is True
    assert len(calls) == 2
    _, p, _ = calls[0]
    assert p["msgtype"] == "text"
    assert "[reconcile]" in p["text"]["content"]
    assert "rebuilt" in p["text"]["content"]


def test_generic_payload_shape(monkeypatch):
    got = {}

    def fake_post(url, payload, timeout):
        got.update(payload)
        return True
    monkeypatch.setattr(nf, "_post", fake_post)
    n = WebhookNotifier(urls=["u"], wtype="generic")
    n.send("session", "pos=10")
    assert got["title"] == "session"
    assert got["text"] == "pos=10"
    assert isinstance(got["ts"], int)


def test_cooldown_dedup(monkeypatch):
    n_called = []

    def fake_post(url, payload, timeout):
        n_called.append(1)
        return True
    monkeypatch.setattr(nf, "_post", fake_post)
    n = WebhookNotifier(urls=["u"], cooldown=3600)
    assert n.send("risk", "a") is True
    assert n.send("risk", "b") is False      # 冷却期内静默
    assert len(n_called) == 1
    assert n.send("other", "c") is True      # 不同 key 不受冷却


def test_all_urls_fail_returns_false(monkeypatch):
    monkeypatch.setattr(nf, "_post", lambda *a: False)
    n = WebhookNotifier(urls=["u"])
    assert n.send("k", "t") is False


def test_strategy_reconcile_alert_hooked():
    """对账重建触发 webhook 推送 (注入记录桩)"""
    from StraTurtle import StraTurtle
    from schemas import InstrumentConfig, TurtleBaseConfig
    from test_reconcile import ReconCtx, _closes

    sent = []

    class Rec:
        def send(self, key, text):
            sent.append((key, text))
            return True

    inst = InstrumentConfig(code="SHFE.rb.HOT", period="d1", system="S1",
                            units=1)
    stra = StraTurtle("tn", inst, TurtleBaseConfig(recon_patience=2),
                      notify=Rec())
    stra._engine.confirm_enter(1, 100, 3450.0)
    ctx = ReconCtx(pos=0)
    stra._reconcile(ctx, _closes())
    stra._reconcile(ctx, _closes())
    keys = [k for k, _ in sent]
    assert any("reconcile" in k for k in keys)
