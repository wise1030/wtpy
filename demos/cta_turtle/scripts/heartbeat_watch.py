#!/usr/bin/env python3
"""心跳 watchdog (Phase 0, 2026-11-09) — 进程存活 / tick 断流外部检测。

背景: 2026-09-28 商品行情通道中断, datakit ingest 正常但 his/rt/UDP
全断, 11 个商品策略全天零 tick — 策略层 on_tick/session_end 都不触发,
内部检测全部失明, 只能靠人工从盈亏异常发现。本脚本在进程外独立检测:

  1) 进程存活: generated/heartbeat/<name>.json 文件 mtime 超过
     --proc-timeout (默认 180s) 未更新 → process_silent
     (12 策略同进程, 单文件陈旧即代表整个 run.py 卡住/死亡);
  2) tick 断流: 交易时段内 (sessions.json 节段 + holidays.json 日历,
     夜盘跨零点段处理 to<from) heartbeat.last_tick_ts 超过
     --timeout (默认 360s) → tick_silent;
  3) 心跳自报 DEGRADED (快照恢复失败) → degraded;
  4) 心跳缺失 (策略从未写过, 如 run.py 未拉起) → no_heartbeat。

告警语义: 状态**转换**才推送 (bad→bad 不重复), 持续 bad 每 --remind
秒 (默认 1800) 提醒一次; 恢复 (bad→ok) 推送一条恢复通知。全部状态落
generated/heartbeat/.watch_state.json。任何自身异常走 timer_notify
推送并吞掉 (watchdog 绝不能自己崩成 silent)。

systemd: wt-turtle-heartbeat.timer 每分钟触发一次本脚本 (oneshot)。
回测/本地手工: python3 scripts/heartbeat_watch.py --once [--verbose]。
"""
import argparse
import datetime
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.dirname(HERE)          # 组合目录 (scripts/ 的父级)
sys.path.insert(0, _PKG)

BAD_STATES = ("no_heartbeat", "process_silent", "tick_silent", "degraded")


# ----------------------------------------------------------------------
# 纯函数 (可单测)
# ----------------------------------------------------------------------
def load_sessions(path: str) -> dict:
    """sessions.json → {模板名: {"sections": [{from,to}], ...}}。

    该文件历史上非 UTF-8 (GBK) 编码, 逐编码尝试; 损坏返回 {}。"""
    for enc in ("utf-8", "gbk"):
        try:
            with open(path, encoding=enc) as f:
                raw = json.load(f)
            out = {}
            for name, tpl in (raw or {}).items():
                if isinstance(tpl, dict):
                    out[name] = tpl
            return out
        except (UnicodeDecodeError, ValueError, OSError):
            continue
    return {}


def section_active(hhmm: int, sections: list) -> bool:
    """now(HHMM int) 是否落在任一交易节段内; 支持跨零点 (to < from)。"""
    for sec in sections or []:
        try:
            f = int(sec.get("from"))
            t = int(sec.get("to"))
        except (TypeError, ValueError):
            continue
        if f == t:
            continue
        if f < t:
            if f <= hhmm < t:
                return True
        else:                    # 跨零点: 21:00-01:00
            if hhmm >= f or hhmm < t:
                return True
    return False


def is_watch_day(day: datetime.date, holidays: set) -> bool:
    """是否工作日 (调休上班日未在 holidays.json 标注, 会漏检 — 已知边界,
    每周末/节假日覆盖绝大多数静默窗口, 可接受)。"""
    key = day.strftime("%Y%m%d")
    return day.weekday() < 5 and key not in holidays


def market_open(now: datetime.datetime, session_tpl: dict,
                holidays: set) -> bool:
    """now 是否处于该品种交易时段 (工作日 + 节段命中)。"""
    if not is_watch_day(now.date(), holidays):
        return False
    secs = (session_tpl or {}).get("sections") or []
    return section_active(now.hour * 100 + now.minute, secs)


def classify(hb: dict, f_mtime: float, now: float,
             tick_timeout: float, proc_timeout: float,
             is_open: bool) -> tuple:
    """单策略心跳 → (status, detail)。

    status ∈ ok | closed | no_heartbeat | process_silent | tick_silent |
    degraded。closed = 非交易时段不检测。"""
    if not is_open:
        return "closed", ""
    if hb is None:
        return "no_heartbeat", "heartbeat file missing"
    if now - f_mtime > proc_timeout:
        return "process_silent", \
            f"heartbeat file stale {now - f_mtime:.0f}s " \
            f"(>{proc_timeout:.0f}s), run.py likely hung/dead"
    tick_ts = float(hb.get("last_tick_ts") or 0.0)
    if tick_ts <= 0:
        return "tick_silent", "no tick ever recorded this run"
    age = now - tick_ts
    if age > tick_timeout:
        return "tick_silent", f"last tick {age:.0f}s ago " \
                              f"(>{tick_timeout:.0f}s) in session"
    if hb.get("degraded"):
        return "degraded", "strategy self-reports DEGRADED " \
                           "(snapshot restore failed)"
    return "ok", ""


def should_alert(prev: str, cur: str, last_alert_ts: float,
                 now: float, remind_secs: float) -> bool:
    """状态转换告警 + 持续 bad 周期提醒 (纯函数)。"""
    if cur == "ok" or cur == "closed":
        return prev in BAD_STATES          # 恢复通知
    if cur != prev:
        return True                        # 新 bad 状态
    if prev in BAD_STATES and now - last_alert_ts >= remind_secs:
        return True                        # 持续 bad 提醒
    return False


