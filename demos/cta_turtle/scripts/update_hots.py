#!/usr/bin/env python3
"""hots.json 主力映射每日自动更新 (P0-1)

痛点: hots.json 此前靠人工跑 pick_hot_sina + rsync, 主力换月无感知
→ datakit 按旧映射合成 HOT K线(通道基准漂移), 策略交易旧合约
(流动性枯竭)。本脚本每日盘后自动更新并联动重启。

流程:
1. contracts.json 筛未过期合约 → 新浪持仓量判定主力 (stdlib 直连,
   无 akshare 依赖, 复用 pick_hot_sina 纯逻辑);
2. 与 hots.json 现有末规则比对, 无变更 → 静默退出;
3. 有变更 → merge_hots 追加规则 (原子替换 + .bak 备份)
   → kill datakit (WatchDog ~12s 拉起, hots 变更须重启生效)
   → webhook 告警变更明细。

调度: systemd timer 每日 20:35 (盘后安全窗口, 在 20:30 的
wt-turtle-contracts.timer 之后, 距夜盘 21:00 留够启动链时间)。

用法:
    python3 scripts/update_hots.py --common ../common \
        --config config/turtle_config.yaml
    python3 scripts/update_hots.py --dry-run     # 只打印不变更
"""
import argparse
import datetime
import json
import os
import signal
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from pick_hot_sina import (PRODUCTS, active_contracts, pick_hots,  # noqa: E402
                           merge_hots)


def _current_hots(hots: dict) -> dict:
    """现值快照: {exchg.pid: to} (变更比对用)"""
    cur = {}
    for exchg, prods in hots.items():
        for pid, rules in (prods or {}).items():
            if rules:
                cur[f"{exchg}.{pid}"] = rules[-1]["to"]
    return cur


def _notify(cfg_path: str, text: str):
    """读 turtle_config.yaml notify 段发 webhook (未配置/失败静默)"""
    try:
        import yaml
        sys.path.insert(0, os.path.join(ROOT, ".."))
        from helpers.notifier import WebhookNotifier
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        n = cfg.get("notify") or {}
        if not n.get("enabled"):
            return
        WebhookNotifier(urls=n.get("urls"), wtype=n.get("type",
                        "generic")).send("hots_roll", text)
    except Exception:
        pass


def _restart_datakit():
    """kill datakit/run.py 由 WatchDog 拉起 (hots 变更须重启生效)"""
    killed = []
    for pat in ("venv/bin/python runDt.py", "venv/bin/python run.py"):
        try:
            out = subprocess.run(["pgrep", "-f", pat],
                                 capture_output=True, text=True,
                                 timeout=10).stdout.split()
            for pid in out:
                os.kill(int(pid), signal.SIGTERM)
                killed.append(f"{pat}(pid={pid})")
        except Exception as e:
            print(f"[hots] restart {pat} failed: {e}", file=sys.stderr)
    return killed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--common", type=str,
                    default=os.path.abspath(os.path.join(ROOT, "..",
                                                         "..", "common")))
    ap.add_argument("--config", type=str,
                    default=os.path.abspath(os.path.join(
                        ROOT, "..", "config", "turtle_config.yaml")))
    ap.add_argument("--today", type=int, default=0,
                    help="判定日 YYYYMMDD, 缺省=今天")
    ap.add_argument("--dry-run", action="store_true", help="只打印不变更")
    ap.add_argument("--no-restart", action="store_true",
                    help="变更后不重启 datakit (测试/手工场景)")
    args = ap.parse_args()
    today = args.today or int(datetime.datetime.now().strftime("%Y%m%d"))

    contracts_path = os.path.join(args.common, "contracts.json")
    hots_path = os.path.join(args.common, "hots.json")
    with open(contracts_path, encoding="utf-8") as f:
        contracts = json.load(f)

    candidates = {}
    for key, (exchg, pid) in PRODUCTS.items():
        codes = active_contracts(contracts, exchg, pid, today)
        if codes:
            candidates[key] = codes
    if not candidates:
        print("[hots] 无候选合约 (contracts.json 异常?), 拒绝变更",
              file=sys.stderr)
        return 1

    picked = pick_hots(candidates)
    empty = [k for k, (h, _) in picked.items() if not h]
    if empty:
        print(f"[hots] 警告: {len(empty)} 品种无持仓数据 {empty}",
              file=sys.stderr)

    with open(hots_path, encoding="utf-8") as f:
        hots = json.load(f)
    before = _current_hots(hots)
    merge_hots(hots, picked, today)
    after = _current_hots(hots)
    diff = {k: (before.get(k), after[k]) for k in after
            if before.get(k) != after[k]}

    if not diff:
        print(f"[hots] {today} 主力映射无变更 ({len(after)} 品种)")
        return 0

    detail = "; ".join(f"{k}: {a or '-'}→{b}" for k, (a, b) in diff.items())
    print(f"[hots] {today} 主力换月: {detail}")
    if args.dry_run:
        print("[hots] dry-run, 不写入不重启")
        return 0

    # 原子替换 + 备份 (与 update_contracts.py 同口径)
    bak = hots_path + ".bak"
    with open(hots_path, encoding="utf-8") as f, \
            open(bak, "w", encoding="utf-8") as g:
        g.write(f.read())
    tmp = hots_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(hots, f, ensure_ascii=False, indent=1)
    os.replace(tmp, hots_path)
    print(f"[hots] hots.json 已更新 (备份 {bak})")

    killed = [] if args.no_restart else _restart_datakit()
    if killed:
        print(f"[hots] 已重启: {', '.join(killed)} (WatchDog 自动拉起)")
    _notify(args.config, f"海龟主力换月 {today}: {detail}"
            + (f", 已重启 {len(killed)} 进程" if killed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
