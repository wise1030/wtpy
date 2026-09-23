#!/usr/bin/env python3
"""HOT 日线每日续接 (2026-09-16 实盘故障修复)

背景 (实盘实测): 策略代码是主力连续 `XXX.HOT`, 读的是
`storage/his/day/<EXCHG>/<EXCHG>.<product>_HOT.dsb` 与
`storage/csv/<code>_d.csv`; 而数据底盘 (WtDt) 每日只写**真实合约**日线
(`his/day/<EXCHG>/<contract>.dsb`, 16:00 会话轮转后落当日 bar)。主力连续
文件在实盘没有日更管道 -> 自 2026-09-01 起冻结, 策略的 N(ATR20) 与 20/55 日
通道一直用陈旧窗口 (硬证据: INE.sc 的 armed exit=581.8 恰为 HOT 文件
2026-08-28 的最低价, 干净的最新 10 日窗口最低是 665.4)。

本脚本每日把"当前主力合约"的日线增量续接进主力连续文件:
  1) 按 common/hots.json 当前映射定位各品种主力合约 (roll 时自动切换);
  2) 读该合约 his/day DSB, **按日期去重** (数据底盘重启会重复追加重放:
     实测 l2701/sc2610 出现同一天两次, 直接进窗口会双计);
  3) BarJournal.merge_rows 增量并入 storage/csv/<code>_d.csv (幂等, 只追加
     键值大于现有最大的行);
  4) 重建 HOT 日线 DSB (复用 scripts/convert_csv_to_dsb 的转换路径);
  5) 打印每品种增量条数/末日/换月情况; 任何异常只告警不抛出 (旁路不阻断交易)。

用法:
  python3 scripts/update_hot_daily.py --config config/turtle_config.yaml \
      --common ../common --storage ../storage [--codes a,b] [--dry-run]

建议每日 20:40 由 systemd timer 调用 (晚于 20:35 的 hots 更新、早于 21:00 夜盘)。
"""
import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from helpers.BarJournal import BarJournal          # noqa: E402
from timer_notify import notify                    # noqa: E402  (P1-4)


# ----------------------------------------------------------------------
# 纯函数 (可单测)
# ----------------------------------------------------------------------
def parse_contracts(hots: dict, codes: list) -> dict:
    """hots.json + 代码列表 -> {code: 当前主力合约}

    hots 规则形如 {"SHFE": {"rb": [{"date":..., "to": "rb2701"}, ...]}}:
    取**最后一条**规则的 "to" 作为当前主力; 键缺失/规则为空返回 None
    (调用方跳过并告警 - 空规则在实盘会导致 segfault, 见
    DataChecker.check_hots(live=True))。
    """
    out = {}
    for code in codes:
        parts = code.split(".")
        if len(parts) < 2:
            out[code] = None
            continue
        rules = (hots.get(parts[0]) or {}).get(parts[1])
        if not isinstance(rules, list) or not rules:
            out[code] = None
            continue
        last = rules[-1]
        out[code] = str(last.get("to") or "").strip() or None
    return out


def dedup_by_date(bars: list) -> list:
    """[{date,...}] -> 按日期去重且升序 (保留最后一条)"""
    seen = {}
    for b in bars:
        try:
            d = int(b["date"])
        except (KeyError, TypeError, ValueError):
            continue
        if d <= 0:
            continue
        seen[d] = b          # 同一天重复出现时保留后者 (最新写入)
    return [seen[k] for k in sorted(seen)]


def bars_to_rows(bars: list) -> list:
    """真值 DSB bar -> BarJournal 行 (字段口径与 storage csv 一致)"""
    rows = []
    for b in bars:
        rows.append({
            "date": int(b["date"]), "time": 1500,
            "open": float(b["open"]), "high": float(b["high"]),
            "low": float(b["low"]), "close": float(b["close"]),
            "volume": float(b.get("volume") or 0),
            "turnover": float(b.get("turnover") or 0),
            "open_interest": float(b.get("open_interest") or 0)})
    return rows


