#!/usr/bin/env python3
"""生产 webhook 自检：只有真实推送成功才返回 0，不伪造启用状态。"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from core.ConfigLoader import ConfigLoader                 # noqa: E402
from core.notifier import WebhookNotifier                  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(
        ROOT, "config", "turtle_config.yaml"))
    ap.add_argument("--text", default="cta_turtle webhook selftest")
    args = ap.parse_args()
    cfg = ConfigLoader.load(args.config, warn=False)
    if not cfg.notify.enabled:
        print("[notify] FAIL: notify.enabled=false；请先配置真实 webhook",
              file=sys.stderr)
        return 2
    notifier = WebhookNotifier(urls=cfg.notify.urls,
                               wtype=cfg.notify.type,
                               cooldown=cfg.notify.cooldown)
    ok = notifier.send("selftest", args.text)
    print(f"[notify] selftest {'OK' if ok else 'FAILED'} "
          f"(type={cfg.notify.type}, urls={len(cfg.notify.urls)})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
