"""Worker: 在独立 subprocess 内调 wtpy.WtDtServo 读回 K 线，结果回 stdout。

输入 (stdin, JSON):
  {
    "wtpy_dir": "/mnt/d/.../wtpy",   # 注入 sys.path
    "basefiles": "/abs/common/",
    "storage_path": "/abs/storage/", # 与 WtBtEngine.configBTStorage path 等价
    "stdCode": "SHFE.rb.HOT",
    "period": "m5",
    "data_count": 5
  }

输出 (stdout, JSON):
  {
    "ok": true/false,
    "rows": int,
    "first_bar": {...} | null,
    "last_bar": {...} | null,
    "error": "..."  (若 ok=false)
  }

设计：每次 subprocess 处理 *一个* 任务后退出，避免 WtDtServo singleton 跨任务污染。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import traceback

logger = logging.getLogger(__name__)


def _to_jsonable(rec) -> dict:
    """numpy record → plain dict，时间字段保留为 int。"""
    out = {}
    for name in rec.dtype.names:
        v = rec[name]
        try:
            v = v.item() if hasattr(v, "item") else v
        except Exception as e:  # noqa: BLE001
            logger.debug("numpy scalar .item() fallback: %s", e)
        out[name] = v
    return out


def main() -> int:
    try:
        raw = sys.stdin.read()
        task = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        logger.warning("输入解析失败: %s", e)
        print(json.dumps({"ok": False, "error": f"bad input: {e}"}))
        return 1

    wtpy_dir = task["wtpy_dir"]
    if wtpy_dir and wtpy_dir not in sys.path:
        sys.path.insert(0, wtpy_dir)

    basefiles = task["basefiles"]
    storage_path = task["storage_path"]
    std_code = task["stdCode"]
    period = task.get("period", "m5")
    data_count = int(task.get("data_count", 5))

    # 在 storage_path 上下文里调用，避免相对路径漂移
    if os.path.isdir(storage_path):
        os.chdir(os.path.dirname(storage_path.rstrip("/")) or "/")

    try:
        from wtpy import WtDtServo  # type: ignore
    except Exception as e:  # noqa: BLE001
        logger.warning("import WtDtServo 失败: %s", e)
        print(json.dumps({"ok": False,
                          "error": f"import WtDtServo fail: {e}",
                          "trace": traceback.format_exc()}))
        return 1

    try:
        dt = WtDtServo()
        dt.setBasefiles(basefiles)
        dt.setStorage(storage_path, adjfactor="adjfactors.json")
        bars = dt.get_bars(stdCode=std_code, period=period, dataCount=data_count)
    except Exception as e:  # noqa: BLE001
        logger.warning("get_bars 异常: %s", e)
        print(json.dumps({"ok": False,
                          "error": f"get_bars raise: {e}",
                          "trace": traceback.format_exc()[-1500:]}))
        return 0  # 故意 0：异常作为正常错误结果上报

    # WtNpKline 不暴露 size；判空必须看 ndarray.shape[0]
    if bars is None:
        print(json.dumps({"ok": False,
                          "error": "WtDtServo returned None bars",
                          "rows": 0}))
        return 0
    try:
        _shape0 = bars.ndarray.shape[0] if hasattr(bars, "ndarray") else 0
    except Exception as e:  # noqa: BLE001
        logger.debug("bars.ndarray.shape 读取异常: %s", e)
        _shape0 = 0
    if _shape0 == 0:
        print(json.dumps({"ok": False,
                          "error": "WtDtServo returned empty bars (ndarray.shape[0]=0)",
                          "rows": 0}))
        return 0

    try:
        arr = bars.ndarray
        rows = int(arr.shape[0])
        first = _to_jsonable(arr[0])
        last = _to_jsonable(arr[-1])
    except Exception as e:  # noqa: BLE001
        logger.warning("bars 解码失败: %s", e)
        print(json.dumps({"ok": False,
                          "error": f"decode bars fail: {e}",
                          "trace": traceback.format_exc()[-1500:]}))
        return 0

    print(json.dumps({"ok": True, "rows": rows,
                      "first_bar": first, "last_bar": last}))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
