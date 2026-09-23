#!/usr/bin/env python3
"""合约信息每日更新管道 (CTPLoader 包装)

从 CTP 交易前置拉取全市场合约/品种信息, 更新 common/ 下
contracts.json 与 commodities.json:

1. 当日幂等: contracts.json mtime 已是今天则跳过 (--force 强制)
2. 种子合并: 临时目录先放入现有 json (C++ LoadFromJson 先读后写,
   我们手工维护的字段得以保留), mapfiles 提供 session 映射
3. 子进程隔离: C++ DumpToJson 后 exit(0) 会带走宿主进程,
   必须以子进程方式调用 (TraderSpi.cpp:376-378)
4. 后处理: 剔除已过期合约 (实盘坑④僵尸合约非法时间戳tick的根治),
   新品种 session 缺失时回填旧文件值
5. 原子替换 + 日期备份 (.bak.YYYYMMDD)

凭据来源 (优先级: --xxx 参数 > --cred yaml > 环境变量):
  CTP_FRONT / CTP_BROKER / CTP_USER / CTP_PASS / CTP_APPID / CTP_AUTHCODE
  --cred 指向 WT tdtraders.yaml (取 traders[0] 的 front/broker/user/pass),
  与交易通道共用同一份凭据, 免重复维护。
SimNow 7x24 交易前置: tcp://182.254.243.31:30001
注意是**交易**前置 (合约查询走 TraderAPI), 不是行情前置 (30011)。

用法:
  python3 scripts/update_contracts.py --common ../common
  python3 scripts/update_contracts.py --common ../common --force
  WT_CONTRACT_SYNC=1 时 deploy run.py 启动链自动调用本脚本

退出码: 0=成功或已是最新; 2=拉取失败 (保留旧文件); 1=参数错误
"""
import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MAP_FILE = os.path.join(HERE, "ctp_loader", "map_future.ini")
sys.path.insert(0, HERE)
from timer_notify import notify   # noqa: E402  (P1-4 失败告警)

# 子进程内执行的引导代码: ContractLoader 同步拉取,
# C++ 侧 dump 完成后 exit(0), 子进程退出码 0 即成功
_LOADER_BOOTSTRAP = """
import sys
from wtpy import ContractLoader, LoaderType
cfg = sys.argv[1]
loader = ContractLoader(lType=LoaderType.LT_CTP)
loader.start_with_config(__import__('json').loads(cfg), bAsync=False)
"""


# ---------- 纯函数 (可单测) ----------

def is_fresh_today(path: str, today: int) -> bool:
    """文件 mtime 的日期是否已是 today (YYYYMMDD)"""
    if not os.path.exists(path):
        return False
    mtime = datetime.datetime.fromtimestamp(os.path.getmtime(path))
    return int(mtime.strftime("%Y%m%d")) == today


def filter_expired(contracts: dict, today: int) -> tuple:
    """剔除已过期合约 (expiredate 非0且 < today)。

    contracts: {exchg: {code: {..., "expiredate": int}}}
    返回 (过滤后字典, 剔除数)。实盘坑④: 僵尸合约推送非法时间戳
    tick 导致 writer 写越界, 过期合约必须不进入订阅集合。
    """
    out, removed = {}, 0
    for exchg, items in contracts.items():
        kept = {}
        for code, c in items.items():
            exp = c.get("expiredate", 0) or 0
            if exp and int(exp) < today:
                removed += 1
                continue
            kept[code] = c
        out[exchg] = kept
    return out, removed


def fill_missing_sessions(new_comms: dict, old_comms: dict) -> tuple:
    """新品种 session 缺失(mapfiles 未覆盖)时回填旧文件值。

    返回 (补齐后字典, 回填数)。MAP_SESSION 未命中时 C++ 写入空串,
    空 session 会让引擎拿不到交易时段模板。
    """
    fixed = 0
    for exchg, items in new_comms.items():
        for pid, comm in items.items():
            if not comm.get("session"):
                old = old_comms.get(exchg, {}).get(pid, {})
                if old.get("session"):
                    comm["session"] = old["session"]
                    fixed += 1
    return new_comms, fixed