# ----------------------------------------------------------------------
# IO 适配 (wtpy 不可用时优雅降级)
# ----------------------------------------------------------------------
def _read_dsb_bars(path: str) -> list:
    """读 DSB -> [{date,open,high,low,close,volume,turnover,open_interest}]"""
    from wtpy.wrapper.WtDtHelper import WtDataHelper
    helper = WtDataHelper()
    bars = helper.read_dsb_bars(path)
    if bars is None or len(bars) == 0:
        return []
    nd = bars.ndarray
    names = set(getattr(nd.dtype, "names", None) or ())
    out = []
    for i in range(len(nd["date"])):
        row = {"date": int(nd["date"][i]), "open": float(nd["open"][i]),
               "high": float(nd["high"][i]), "low": float(nd["low"][i]),
               "close": float(nd["close"][i])}
        for src, dst in (("volume", "volume"), ("vol", "volume"),
                         ("turnover", "turnover"), ("money", "turnover"),
                         ("open_interest", "open_interest"),
                         ("hold", "open_interest")):
            if src in names and dst not in row:
                try:
                    row[dst] = float(nd[src][i])
                except (TypeError, ValueError, IndexError):
                    pass
        out.append(row)
    return out


def _rebuild_hot_dsb(csv_dir: str, storage_dir: str) -> int:
    """由 storage/csv/*_d.csv 重建 HOT 日线 DSB (复用既有转换脚本)"""
    sys.path.insert(0, HERE)
    import convert_csv_to_dsb as conv
    return conv.convert(csv_dir, storage_dir, ["d"])


def _contract_path(storage_dir: str, code: str, contract: str) -> str:
    exchg = code.split(".")[0]
    return os.path.join(storage_dir, "his", "day", exchg,
                        "%s.dsb" % contract)


def _csv_last_date(path: str):
    """storage csv 最后一行的日期 (int); 不存在 -> None"""
    if not os.path.exists(path):
        return None
    last = None
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split(",")
                if not parts:
                    continue
                d = parts[0].replace("/", "").replace("-", "")
                if d.isdigit() and len(d) >= 8:
                    last = int(d[:8])
    except OSError:
        return None
    return last


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def update(codes: list, common_dir: str, storage_dir: str,
           read_bars=_read_dsb_bars, rebuild=_rebuild_hot_dsb,
           dry_run: bool = False) -> dict:
    """为 codes 续接 HOT 日线; 返回 {code: 增量条数}。异常不抛出。"""
    hots_path = os.path.join(common_dir, "hots.json")
    try:
        with open(hots_path, encoding="utf-8") as f:
            hots = json.load(f)
    except (OSError, ValueError) as e:
        print(f"[hot_daily] hots.json 读取失败: {e}")
        return {}
    mapping = parse_contracts(hots, codes)
    csv_dir = os.path.join(storage_dir, "csv")
    bj = BarJournal(os.path.join(storage_dir, "_hot_daily_journal"))
    added = {}
    rolls = []
    for code, contract in mapping.items():
        if not contract:
            print(f"[hot_daily] {code}: hots 无规则/无当前主力 -> 跳过")
            added[code] = 0
            continue
        path = _contract_path(storage_dir, code, contract)
        if not os.path.exists(path):
            print(f"[hot_daily] {code}: 主力合约数据缺失 {path} -> 跳过")
            added[code] = 0
            continue
        try:
            bars = dedup_by_date(read_bars(path))
        except Exception as e:          # 数据侧异常不得影响交易链路
            print(f"[hot_daily] {code}: 读取/解析失败 {e} -> 跳过")
            added[code] = 0
            continue
        if not bars:
            added[code] = 0
            continue
        csv_path = os.path.join(csv_dir, f"{code}_d.csv")
        prev_last = _csv_last_date(csv_path)
        rows = bars_to_rows(bars)
        if prev_last and rows and rows[0]["date"] > prev_last:
            rolls.append((code, contract, prev_last, rows[0]["date"]))
        if dry_run:
            n = sum(1 for r in rows if prev_last is None
                    or r["date"] > (prev_last or 0))
        else:
            n = bj.merge_rows(rows, "d", csv_path)
        added[code] = n
        print(f"[hot_daily] {code}: 主力={contract} 读入 {len(bars)} 根, "
              f"增量 {n} 根 (csv 原末日 {prev_last} -> 现末日 "
              f"{bars[-1]['date']})")
    total = sum(added.values())
    # DSB 重建与"本次是否追加"无关: csv 可能已被别的路径写新而 DSB 落后
    # (2026-09-16 实测: 先跑了 csv 合并、DSB 因缺脚本失败, 再跑就 0 增量
    # → 若以增量为条件会永远不重建), 故每次都对 csv 做一次全量重建。
    if not dry_run:
        try:
            cnt = rebuild(csv_dir, storage_dir)
            print(f"[hot_daily] HOT 日线 DSB 重建: {cnt} 个文件")
        except Exception as e:
            # P1-4: 本脚本的静默失败正是 2026-09-01→16 HOT 日线冻结
            # 16 天的直接原因 — DSB 重建失败必须"响"
            print(f"[hot_daily] DSB 重建失败(非致命, 策略会重读 csv): {e}")
            fails = [c for c, n in added.items() if n == 0]
            notify("hotdaily_fail",
                   f"HOT 日线续接异常: DSB 重建失败 ({e}); "
                   f"零增量品种 {fails or '无'} — 策略可能用陈旧 "
                   f"N/通道评估, 请检查 csv 末日")
    for code, contract, pd, nd in rolls:
        print(f"[hot_daily] WARN 换月: {code} -> {contract} "
              f"(csv 末日 {pd}, 新合约首日 {nd})")
    print(f"[hot_daily] 合计增量 {total} 根, 覆盖 {len(codes)} 个品种"
          + (" (dry-run)" if dry_run else ""))
    # P0 (2026-09-22): 重建后必须重启交易进程 — C++ WtDataReader 按
    # (code, period) 缓存 his 块, 进程不重启永远看不到新 DSB。实测后果:
    # ①周五 20:40 重建后, 周五夜盘 catchup 读到的 his 末根仍是周四
    # (静默跳过) → 商品通道缺一根 bar; ②进程长期运行时指标窗口逐步
    # 陈旧 (N 漂移); ③每日重启顺带重置执行单元 (已知卡死 bug 的日线
    # 级自愈) + 重登 CTP (重活 rtdata 账户查询链 — 真值源唯一的
    # 复活手段)。仅在有增量时重启 (周末空跑不打扰); 环境变量
    # WT_HOTDAILY_RESTART=0 可关闭。WatchDog span=10s 自动拉起。
    if not dry_run and total > 0 and \
            os.environ.get("WT_HOTDAILY_RESTART", "1") != "0":
        _restart_runner()
    return added


