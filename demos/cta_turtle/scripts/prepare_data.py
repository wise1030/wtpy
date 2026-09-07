#!/usr/bin/env python3
"""数据一键预检管道 (P1-4 + 数据完整性运维)

对 turtle_config.yaml 的全部品种执行:
1. hots.json 键检查 (--fix-hots 自动补空键, 空列表=不换月)
2. 信号周期 (d1/h1) 与盘中周期 (intrabar_stop 时) K 线存在性/健康/深度检查
3. 输出健康报告; 存在 error 时退出码 1 (--gate 语义, 可作启动门禁)
4. --repair-journal: 将运行时旁路日志 (helpers/BarJournal) 尾部
   增量合并回 storage/csv, 兜底宕机丢失的会话内K线

数据获取不在本脚本范围 (需外部数据源凭据), 缺失时会给出
WtHotPicker/datahelper 或 CSV 转换的操作提示。

用法:
  python3 scripts/prepare_data.py                       # 只读检查
  python3 scripts/prepare_data.py --fix-hots            # 自动补 hots 键
  python3 scripts/prepare_data.py --repair-journal ./journals
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from helpers.ConfigLoader import ConfigLoader          # noqa: E402
from helpers import DataChecker                        # noqa: E402


def repair_journals(cfg, journal_dir: str, storage_dir: str) -> int:
    """全部品种 × 相关周期执行 journal→csv 回补, 返回追加总行数"""
    from helpers.BarJournal import BarJournal
    bj = BarJournal(journal_dir)
    total = 0
    for inst in cfg.instruments:
        periods = {inst.period}
        if inst.intrabar_stop:
            periods.add(getattr(cfg.base, "intrabar_period", "m5"))
        for period in sorted(periods):
            suffix = {"d1": "d", "m5": "m5", "m1": "m1"}.get(period,
                                                             period)
            csv_path = os.path.join(storage_dir, "csv",
                                    f"{inst.code}_{suffix}.csv")
            n = bj.merge_to_csv(inst.code, period, csv_path)
            if n:
                print(f"[repair] {inst.code} {period}: +{n} bars -> "
                      f"{csv_path}")
            else:
                print(f"[repair] {inst.code} {period}: 无新增 (已是最新)")
            total += n
    return total


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
    ap.add_argument("--fix-hots", action="store_true",
                    help="hots.json 缺键时自动补空列表 (不换月)")
    ap.add_argument("--repair-journal", type=str, default="",
                    help="旁路日志目录; 指定后将 journal 尾部增量"
                         "合并回 storage/csv (幂等可重复)")
    args = ap.parse_args()

    cfg = ConfigLoader.load(args.config)
    print(f"[prepare] config={args.config}")
    print(f"[prepare] common={args.common} storage={args.storage}")

    if args.fix_hots:
        for inst in cfg.instruments:
            if DataChecker.fix_hots(inst.code, args.common):
                print(f"[prepare] hots.json 已补空键: {inst.code}")

    if args.repair_journal:
        try:
            n = repair_journals(cfg, args.repair_journal, args.storage)
            print(f"[repair] done: +{n} bars total")
        except Exception as e:
            print(f"[repair] FAILED: {e}")
            sys.exit(2)

    issues = DataChecker.check_config(cfg, args.common, args.storage)
    n_err = sum(1 for i in issues if i.level == "error")
    n_warn = sum(1 for i in issues if i.level == "warn")
    for i in issues:
        print(f"  {i}")
    print(f"[prepare] done: {len(cfg.instruments)} instruments, "
          f"{n_err} errors, {n_warn} warnings")
    sys.exit(1 if n_err else 0)


if __name__ == "__main__":
    main()
