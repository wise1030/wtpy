#!/usr/bin/env python3
"""trim_to_truth — 持仓裁剪手术: 引擎+信号双账重建到券商真值。

背景与用途 (2026-11-09 otp 事件处置工具化):
共享账户上第三方策略 (otcpricer 等) 的平仓单会平掉**我们的**持仓
(CTP 账户级持仓可互换), 造成 信号/引擎账 > 券商实有。alert 模式
拒绝自动重建是正确的, 但需要人工手术把双账收敛到 broker 真值,
否则出场单量 > 实有仓 → 平完剩余空头后反向开出多仓 (翻转事故)。

本脚本: 按 rtdata.json 当前值, 把指定品种 (或全部失配品种) 的
引擎快照 (userdata/ud_<name>.json) 与信号账 (stradata/<name>.json)
重建为单层 (手数=broker 净空头, 成本=引擎层 vwap), 仓位连续性保留
(s1_last/direction/锚点不动, _armed 置 None 由重启 rearm 重建)。

⚠️ 必须在 run.py 停止后运行 (运行期周期快照会覆盖落盘修改):
  sudo systemctl stop wt-turtle-monitor.service   # 级联停 datakit+run.py
  python3 scripts/trim_to_truth.py --dry-run      # 预览失配清单
  python3 scripts/trim_to_truth.py                # 执行
  sudo systemctl start wt-turtle-monitor.service

幂等; 全量备份到 --backup 目录; 每次执行留一行 alerts.log 审计。
"""
import argparse
import datetime
import json
import os
import sys

BASE = "/home/ubuntu/projects/cta_turtle/deploy/cta_turtle/generated"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _code_prefix(std_code: str) -> str:
    parts = std_code.split(".")
    return ".".join(parts[:2]) + "." if len(parts) >= 2 else ""


def broker_nets(rtdata_path: str) -> dict:
    """rtdata.json → {品种前缀: 净多-净空} 非零项。"""
    with open(rtdata_path, encoding="utf-8") as f:
        rt = json.load(f)
    out = {}
    for p in rt.get("positions") or []:
        code = str(p.get("code", ""))
        parts = code.split(".")
        if len(parts) < 3:
            continue
        prefix = ".".join(parts[:2])
        s = p.get("short") or {}
        l = p.get("long") or {}
        net = (float(l.get("newvol", 0) or 0) + float(l.get("prevol", 0) or 0)) \
            - (float(s.get("newvol", 0) or 0) + float(s.get("prevol", 0) or 0))
        out[prefix] = out.get(prefix, 0.0) + net
    return {k: v for k, v in out.items() if v}


def _vwap(layers) -> float:
    tot = sum(l for l, _ in layers)
    return sum(l * p for l, p in layers) / tot if tot else 0.0


def _load_cfg_codes(cfg_path: str):
    from core.ConfigLoader import ConfigLoader
    cfg = ConfigLoader.load(cfg_path, warn=False)
    return [(i.code, "turtle_" + i.code.replace(".", "_"))
            for i in cfg.instruments]


def plan(group_dir: str, cfg_path: str):
    """失配清单: [(code, name, engine_lots, signal_lots, broker_net)]"""
    rt = os.path.join(group_dir, "generated", "traders", "simnow",
                      "rtdata.json")
    nets = broker_nets(rt)
    rows = []
    for code, name in _load_cfg_codes(cfg_path):
        prefix = _code_prefix(code)
        broker = nets.get(prefix.rstrip("."), 0.0)
        ud_p = os.path.join(group_dir, "generated", "userdata",
                            f"ud_{name}.json")
        sd_p = os.path.join(group_dir, "generated", "stradata",
                            f"{name}.json")
        eng = sig = None
        try:
            ud = json.load(open(ud_p))
            snap = json.loads(ud[f"{name}_engine"])
            eng = sum(int(sub.get("direction", 0)) *
                      sum(int(l) for l, _ in (sub.get("layers") or []))
                      for sub in (snap.get("subs") or {}).values())
        except (OSError, ValueError, KeyError):
            pass
        try:
            sd = json.load(open(sd_p))
            pos = next((p for p in sd.get("positions", [])
                        if p.get("code") == code), None)
            sig = int(round(float(pos.get("volume", 0)))) if pos else 0
        except (OSError, ValueError):
            pass
        if eng is not None and int(round(broker)) != eng:
            rows.append((code, name, eng, sig, int(round(broker))))
    return rows