def _restart_runner():
    """kill 交易进程 (run.py), 由 WatchDog 拉起 — 读到新重建的 DSB。

    时点 20:40 在收盘评估 (15:15) 之后、夜盘 (21:00) 之前, 策略状态
    已随快照持久化, 重启零信息损失 (与 update_hots 的重启同款模式)。"""
    import signal
    import subprocess
    killed = []
    for pat in ("venv/bin/python run.py",):
        try:
            out = subprocess.run(["pgrep", "-f", pat],
                                 capture_output=True, text=True,
                                 timeout=10).stdout.split()
            for pid in out:
                os.kill(int(pid), signal.SIGTERM)
                killed.append(f"{pat}(pid={pid})")
        except Exception as e:
            print(f"[hot_daily] restart {pat} failed: {e}", file=sys.stderr)
    if killed:
        print(f"[hot_daily] 已重启交易进程: {', '.join(killed)} "
              f"(WatchDog 自动拉起, 读取新 DSB + 重登 CTP 刷新真值链)")
        notify("hotdaily_restart",
               f"HOT 日线已续接并重启交易进程 (读新 DSB/重置执行单元/"
               f"重登 CTP): {', '.join(killed)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=str,
                    default=os.path.join(ROOT, "config",
                                         "turtle_config.yaml"))
    ap.add_argument("--common", type=str,
                    default=os.path.abspath(os.path.join(ROOT, "..",
                                                         "common")))
    ap.add_argument("--storage", type=str,
                    default=os.path.abspath(os.path.join(ROOT, "..",
                                                         "storage")))
    ap.add_argument("--codes", type=str, default="",
                    help="逗号分隔的子集, 默认配置内全部品种")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from helpers.ConfigLoader import ConfigLoader
    cfg = ConfigLoader.load(args.config)
    codes = [i.code for i in cfg.instruments]
    if args.codes:
        want = {c.strip() for c in args.codes.split(",") if c.strip()}
        codes = [c for c in codes if c in want]
    print(f"[hot_daily] {datetime.datetime.now():%F %T} "
          f"common={args.common} storage={args.storage} codes={len(codes)}")
    update(codes, args.common, args.storage, dry_run=args.dry_run)


if __name__ == "__main__":
    import traceback
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        try:
            notify("hotdaily_fail", "HOT 日线续接脚本崩溃: " + tb[-400:])
        finally:
            sys.exit(1)
