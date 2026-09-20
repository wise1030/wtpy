#!/usr/bin/env python3
"""运维定时器失败告警共享 helper (P1-4)

背景: 三个 systemd timer (contracts 20:30 / hots 20:35 / hotdaily
20:40) 全部 fail-silent — 新浪不可达/CTP 断连/DSB 重建失败都只打
本地日志, 无人值守时换月静默顺延 (2026-09 两次静默降级事故都是
人类看日志才发现)。本 helper 让每个定时器失败时推送 webhook,
与策略运行时的 notifier 共用 turtle_config.yaml notify 段配置。

失败语义: 失败要"响", 成功不必吱声 (hots 换月成功仍单独通知)。
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.dirname(ROOT)


def notify(event: str, text: str, cfg_path: str = ""):
    """读 turtle_config.yaml notify 段发 webhook (未配置/失败静默)。

    cfg_path 缺省 = <包根>/config/turtle_config.yaml (即仓库内路径);
    部署目录里 scripts/ 与 config/ 同级, 该缺省同样命中。"""
    try:
        import yaml
        if not cfg_path:
            cfg_path = os.path.join(_PKG, "config", "turtle_config.yaml")
            if not os.path.exists(cfg_path):
                cfg_path = os.path.join(_PKG, "turtle_config.yaml")
        sys.path.insert(0, _PKG)
        from helpers.notifier import WebhookNotifier
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        n = cfg.get("notify") or {}
        if not n.get("enabled"):
            return
        WebhookNotifier(urls=n.get("urls"),
                        wtype=n.get("type", "generic")).send(event, text)
    except Exception:
        pass