def trim(group_dir: str, code: str, name: str, broker_net: int,
         backup_dir: str) -> str:
    """单品种双账重建; broker_net = 净多-空 (负=空头)。"""
    import shutil
    for rel in (f"userdata/ud_{name}.json", f"stradata/{name}.json"):
        src = os.path.join(group_dir, "generated", rel)
        dst = os.path.join(backup_dir, rel.replace("/", "_"))
        if os.path.exists(src) and not os.path.exists(dst):
            os.makedirs(backup_dir, exist_ok=True)
            shutil.copy2(src, dst)
    ud_p = os.path.join(group_dir, "generated", "userdata",
                        f"ud_{name}.json")
    ud = json.load(open(ud_p))
    snap = json.loads(ud[f"{name}_engine"])
    sub = snap["subs"]["S1"]
    layers = sub.get("layers") or []
    cost = _vwap(layers)
    d = -1 if broker_net < 0 else (1 if broker_net > 0 else 0)
    lots = abs(broker_net)
    n_last = (sub.get("layer_ns") or [None])[-1] or sub.get("_n")
    before = sum(l for l, _ in layers)
    if lots == 0:
        sub["layers"] = []
        sub["layer_ns"] = []
        sub["direction"] = 0
    else:
        sub["layers"] = [[lots, cost]]
        sub["layer_ns"] = [n_last] if n_last else []
        sub["direction"] = d
    sub["_stop_level"] = None
    sub["_add_level"] = None
    armed = sub.get("_armed") or {}
    for k in armed:
        armed[k] = None
    sub["_armed"] = armed
    snap["recon_miss"] = 0
    ud[f"{name}_engine"] = json.dumps(snap)
    tmp = ud_p + ".tmp"
    json.dump(ud, open(tmp, "w"), ensure_ascii=False)
    os.replace(tmp, ud_p)
    # 信号账
    sd_p = os.path.join(group_dir, "generated", "stradata", f"{name}.json")
    sd = json.load(open(sd_p))
    pos = next((p for p in sd["positions"] if p.get("code") == code), None)
    if pos is not None:
        d0 = (pos.get("details") or [{}])[-1]
        detail = dict(d0)
        detail.update({"long": d > 0, "volume": float(lots),
                       "price": round(cost, 6)})
        pos["volume"] = float(broker_net)
        pos["details"] = [detail] if lots else []
        pos["frozen"] = 0.0
    sd["signals"] = {}
    tmp = sd_p + ".tmp"
    json.dump(sd, open(tmp, "w"), ensure_ascii=False)
    os.replace(tmp, sd_p)
    return f"{code}: {before} -> {broker_net} @ {cost:.2f}"


def main():
    ap = argparse.ArgumentParser(description="trim books to broker truth")
    ap.add_argument("--group",
                    default="/home/ubuntu/projects/cta_turtle/deploy/cta_turtle")
    ap.add_argument("--config", default="")
    ap.add_argument("--code", default="", help="只处理指定 code (缺省=全部失配)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--backup", default="/home/ubuntu/backup_trim_" +
                    datetime.datetime.now().strftime("%Y%m%d_%H%M"))
    args = ap.parse_args()
    cfg_path = args.config or os.path.join(args.group, "config",
                                           "turtle_config.yaml")
    rows = plan(args.group, cfg_path)
    if args.code:
        rows = [r for r in rows if r[0] == args.code]
    if not rows:
        print("无失配品种 (engine == broker)")
        return
    print("失配清单 (engine != broker):")
    for code, name, eng, sig, broker in rows:
        print(f"  {code:16s} engine={eng:6d} signal={sig:6d} broker={broker:6d}")
    if args.dry_run:
        return
    if input("确认执行? [y/N] ").strip().lower() != "y":
        return
    for code, name, eng, sig, broker in rows:
        print("OK", trim(args.group, code, name, broker, args.backup))
    # 审计留痕
    try:
        log = os.path.join(args.group, "Logs", "alerts.log")
        with open(log, "a", encoding="utf-8") as f:
            f.write("%s trim_to_truth | %s\n"
                    % (datetime.datetime.now().strftime("%F %T"),
                       "; ".join(f"{c}:{e}->{b}" for c, _, e, _, b in rows)))
    except OSError:
        pass
    print("BACKUP ->", args.backup)
    print("完成. 重启 run.py 后验证 restored 行.")


if __name__ == "__main__":
    main()
