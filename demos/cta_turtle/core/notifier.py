"""Webhook 推送适配器 (P2 平台化)

支持企业微信(bot)/钉钉(bot)/通用 JSON 三种 payload; stdlib 实现,
失败静默(返回 False)不阻断交易线程。按 key 冷却去重防刷屏。

接入点 (StraTurtle): 运行时对账重建 / 风控缩量 / 会话结束汇总。
配置 (turtle_config.yaml):
    notify:
      enabled: true
      type: wechat            # wechat | dingtalk | generic
      urls: ["https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"]
      cooldown: 300           # 同 key 秒级冷却
"""
import json
import time
import urllib.request


def _post(url: str, payload: dict, timeout: float) -> bool:
    """实际 HTTP POST (独立函数便于测试打桩)"""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception:
        return False


class WebhookNotifier:
    def __init__(self, urls: list = None, wtype: str = "generic",
                 timeout: float = 5.0, cooldown: float = 300.0,
                 enabled: bool = True):
        self.urls = [u for u in (urls or []) if u]
        self.wtype = wtype if wtype in ("wechat", "dingtalk",
                                        "generic") else "generic"
        self.timeout = float(timeout)
        self.cooldown = float(cooldown)
        self.enabled = bool(enabled) and bool(self.urls)
        self._last_sent = {}    # key -> monotonic ts

    def _payload(self, key: str, text: str) -> dict:
        content = f"[{key}] {text}"
        if self.wtype == "wechat":
            return {"msgtype": "text", "text": {"content": content}}
        if self.wtype == "dingtalk":
            return {"msgtype": "text", "text": {"content": content}}
        return {"title": key, "text": text,
                "ts": int(time.time())}

    def send(self, key: str, text: str) -> bool:
        """推送事件; 同 key 在冷却期内静默跳过。任一 url 成功即 True。"""
        if not self.enabled:
            return False
        now = time.monotonic()
        last = self._last_sent.get(key)
        if last is not None and now - last < self.cooldown:
            return False
        ok = False
        for u in self.urls:
            if _post(u, self._payload(key, text), self.timeout):
                ok = True
        if ok:
            self._last_sent[key] = now
        return ok


class NullNotifier:
    """禁用态占位 (零开销)"""

    def send(self, key: str, text: str) -> bool:
        return False