# ----------------------------------------------------------------------
# 组装
# ----------------------------------------------------------------------
def _load_holidays(common_dir: str) -> set:
    try:
        with open(os.path.join(common_dir, "holidays.json"),
                  encoding="utf-8") as f:
            raw = json.load(f) or {}
        return set(raw.get("CHINA") or [])
    except (OSError, ValueError):
        return set()


def _strategy_names(cfg) -> list:
    return [("turtle_" + i.code.replace(".", "_"), i.code)
            for i in cfg.instruments]


def _product_sessions(common_dir: str, codes: list) -> dict:
    """code → 该品种 session 模板 dict; 缺商品信息 → None (跳过检测)。"""
    try:
        with open(os.path.join(common_dir, "commodities.json"),
                  encoding="utf-8") as f:
            comms = json.load(f) or {}
    except (OSError, ValueError):
        return {}
    sessions = load_sessions(os.path.join(common_dir, "sessions.json"))
    out = {}
    for code in codes:
        parts = code.split(".")
        info = (comms.get(parts[0]) or {}).get(parts[1]) if \
            len(parts) >= 2 else None
        tpl_name = info.get("session") if isinstance(info, dict) else None
        out[code] = sessions.get(tpl_name) if tpl_name else None
    return out


def run_watch(group_dir: str, common_dir: str, cfg_path: str,
              tick_timeout: float, proc_timeout: float,
              remind_secs: float, verbose: bool = False) -> int:
    """执行一轮检测; 返回告警条数 (0=全绿)。"""
    from core.ConfigLoader import ConfigLoader
    from timer_notify import notify
    cfg = ConfigLoader.load(cfg_path, warn=False)
    holidays = _load_holidays(common_dir)
    hb_dir = os.path.join(group_dir, "generated", "heartbeat")
    state_path = os.path.join(hb_dir, ".watch_state.json")
    now = time.time()
    now_dt = datetime.datetime.now()
    names = _strategy_names(cfg)
    sessions = _product_sessions(common_dir, [c for _, c in names])
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f) or {}
    except (OSError, ValueError):
        state = {}

    alerts = 0
    lines = []
    for name, code in names:
        path = os.path.join(hb_dir, f"{name}.json")
        hb = None
        mtime = 0.0
        try:
            with open(path, encoding="utf-8") as f:
                hb = json.load(f)
            mtime = os.path.getmtime(path)
        except (OSError, ValueError):
            pass
        tpl = sessions.get(code)
        if tpl is None:
            status, detail = "closed", "no session info (skipped)"
        else:
            status, detail = classify(
                hb, mtime, now, tick_timeout, proc_timeout,
                market_open(now_dt, tpl, holidays))
        prev = state.get(name, {})
        prev_status = prev.get("status", "")
        last_ts = float(prev.get("alert_ts", 0.0))
        if should_alert(prev_status, status, last_ts, now, remind_secs):
            if status == "ok":
                text = f"{name} recovered (was {prev_status})"
                key = "heartbeat_recover"
            else:
                text = f"{name} [{code}] {status}: {detail}"
                key = f"heartbeat_{status}"
            lines.append(f"{key} | {text}")
            notify(key, text, cfg_path)
            alerts += 1
            prev = {"status": status, "alert_ts": now}
        else:
            prev = {"status": status,
                    "alert_ts": last_ts if status in BAD_STATES else 0.0}
        state[name] = prev
        if verbose:
            print(f"[watch] {name}: {status} {detail}")

    try:
        os.makedirs(hb_dir, exist_ok=True)
        tmp = f"{state_path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, state_path)
    except OSError:
        pass
    if lines:
        try:
            log_dir = os.path.join(group_dir, "Logs")
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, "alerts.log"), "a",
                      encoding="utf-8") as f:
                for ln in lines:
                    f.write("%s watchdog heartbeat_watch | %s\n"
                            % (time.strftime("%F %T"), ln))
        except OSError:
            pass
    return alerts


def main():
    ap = argparse.ArgumentParser(description="cta_turtle heartbeat watchdog")
    ap.add_argument("--group", default=_PKG,
                    help="组合目录 (default: scripts/ 父级)")
    ap.add_argument("--common",
                    default=os.path.abspath(
                        os.path.join(_PKG, "..", "common")),
                    help="common 目录 (sessions/holidays/commodities)")
    ap.add_argument("--config", default="",
                    help="turtle_config.yaml (default <group>/config/...)")
    ap.add_argument("--timeout", type=float, default=360.0,
                    help="tick 断流阈值 (秒, 交易时段内)")
    ap.add_argument("--proc-timeout", type=float, default=180.0,
                    help="心跳文件陈旧阈值 (秒)")
    ap.add_argument("--remind", type=float, default=1800.0,
                    help="持续 bad 状态重复提醒间隔 (秒)")
    ap.add_argument("--once", action="store_true",
                    help="跑一轮退出 (默认, 供 timer 调用)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    cfg_path = args.config or os.path.join(
        args.group, "config", "turtle_config.yaml")
    try:
        run_watch(args.group, args.common, cfg_path,
                  args.timeout, args.proc_timeout, args.remind,
                  verbose=args.verbose)
    except Exception as e:      # watchdog 自身故障必须"响"
        try:
            from timer_notify import notify
            import traceback
            notify("heartbeat_watch_crash",
                   f"heartbeat_watch.py crashed: {e}\n"
                   f"{traceback.format_exc()[-800:]}", cfg_path)
        except Exception:
            pass
    sys.exit(0)


if __name__ == "__main__":
    main()
