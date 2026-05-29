"""WT loader 读回 gate（P0-C）。

通过 subprocess 调用 `_wt_readback_worker.py`，让真实的 wtpy/WtDtServo
（C++ loader）读回 ks2wtpy 转换出的 CSV/DSB/LMDB，覆盖：

  - HOT 合约 (e.g. SHFE.rb.HOT)        — 期望成功
  - 具体合约 (e.g. SHFE.rb.2510)       — 期望成功
  - _index 反例 (e.g. SHFE.rb.IDX/...)— 期望失败（确认 _index 不会被误判为 HOT）

任何"期望成功但失败"或"期望失败但成功"都计为 fail，并在 details 暴露原因。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .gates import GateResult

logger = logging.getLogger(__name__)


@dataclass
class WtReadbackTask:
    label: str                  # 用例名 (HOT / specific / _index)
    std_code: str
    period: str
    expect: str                 # "pass" 或 "fail"
    storage_mode: str = "dsb"   # csv / lmdb / dsb
                                # 注意：WT C++ WtDtServo 真实数据源走 his/{period}/{exchg}/{code}.dsb，
                                # CSV/LMDB 仅为 ks2wtpy 中间产物或可视化用，setStorage 不消费。
                                # 因此默认必须用 dsb（=WT 标准根，含 his/ 子树）。
    data_count: int = 5


def _worker_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "_wt_readback_worker.py")


def _wtpy_root() -> str:
    """tools/ks2wtpy/acceptance/wt_readback.py → wtpy 根。"""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "..", ".."))


def _scan_storage(output_dir: str) -> Dict[str, Optional[str]]:
    """返回 {csv: dir | None, lmdb: dir | None, dsb: dir | None}

    适配两种布局：
      A) 子目录布局：output_dir/{csv,lmdb,dsb}/
      B) WT 标准布局：output_dir/csv/  +  output_dir/his/{day,min5,...}/EXCHG/
         （此时 dsb 指向 output_dir 根，由 WtDtServo 自行解析 his/）
    """
    found: Dict[str, Optional[str]] = {"csv": None, "lmdb": None, "dsb": None}
    for k in found:
        p = os.path.join(output_dir, k)
        if os.path.isdir(p):
            found[k] = p
    if found["dsb"] is None and os.path.isdir(os.path.join(output_dir, "his")):
        # WtDtServo.setStorage 要求传 output_dir 本身（含 his/ 子树）
        found["dsb"] = output_dir
    return found


def _is_wt_std_code(code: str) -> bool:
    """WT 标准 stdCode = EXCHG.product.contract（至少 3 段，product 段为纯字母）。

    例：
      合规：SHFE.rb.HOT, SHFE.rb.2510, SHFE.rb.2510Q
      不合规：SHFE.rb2510 (老式合并形式) — 会让 WT C++ loader segfault
    """
    segs = code.split(".")
    if len(segs) < 3:
        return False
    _, product, _contract = segs[0], segs[1], ".".join(segs[2:])
    return product.isalpha() and len(product) >= 1


def _load_product_dict(output_dir: str) -> Dict[str, List[str]]:
    """从 common/commodities.json 加载 {EXCHG: [product, ...]} 字典。

    commodities.json 结构 (ks2wtpy 产物)：{EXCHG: {product: {...}}}。
    用于把 DSB 文件名 (IC1505) 反推成三段 stdCode (CFFEX.IC.1505)。
    缺文件或非嵌套结构返回空字典—— 调用方会跳过未匹配的 code。
    """
    path = os.path.join(output_dir, "common", "commodities.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except Exception as e:  # noqa: BLE001
        logger.warning("commodities.json 加载失败: %s", e)
        return {}
    out: Dict[str, List[str]] = {}
    if isinstance(data, dict):
        for exchg, sub in data.items():
            if isinstance(sub, dict):
                # 按长度倒序，保证 "ag" 不抢 "agL" 这种前缀
                out[exchg] = sorted(sub.keys(), key=len, reverse=True)
    return out


def _split_dsb_code(exchg: str, dsb_code: str,
                    product_dict: Dict[str, List[str]]) -> Optional[str]:
    """从 DSB 文件名反推三段 stdCode。

    输入: exchg='CFFEX', dsb_code='IC1505'
    输出: 'CFFEX.IC.1505'  (commodities 含 CFFEX.IC)
          None             (未匹配，调用方跳过)

    说明：
      - DSB 文件名 = C++ CodeHelper.extractStdCode 中 _code 字段：
          非 CZCE: product+contract (rb2510)
          CZCE:    product+contract[1:] (AP610，原 4 位月号去首位)
        所以反向只需在 product_dict[exchg] 里找最长前缀匹配即可。
      - product_dict 倒序保证 "CF" 不抢 "CFI" 这种潜在前缀冲突。
    """
    products = product_dict.get(exchg, [])
    for prod in products:
        if dsb_code.startswith(prod) and len(dsb_code) > len(prod):
            contract = dsb_code[len(prod):]
            # contract 必须非空且首字符是数字（排除 rb_main 这种特殊名）
            if contract and contract[0].isdigit():
                return f"{exchg}.{prod}.{contract}"
    return None


def _autodiscover_codes(output_dir: str) -> Dict[str, List[str]]:
    """从 DSB 目录 + commodities.json 反推三段 stdCode 供 WtDtServo 读回。

    扫描 his/day/{exchg}/*.dsb 物理文件，用 commodities.json 的 product 字典
    把 IC1505 反推成 'CFFEX.IC.1505'，IC.HOT 直接识别为 'CFFEX.IC.HOT'。

    返回 {hot, specific}：
      - hot:        三段 HOT stdCode 列表 (限前 3)
      - specific:   三段具体合约 stdCode 列表 (限前 3)

    （旧实现扫 CSV 文件名 + 把不合规的当 malformed 反例；
      重构后 acceptance 不再依赖 CSV alias，malformed 反例由
      build_default_tasks 的 .IDX 反例独立提供，足够覆盖。）
    """
    hots: List[str] = []
    specifics: List[str] = []

    his_day = os.path.join(output_dir, "his", "day")
    if not os.path.isdir(his_day):
        return {"hot": [], "specific": []}

    product_dict = _load_product_dict(output_dir)

    for exchg in sorted(os.listdir(his_day)):
        ex_dir = os.path.join(his_day, exchg)
        if not os.path.isdir(ex_dir):
            continue
        for f in sorted(os.listdir(ex_dir)):
            if not f.endswith(".dsb"):
                continue
            dsb_code = f[:-4]  # 去 .dsb
            # HOT: dsb_code 形如 "rb.HOT" → stdCode "SHFE.rb.HOT"
            if dsb_code.endswith(".HOT"):
                std = f"{exchg}.{dsb_code}"
                if std not in hots:
                    hots.append(std)
                continue
            # 具体合约: 反推 product → 三段
            std = _split_dsb_code(exchg, dsb_code, product_dict)
            if std and std not in specifics:
                specifics.append(std)

    return {"hot": hots[:3], "specific": specifics[:3]}


def build_default_tasks(output_dir: str) -> List[WtReadbackTask]:
    storages = _scan_storage(output_dir)
    csv_dir = storages.get("csv")
    # 注意：_autodiscover_codes 重构后扫 his/day/{exchg}/*.dsb 反推三段 stdCode，
    # 必须传 WT 标准根 (output_dir)，不是 csv 子目录。
    discovery = _autodiscover_codes(output_dir)
    tasks: List[WtReadbackTask] = []

    for code in discovery["hot"]:
        period = "m5" if (csv_dir and os.path.exists(
            os.path.join(csv_dir, f"{code}_m5.csv"))) else "d"
        tasks.append(WtReadbackTask(
            label=f"HOT/{code}/{period}",
            std_code=code, period=period, expect="pass",
        ))
    for code in discovery["specific"]:
        m5_path = os.path.join(csv_dir, f"{code}_m5.csv") if csv_dir else ""
        period = "m5" if (m5_path and os.path.exists(m5_path)) else "d"
        tasks.append(WtReadbackTask(
            label=f"specific/{code}/{period}",
            std_code=code, period=period, expect="pass",
        ))
    # 反例：_index/.IDX 伪 HOT，期望不被解析（确认 _index 不会被误判为 HOT）
    for code in discovery["hot"][:1]:
        prod = code.replace(".HOT", "")
        tasks.append(WtReadbackTask(
            label=f"counterexample/{prod}.IDX",
            std_code=f"{prod}.IDX", period="d", expect="fail",
        ))
    # 注：原 "malformed" 反例分支已删除（修订方案 A, fix-dup-4）。
    # 该分支依赖 _autodiscover_codes 扫到带点 alias CSV 中"不合规"样本，
    # 但 csv alias 已彻底删除（fix-dup-2），DSB 文件名不带点本身就合规，
    # 无法再产出 malformed 候选。.IDX 反例已足够覆盖"不应被解析为 HOT"。
    return tasks


def _run_one(task: WtReadbackTask, *,
             output_dir: str, basefiles: str, wtpy_dir: str,
             timeout: int) -> Dict[str, Any]:
    storages = _scan_storage(output_dir)
    storage_path = storages.get(task.storage_mode)
    if not storage_path:
        return {"task": task.__dict__, "ok": False,
                "skipped": True,
                "error": f"storage '{task.storage_mode}' not present"}

    payload = {
        "wtpy_dir": wtpy_dir,
        "basefiles": basefiles,
        "storage_path": storage_path,
        "stdCode": task.std_code,
        "period": task.period,
        "data_count": task.data_count,
    }
    cmd = [sys.executable, _worker_path()]
    try:
        proc = subprocess.run(cmd, input=json.dumps(payload),
                              capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"task": task.__dict__, "ok": False,
                "error": f"worker timeout ({timeout}s)"}

    raw = (proc.stdout or "").strip()
    rc = proc.returncode
    if not raw and rc != 0:
        # worker 静默崩（C++ segfault/abort）→ 显式上报
        result = {"ok": False,
                  "error": f"worker exited rc={rc} (likely C++ crash) "
                           "without stdout — stdCode 可能违反 WT 规范",
                  "stderr_tail": (proc.stderr or "")[-800:]}
    else:
        try:
            result = json.loads(raw.splitlines()[-1]) if raw else {"ok": False, "error": "no stdout"}
        except Exception as e:  # noqa: BLE001
            logger.warning("worker JSON 解析失败: %s", e)
            result = {"ok": False,
                      "error": f"bad worker json: {e}",
                      "stdout_tail": raw[-1500:]}
    result["task"] = task.__dict__
    result["worker_rc"] = rc
    if proc.stderr:
        result["stderr_tail"] = proc.stderr[-800:]
    return result


class WtReadbackGate:
    name = "wt_readback"

    def __init__(self, tasks: Optional[List[WtReadbackTask]] = None,
                 timeout: int = 60):
        self.tasks = tasks
        self.timeout = timeout

    def run(self, output_dir: str) -> GateResult:
        basefiles = os.path.join(output_dir, "common")
        if not os.path.isdir(basefiles):
            return GateResult(self.name, "skip",
                              f"common/ 不存在: {basefiles}",
                              {"output_dir": output_dir})
        try:
            from wtpy import WtDtServo  # noqa: F401
            wtpy_dir = _wtpy_root()
        except Exception as e:  # noqa: BLE001
            logger.debug("wtpy 不可用: %s", e)
            return GateResult(self.name, "skip",
                              f"wtpy 不可用: {e}",
                              {"output_dir": output_dir})

        tasks = self.tasks or build_default_tasks(output_dir)
        if not tasks:
            return GateResult(self.name, "skip",
                              "未发现可用 CSV 合约样本",
                              {"output_dir": output_dir})

        results: List[Dict[str, Any]] = []
        verdicts: List[str] = []
        for t in tasks:
            res = _run_one(t, output_dir=output_dir, basefiles=basefiles,
                           wtpy_dir=wtpy_dir, timeout=self.timeout)
            if res.get("skipped"):
                res["verdict"] = "skip"
                verdicts.append("skip"); results.append(res); continue
            ok = bool(res.get("ok"))
            expect_pass = (t.expect == "pass")
            if ok == expect_pass:
                res["verdict"] = "pass"; verdicts.append("pass")
            else:
                res["verdict"] = "fail"; verdicts.append("fail")
            results.append(res)

        details = {
            "output_dir": output_dir,
            "wtpy_dir": wtpy_dir,
            "basefiles": basefiles,
            "tasks_total": len(tasks),
            "results": results,
            "verdicts_summary": {
                "pass": verdicts.count("pass"),
                "fail": verdicts.count("fail"),
                "skip": verdicts.count("skip"),
            },
        }
        if "fail" in verdicts:
            return GateResult(self.name, "fail",
                              f"{verdicts.count('fail')}/{len(verdicts)} WT 读回用例不符预期",
                              details)
        if all(v == "skip" for v in verdicts):
            return GateResult(self.name, "skip",
                              "全部用例 skip", details)
        return GateResult(self.name, "pass",
                          f"全部 {verdicts.count('pass')} 用例符合预期 "
                          f"(skip={verdicts.count('skip')})",
                          details)
