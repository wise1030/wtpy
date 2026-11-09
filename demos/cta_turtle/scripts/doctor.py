#!/usr/bin/env python3
"""doctor (Phase 0, 2026-11-09) — 组合状态只读体检 CLI。

设计目标: 把"人工巡检要看十几个文件"收敛成一条命令, 输出按严重度分级的
结构化报告, 供盘前/盘后巡检与事故初判。全程只读, 绝不写任何状态。

检查项:
  [全局] 配置加载 + 口径摘要; hots.json 换月规则; K 线数据时效
         (复用 DataChecker.check_staleness); 交易通道真值 (rtdata.json
         年龄/资金/持仓品种数)。
  [策略] 每品种三方对账: 引擎快照净仓 (userdata/ud_<name>.json 内嵌)
         vs 框架信号仓 (stradata) vs 券商真值仓 (rtdata, 共享账户
         口径仅参考); 快照可解析性 (损坏=重启即 DEGRADED 的预告);
         profiles 指纹一致性; 心跳状态 (复用 heartbeat_watch 分类);
         快照备份存在性。

退出码: 0=无 error (warn/info 可存在); 1=有 error; --json 输出结构化。
用法:
  python3 scripts/doctor.py [--group <dir>] [--common <dir>]
      [--storage <dir>] [--config <yaml>] [--json] [-v]
"""
import argparse
import datetime
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.dirname(HERE)
sys.path.insert(0, _PKG)
sys.path.insert(0, HERE)

ERR, WARN, INFO = "error", "warn", "info"


def _add(out, level, section, msg):
    out.append({"level": level, "section": section, "msg": msg})


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), None
    except OSError as e:
        return None, f"missing: {e}"
    except ValueError as e:
        return None, f"corrupt: {e}"


# ----------------------------------------------------------------------
# 全局检查
# ----------------------------------------------------------------------
def check_config(cfg_path, out):
    from core.ConfigLoader import ConfigLoader
    try:
        cfg = ConfigLoader.load(cfg_path, warn=False)
    except Exception as e:
        _add(out, ERR, "config", f"加载失败: {e}")
        return None
    r = cfg.risk
    _add(out, INFO, "config",
         f"instruments={len(cfg.instruments)} entry_mode={cfg.base.entry_mode} "
         f"stop_n_mode={cfg.base.stop_n_mode} truth_mode="
         f"{cfg.base.truth_rebuild_mode} gate={r.account_gate_mode} "
         f"notify={'ON' if getattr(cfg, 'notify', None) and cfg.notify.enabled else 'OFF'}")
    _add(out, INFO, "config",
         f"资金: fallback={r.fallback_capital:,.0f} "
         f"portfolio_capital={r.portfolio_capital:,.0f}"
         + (" (分配未启用 — 复核报告 B1)" if r.portfolio_capital <= 0 else ""))
    return cfg


def check_hots(cfg, common_dir, out):
    path = os.path.join(common_dir, "hots.json")
    data, err = _read_json(path)
    if err:
        _add(out, ERR, "hots", err)
        return
    for inst in cfg.instruments:
        parts = inst.code.split(".")
        rules = (data.get(parts[0]) or {}).get(parts[1]) \
            if len(parts) >= 2 else None
        if not rules:
            _add(out, WARN, "hots",
                 f"{inst.code}: 无换月规则 (实盘 C++ 回放器空规则 segfault 风险)")
        else:
            no_delta = sum(1 for x in rules
                           if not float(x.get("oldclose") or 0)
                           or not float(x.get("newclose") or 0))
            _add(out, INFO, "hots",
                 f"{inst.code}: {len(rules)} 条规则"
                 + (f" ({no_delta} 条缺 oldclose/newclose — 复权 delta 需估算)"
                    if no_delta else ""))


def check_data(cfg, common_dir, storage_dir, out):
    from core.DataChecker import check_staleness
    try:
        for issue in check_staleness(cfg, common_dir, storage_dir):
            _add(out, WARN if issue.level == "warn" else INFO,
                 "data", f"{issue.code}: {issue.msg}")
    except Exception as e:
        _add(out, WARN, "data", f"时效检查失败: {e}")


