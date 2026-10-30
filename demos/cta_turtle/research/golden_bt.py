#!/usr/bin/env python3
"""黄金回测 (R1, 2026-10-22): 端到端哈希基线防回归

单测覆盖不到"整条回测链路的端到端等价"——core/ 改名、h1 改造这类
"语义应保持零漂移"的重构, 需要一个固定 (config_sha, 数据快照, 窗口)
的基线断言: 任何导致基线漂移的 PR 必须在描述里显式声明预期差异。

基线: rb d1 trigger, 2018-01~2023-04, capital=5M, slippage=0。
对 sha256(signals.csv + funds.csv + trades.csv) 与 summary 关键指标
做精确比对; 与预期不符即非零退出 (除 --update 显式刷新基线)。

用法:
  python3 research/golden_bt.py            # 校验 (漂移 → exit 1)
  python3 research/golden_bt.py --update   # 刷新基线 (口径变更后)
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

BASELINE = os.path.join(ROOT, "golden", "rb_d1_trigger_baseline.json")
OUT_DIR = os.path.join(ROOT, "outputs_bt", "turtle_SHFE_rb_HOT")
HASH_FILES = ("signals.csv", "funds.csv", "trades.csv")
SUMMARY_KEYS = ("total_return", "annual_return", "max_falldown",
                "sharpe_ratio", "days")

GOLDEN = {
    "code": "SHFE.rb.HOT",
    "config": "config/turtle_config.yaml",
    "start": 201801010900,
    "end": 202304031500,
    "capital": 5_000_000,
    "slippage": 0,
}


def sha256_file(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _input_hashes() -> dict:
    """黄金回测输入数据/费率/合约/hots 指纹（基线缺字段时兼容旧档）。"""
    paths = {
        "rb_d1": os.path.join(ROOT, "..", "storage", "csv",
                              "SHFE.rb.HOT_d.csv"),
        "rb_m5": os.path.join(ROOT, "..", "storage", "csv",
                              "SHFE.rb.HOT_m5.csv"),
        "fees": os.path.join(ROOT, "..", "common", "fees.json"),
        "commodities": os.path.join(ROOT, "..", "common", "commodities.json"),
        "hots": os.path.join(ROOT, "..", "common", "hots.json"),
    }
    return {k: sha256_file(v) if os.path.exists(v) else None
            for k, v in paths.items()}


def collect(out_dir: str = OUT_DIR) -> dict:
    """采集当前回测产物指纹 (纯函数, 可单测)"""
    hashes = {}
    for name in HASH_FILES:
        p = os.path.join(out_dir, name)
        hashes[name] = sha256_file(p) if os.path.exists(p) else None
    summary = {}
    sp = os.path.join(out_dir, "summary.json")
    if os.path.exists(sp):
        with open(sp, encoding="utf-8") as f:
            raw = json.load(f)
        summary = {k: raw.get(k) for k in SUMMARY_KEYS}
    return {"hashes": hashes, "summary": summary}


def diff_baseline(base: dict, cur: dict) -> list:
    """基线比对, 返回差异描述列表 (空=一致)"""
    out = []
    for name in HASH_FILES:
        a, b = base["hashes"].get(name), cur["hashes"].get(name)
        if a != b:
            out.append(f"{name}: {str(a)[:12]}.. != {str(b)[:12]}..")
    for k in SUMMARY_KEYS:
        a, b = base["summary"].get(k), cur["summary"].get(k)
        if a != b:
            out.append(f"summary.{k}: {a} != {b}")
    return out


def run_bt() -> int:
    cmd = [sys.executable, os.path.join(ROOT, "runBT.py"),
           "--code", GOLDEN["code"],
           "--config", os.path.join(ROOT, GOLDEN["config"]),
           "--start", str(GOLDEN["start"]), "--end", str(GOLDEN["end"]),
           "--capital", str(GOLDEN["capital"]),
           "--slippage", str(GOLDEN["slippage"])]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[golden] runBT failed rc={r.returncode}\n"
              f"{r.stdout[-800:]}\n{r.stderr[-800:]}", file=sys.stderr)
    return r.returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true",
                    help="口径变更后刷新基线 (需 commit 说明)")
    ap.add_argument("--skip-run", action="store_true",
                    help="不重跑, 直接比对现有 outputs_bt (调试)")
    args = ap.parse_args()

    cfg_path = os.path.join(ROOT, GOLDEN["config"])
    config_sha = sha256_file(cfg_path)

    if not args.skip_run:
        if run_bt() != 0:
            raise SystemExit(2)
    cur = collect()
    cur["spec"] = dict(GOLDEN)
    cur["config_sha256"] = config_sha
    cur["input_hashes"] = _input_hashes()

    if args.update:
        os.makedirs(os.path.dirname(BASELINE), exist_ok=True)
        with open(BASELINE, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
        print(f"[golden] baseline written -> {BASELINE}")
        print(f"[golden] summary: {cur['summary']}")
        return
    if not os.path.exists(BASELINE):
        raise SystemExit("[golden] 基线缺失；仅允许显式 --update 建立，"
                         "不得默认接受当前输出")
    with open(BASELINE, encoding="utf-8") as f:
        base = json.load(f)
    if base.get("config_sha256") != config_sha:
        raise SystemExit(
            "[golden] FAILED: 配置哈希变化 "
            f"({base.get('config_sha256', '')[:12]}.. → "
            f"{config_sha[:12]}..)；仅允许显式 --update 并声明口径变更")
    if base.get("input_hashes") and \
            base["input_hashes"] != cur["input_hashes"]:
        raise SystemExit("[golden] FAILED: 输入数据/费率/合约/hots 指纹变化；"
                         "仅允许显式 --update 并声明数据口径变化")
    diffs = diff_baseline(base, cur)
    if diffs:
        print("[golden] FAILED: 回测链路漂移")
        for d in diffs:
            print(f"  {d}")
        print("若差异属预期 (口径变更), 请 research/golden_bt.py --update "
              "并在 PR 描述中声明", file=sys.stderr)
        raise SystemExit(1)
    print(f"[golden] OK: 端到端一致 {cur['summary']}")


if __name__ == "__main__":
    main()