def backup_and_replace(src: str, dst: str, today: int):
    """dst → dst.bak.<today> 备份后, src 原子替换 dst"""
    if os.path.exists(dst):
        shutil.copy2(dst, f"{dst}.bak.{today}")
    shutil.move(src, dst)


def build_loader_config(front: str, broker: str, user: str,
                        password: str, appid: str, authcode: str,
                        save_path: str, module: str = "") -> dict:
    """组装 start_with_config 的配置字典 (isFile=False 分支)"""
    if not module:
        # wtpy 包内自带的 CTP 交易 API 动态库 (无 lib 前缀, getModule 不适用)
        import wtpy
        subdir = "x64" if sys.platform.startswith("win") else "linux"
        module = os.path.join(os.path.dirname(wtpy.__file__), "wrapper",
                              subdir, "traders", "thosttraderapi_se.dll"
                              if subdir == "x64" else
                              "thosttraderapi_se.so")
    return {
        "ctp": {"front": front, "broker": broker, "user": user,
                "pass": password, "appid": appid, "authcode": authcode,
                "module": module},
        "config": {"path": save_path, "mask": 1,  # 1=期货 (不含期权/股票)
                   "mapfiles": MAP_FILE,
                   "commfile": "commodities.json",
                   "contfile": "contracts.json"},
    }


def load_cred_yaml(path: str) -> dict:
    """从 WT tdtraders.yaml 提取凭据 (traders[0])"""
    import yaml
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    t = cfg["traders"][0]
    return {"front": str(t.get("front", "")), "broker": str(t["broker"]),
            "user": str(t["user"]), "password": str(t["pass"])}