def check_truth(group_dir, cfg, out):
    """rtdata.json 位置/年龄/资金/持仓; 返回 data 供策略级对账复用。"""
    import glob
    cands = glob.glob(os.path.join(group_dir, "generated", "traders", "*",
                                   "rtdata.json"))
    if not cands:
        _add(out, WARN, "truth", "rtdata.json 未找到 (真值链未落盘?)")
        return None
    path = max(cands, key=os.path.getmtime)
    data, err = _read_json(path)
    if err:
        _add(out, WARN, "truth", f"{os.path.basename(path)}: {err}")
        return None
    age = time.time() - os.path.getmtime(path)
    try:
        from core.LiveTruth import account_funds
        f = account_funds(data)
    except Exception:
        f = {}
    txt = (f"{os.path.basename(os.path.dirname(path))}: age={age:.0f}s, "
           f"balance={f.get('balance', 0):,.0f}, "
           f"available={f.get('available', 0):,.0f}, "
           f"margin={f.get('margin', 0):,.0f}")
    max_age = float(getattr(cfg.base, "truth_max_age", 86400) or 86400)
    _add(out, WARN if age > max_age else INFO, "truth",
         txt + (f" (>truth_max_age={max_age:.0f}s, 真值链疑似死亡)"
                if age > max_age else ""))
    return data


# ----------------------------------------------------------------------
# 策略级检查
# ----------------------------------------------------------------------
def _engine_net(snap: dict):
    """快照各子系统净仓合计。"""
    net = 0
    for sub in (snap.get("subs") or {}).values():
        d = int(sub.get("direction") or 0)
        lots = sum(int(l) for l, _ in (sub.get("layers") or []))
        net += d * lots
    return net


def check_strategy(name, code, group_dir, cfg, truth, session_ctx, out):
    sec = f"{code}"
    # 1) 引擎快照 (userdata 内嵌 JSON 字符串)
    ud_path = os.path.join(group_dir, "generated", "userdata",
                           f"ud_{name}.json")
    ud, err = _read_json(ud_path)
    snap, snap_net = None, None
    if err:
        _add(out, WARN, sec, f"userdata: {err} (重启将以空状态收养)")
    else:
        raw = (ud or {}).get(f"{name}_engine")
        if not raw:
            _add(out, WARN, sec, "userdata: 无引擎快照键 (从未持久化?)")
        else:
            try:
                snap = json.loads(raw)
                snap_net = _engine_net(snap)
            except ValueError as e:
                _add(out, ERR, sec,
                     f"快照 JSON 损坏: {e} — 重启即 DEGRADED 且禁止新开/加仓")
    # 2) profiles 指纹
    if snap is not None:
        try:
            from core.profiles import validate_snapshot
            validate_snapshot(snap, __import__(
                "core.profiles", fromlist=["resolve_profiles"])
                .resolve_profiles(
                    next(i for i in cfg.instruments if i.code == code),
                    cfg.base))
            _add(out, INFO, sec, "profiles 指纹: OK")
        except ValueError as e:
            _add(out, ERR, sec, f"profiles 指纹: {e}")
        except Exception as e:
            _add(out, WARN, sec, f"profiles 校验异常: {e}")
    # 3) 信号仓 (stradata)
    sig_net = None
    sd_path = os.path.join(group_dir, "generated", "stradata",
                           f"{name}.json")
    sd, err = _read_json(sd_path)
    if err:
        _add(out, WARN, sec, f"stradata: {err}")
    else:
        poss = (sd or {}).get("positions") or []
        pos = next((p for p in poss if p.get("code") == code), None)
        sig_net = int(round(float(pos.get("volume", 0)))) if pos else 0
    # 4) 券商真值 (共享账户口径, 仅参考)
    broker_net = None
    if truth is not None:
        try:
            from core.LiveTruth import net_position
            broker_net = int(round(net_position(truth, code)))
        except Exception:
            pass
    # 5) 三方对账结论
    if snap_net is not None and sig_net is not None and snap_net != sig_net:
        _add(out, ERR, sec,
             f"引擎净仓 {snap_net} ≠ 信号仓 {sig_net} — 重启将播种对账,"
             f" 下一日评估重建")
    if sig_net is not None and broker_net is not None \
            and sig_net != broker_net:
        _add(out, WARN, sec,
             f"信号仓 {sig_net} ≠ 券商仓 {broker_net} (共享账户含其他"
             f"策略/在途单, 仅供参考; 持续失配查 alerts.log)")
    if snap_net is not None and sig_net is not None \
            and snap_net == sig_net:
        _add(out, INFO, sec,
             f"持仓三方: engine=signal={snap_net}"
             + (f", broker={broker_net}" if broker_net is not None else ""))
    # 6) 心跳
    if session_ctx is not None:
        hb_path = os.path.join(group_dir, "generated", "heartbeat",
                               f"{name}.json")
        hb, err = _read_json(hb_path)
        now = time.time()
        mtime = os.path.getmtime(hb_path) if \
            os.path.exists(hb_path) else 0.0
        status, detail = session_ctx(hb, mtime, now)
        _add(out, WARN if status in ("process_silent", "tick_silent",
                                     "no_heartbeat", "degraded") else INFO,
             sec, f"heartbeat: {status}"
             + (f" ({detail})" if detail else ""))
    # 7) 快照备份
    bk = os.path.join(group_dir, "generated", "snapshots", f"{name}.json")
    if not os.path.exists(bk):
        _add(out, WARN, sec, "snapshots 备份缺失 (周期快照未运行?)")


def run(group_dir, common_dir, storage_dir, cfg_path, verbose=False):
    out = []
    cfg = check_config(cfg_path, out)
    if cfg is None:
        return out
    check_hots(cfg, common_dir, out)
    check_data(cfg, common_dir, storage_dir, out)
    truth = check_truth(group_dir, cfg, out)
    # 心跳分类上下文 (含交易时段判定)
    session_ctx = None
    try:
        import heartbeat_watch as hw
        holidays = hw._load_holidays(common_dir)
        sessions = hw._product_sessions(
            common_dir, [i.code for i in cfg.instruments])
        now_dt = datetime.datetime.now()

        def session_ctx(hb, mtime, now):    # noqa: F811
            code = session_ctx.code
            tpl = sessions.get(code)
            if tpl is None:
                return "closed", "no session info"
            return hw.classify(hb, mtime, now, 360.0, 180.0,
                               hw.market_open(now_dt, tpl, holidays))
    except Exception as e:
        _add(out, WARN, "heartbeat", f"分类器不可用: {e}")
        session_ctx = None
    for inst in cfg.instruments:
        name = "turtle_" + inst.code.replace(".", "_")
        if session_ctx is not None:
            session_ctx.code = inst.code   # type: ignore[attr-defined]
        check_strategy(name, inst.code, group_dir, cfg, truth,
                       session_ctx, out)
    return out


def _find_common(group_dir: str) -> str:
    """common 目录自适应: 本地开发 = <group>/../common; 服务器部署 =
    <group>/../../common (deploy/ 层)。显式存在者优先。"""
    for cand in (os.path.join(group_dir, "..", "common"),
                 os.path.join(group_dir, "..", "..", "common")):
        if os.path.exists(os.path.join(cand, "hots.json")):
            return os.path.abspath(cand)
    return os.path.abspath(os.path.join(group_dir, "..", "common"))


def main():
    ap = argparse.ArgumentParser(description="cta_turtle doctor (read-only)")
    ap.add_argument("--group", default=_PKG)
    ap.add_argument("--common", default="")
    ap.add_argument("--storage", default="")
    ap.add_argument("--config", default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    cfg_path = args.config or os.path.join(
        args.group, "config", "turtle_config.yaml")
    common = args.common or _find_common(args.group)
    storage = args.storage or os.path.abspath(os.path.join(common, "..",
                                                           "storage"))
    report = run(args.group, common, storage, cfg_path,
                 verbose=args.verbose)
    n_err = sum(1 for x in report if x["level"] == ERR)
    n_warn = sum(1 for x in report if x["level"] == WARN)
    if args.json:
        print(json.dumps({"errors": n_err, "warnings": n_warn,
                          "items": report}, ensure_ascii=False, indent=1))
    else:
        icon = {ERR: "✗", WARN: "!", INFO: "·"}
        for x in report:
            if x["level"] == INFO and not args.verbose:
                continue
            print(f"{icon[x['level']]} [{x['level']:5s}] "
                  f"{x['section']:20s} {x['msg']}")
        print(f"\ndoctor: {n_err} error(s), {n_warn} warning(s)")
    sys.exit(1 if n_err else 0)


if __name__ == "__main__":
    main()