# ---------- 主流程 ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--common", type=str,
                    default=os.path.abspath(os.path.join(ROOT, "..",
                                                         "common")),
                    help="basefiles 目录 (contracts/commodities.json 所在)")
    ap.add_argument("--cred", type=str, default="",
                    help="tdtraders.yaml 路径 (优先级低于显式参数)")
    ap.add_argument("--front", default="")
    ap.add_argument("--broker", default="")
    ap.add_argument("--user", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--appid", default=os.environ.get(
        "CTP_APPID", "simnow_client_test"))
    ap.add_argument("--authcode", default=os.environ.get(
        "CTP_AUTHCODE", "0000000000000000"))
    ap.add_argument("--timeout", type=int, default=300,
                    help="子进程拉取超时秒数")
    ap.add_argument("--force", action="store_true",
                    help="忽略当日幂等检查, 强制更新")
    args = ap.parse_args()

    # 凭据合并: 显式参数 > --cred yaml > 环境变量
    if args.cred and os.path.exists(args.cred):
        cred = load_cred_yaml(args.cred)
        for k, v in cred.items():
            if not getattr(args, k):
                setattr(args, k, v)
    for k, env in (("front", "CTP_FRONT"), ("broker", "CTP_BROKER"),
                   ("user", "CTP_USER"), ("password", "CTP_PASS")):
        if not getattr(args, k):
            setattr(args, k, os.environ.get(env, ""))

    today = int(datetime.datetime.now().strftime("%Y%m%d"))
    # 绝对化: C++ loader 以 subprocess cwd 解析相对 SAVEPATH,
    # 相对路径会写入嵌套目录 (实测坑: ./ctp_loader_xx/ctp_loader_xx/)
    args.common = os.path.abspath(args.common)
    comm_path = os.path.join(args.common, "commodities.json")
    cont_path = os.path.join(args.common, "contracts.json")

    if not args.force and is_fresh_today(cont_path, today):
        print(f"[contracts] 今日已更新 ({cont_path}), 跳过 "
              f"(--force 强制)")
        return 0

    for k in ("front", "broker", "user", "password"):
        if not getattr(args, k):
            print(f"[contracts] 缺少凭据: --{k} 或环境变量 "
                  f"CTP_{k.upper()}", file=sys.stderr)
            notify("contracts_fail",
                   f"合约信息更新失败: 缺少凭据 --{k} (检查 tdtraders.yaml"
                   f" 与 CTP_* 环境变量)")
            return 1

    tmpdir = tempfile.mkdtemp(prefix="ctp_loader_",
                              dir=os.path.dirname(args.common) or ".")
    try:
        # 种子: C++ 先 LoadFromJson 再合并 CTP 结果,
        # 手工维护字段 (session 等) 由此保留
        old_comms = {}
        for p in (comm_path, cont_path):
            if os.path.exists(p):
                shutil.copy2(p, tmpdir)
                if p == comm_path:
                    with open(p, encoding="utf-8") as f:
                        old_comms = json.load(f)

        cfg = build_loader_config(args.front, args.broker, args.user,
                                  args.password, args.appid,
                                  args.authcode,
                                  save_path=tmpdir + os.sep)
        print(f"[contracts] querying CTP {args.front} "
              f"broker={args.broker} user={args.user} ...")
        r = subprocess.run(
            [sys.executable, "-c", _LOADER_BOOTSTRAP,
             json.dumps(cfg)],
            cwd=tmpdir, timeout=args.timeout,
            capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[contracts] loader FAILED (rc={r.returncode}):\n"
                  f"{r.stdout[-1500:]}\n{r.stderr[-1500:]}",
                  file=sys.stderr)
            notify("contracts_fail",
                   f"合约信息更新失败 {today}: CTP loader rc={r.returncode} "
                   f"— 沿用旧文件, 新上市/到期合约滞后一天")
            return 2

        new_cont_file = os.path.join(tmpdir, "contracts.json")
        new_comm_file = os.path.join(tmpdir, "commodities.json")
        if not (os.path.exists(new_cont_file)
                and os.path.exists(new_comm_file)):
            print("[contracts] loader 未产出 json 文件", file=sys.stderr)
            notify("contracts_fail",
                   f"合约信息更新失败 {today}: loader 未产出 json")
            return 2
        with open(new_cont_file, encoding="utf-8") as f:
            contracts = json.load(f)
        with open(new_comm_file, encoding="utf-8") as f:
            comms = json.load(f)
        n_cont = sum(len(v) for v in contracts.values())
        if n_cont < 100:
            print(f"[contracts] 合约数异常 ({n_cont}), 拒绝替换; "
                  f"loader 输出:\n{r.stdout[-1200:]}\n{r.stderr[-600:]}",
                  file=sys.stderr)
            notify("contracts_fail",
                   f"合约信息更新失败 {today}: 合约数异常 {n_cont}<100, "
                   f"拒绝替换")
            return 2

        contracts, removed = filter_expired(contracts, today)
        comms, fixed = fill_missing_sessions(comms, old_comms)

        # 写回临时文件再原子替换
        for path, obj in ((new_cont_file, contracts),
                          (new_comm_file, comms)):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=1)
        backup_and_replace(new_cont_file, cont_path, today)
        backup_and_replace(new_comm_file, comm_path, today)

        n_new = sum(len(v) for v in contracts.values())
        n_comm = sum(len(v) for v in comms.values())
        print(f"[contracts] OK: {n_cont} → {n_new} 合约 "
              f"(剔过期 {removed}), {n_comm} 品种 "
              f"(session回填 {fixed}); 备份 .bak.{today}")
        return 0
    except subprocess.TimeoutExpired:
        print(f"[contracts] loader 超时 ({args.timeout}s), 保留旧文件",
              file=sys.stderr)
        notify("contracts_fail",
               f"合约信息更新失败 {today}: CTP loader 超时 "
               f"{args.timeout}s, 保留旧文件")
        return 2
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    import traceback
    try:
        sys.exit(main())
    except Exception:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        try:
            notify("contracts_fail", "合约信息更新脚本崩溃: " + tb[-400:])
        finally:
            sys.exit(1)
