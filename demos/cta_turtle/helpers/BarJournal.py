"""运行时 K 线旁路日志 (P1 数据完整性: 崩溃窗口兜底)

背景: WT 引擎在会话轮转时才将闭合K线转存 storage (DSB); 进程在
会话中途宕机时, 当日已收线的 m5/d1 存在于内存而未落 storage,
重启后 stra_prepare_bars 拉到的历史尾部缺一段。

机制: 策略层每消费一根新K线, 幂等追加写入本地 JSONL 旁路日志
(与 storage 写路径完全解耦, 不影响主链路)。重启后由
scripts/prepare_data.py --repair-journal 将 journal 尾部合并回
storage/csv (再按需经 convert_csv_to_dsb.py 重建 DSB)。

文件布局:
    {journal_dir}/{CODE}_{suffix}.jsonl    suffix ∈ d/m5/m1
    每行: {"date":20230103,"time":1500,"open":..,"high":..,
           "low":..,"close":..,"volume":..,"open_interest":..}
"""
import json
import os

_SUFFIX = {"d1": "d", "d": "d", "m5": "m5", "m1": "m1"}


def _safe(code: str) -> str:
    return code.replace(".", "_")


class BarJournal:
    def __init__(self, journal_dir: str):
        self.dir = journal_dir
        os.makedirs(journal_dir, exist_ok=True)

    def path(self, code: str, period: str) -> str:
        suffix = _SUFFIX.get(period, period)
        return os.path.join(self.dir, f"{_safe(code)}_{suffix}.jsonl")

    # ------------------------------------------------------------------
    # 写入 (append-only, 单行 JSON)
    # ------------------------------------------------------------------
    def append(self, code: str, period: str, row: dict):
        """追加一根K线; 必要字段缺失/非法时静默丢弃 (旁路不阻断交易)"""
        try:
            rec = {
                "date": int(row["date"]), "time": int(row.get("time", 0)),
                "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]), "close": float(row["close"]),
                "volume": float(row.get("volume") or 0),
                "open_interest": float(row.get("open_interest") or 0),
            }
        except (KeyError, TypeError, ValueError):
            return False
        line = json.dumps(rec, separators=(",", ":"))
        try:
            with open(self.path(code, period), "a", encoding="utf-8") as f:
                f.write(line + "\n")
            return True
        except OSError:
            return False

    # ------------------------------------------------------------------
    # 读取 / 回补
    # ------------------------------------------------------------------
    def read(self, code: str, period: str) -> list:
        """全量读取 (损坏行跳过); 按 (date,time) 升序去重。
        time 为框架口径整数 (日线=收盘秒或0, 分钟线=HHMMSS)。"""
        path = self.path(code, period)
        if not os.path.exists(path):
            return []
        seen = {}
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                        key = (int(r["date"]), int(r.get("time", 0)))
                        seen[key] = r
                    except (json.JSONDecodeError, KeyError, TypeError,
                            ValueError):
                        continue   # 半行(崩溃瞬间截断)容错
        except OSError:
            return []
        return [seen[k] for k in sorted(seen)]

    # ---- storage csv 版式 ----
    # 日线:  date,open,high,low,close,volume,turnover,open_interest
    # 分钟:  date,time,open,...  (time 形如 09:05:00)
    _HDR_D = ("date,open,high,low,close,volume,turnover,"
              "open_interest")
    _HDR_M = ("date,time,open,high,low,close,volume,turnover,"
              "open_interest")

    @staticmethod
    def _fmt_time(t: int) -> str:
        t = int(t)
        hh, rem = divmod(t, 10000)
        mm, ss = divmod(rem, 100)
        if hh == 0 and mm == 0 and ss == 0:
            return ""
        return f"{hh:02d}:{mm:02d}:{ss:02d}"

    @staticmethod
    def _plausible_hhmmss(v: int) -> bool:
        if v < 10000 or v >= 240000:   # CN期货无 00:xx 时段; 排除价格小整数
            return False
        return (v % 100) < 60 and (v // 100 % 100) < 60

    @classmethod
    def _parse_time(cls, s: str) -> int:
        """'09:05:00' → 90500 (HHMMSS); 非时间列 → 0"""
        s = s.strip()
        if ":" in s:
            parts = s.split(":")
            try:
                hh, mm, ss = (int(parts[0]), int(parts[1]),
                              int(parts[2]) if len(parts) > 2 else 0)
                v = hh * 10000 + mm * 100 + ss
                return v if cls._plausible_hhmmss(v) else 0
            except ValueError:
                return 0
        if s.isdigit():
            v = int(s)
            return v if cls._plausible_hhmmss(v) else 0
        return 0

    @staticmethod
    def _parse_date(s: str):
        d = s.strip().replace("/", "").replace("-", "")
        return int(d[:8]) if d[:8].isdigit() else None

    @staticmethod
    def _date_style(first_data_line: str) -> bool:
        """True → 目标文件日期用 'YYYY/MM/DD' 斜杠风格"""
        head = first_data_line.strip().split(",")[0]
        return "/" in head

    @staticmethod
    def _fmt_date(d: int, slashed: bool) -> str:
        if not slashed:
            return str(d)
        return f"{d // 10000:04d}/{d // 100 % 100:02d}/{d % 100:02d}"

    def merge_to_csv(self, code: str, period: str, csv_path: str) -> int:
        """journal → storage csv 增量回补 (幂等可重复)。

        键口径: 日线版式 (date,0); 分钟版式 (date,time_HHMMSS)。
        只追加键值严格大于 csv 现有最大键的行; 新文件用无斜杠日期+
        含 time 表头 (分钟) / 标准8列 (日线), 与既有行风格保持一致。
        """
        return self.merge_rows(self.read(code, period), period, csv_path)

    def merge_rows(self, rows: list, period: str, csv_path: str) -> int:
        """任意来源行 → storage csv 增量合并 (幂等可重复)。

        rows: [{"date":int,"time":HHMMSS,"open",...,"close","volume"?,
               "open_interest"?}]; 键口径与 merge_to_csv 相同。
        供外部数据适配器 (TqSdk 等) 直传内存行复用同一合并逻辑。
        """
        suffix = _SUFFIX.get(period, period)
        if not rows and os.path.exists(csv_path):
            return 0

        has_time_col = None     # None=新文件
        slashed = False
        existing_keys = set()
        max_key = None
        if os.path.exists(csv_path):
            try:
                with open(csv_path, encoding="utf-8") as f:
                    for i, line in enumerate(f):
                        s = line.rstrip("\n").lstrip("﻿").strip()
                        if not s:
                            continue
                        parts = [p.strip() for p in s.split(",")]
                        if i == 0 and not parts[0][:4].isdigit():
                            has_time_col = "time" in [
                                c.lower() for c in parts]
                            continue
                        if len(parts) < 5:
                            continue
                        d = self._parse_date(parts[0])
                        if d is None:
                            continue
                        if has_time_col is None:   # 无表头文件
                            has_time_col = self._parse_time(
                                parts[1]) > 0
                        t = self._parse_time(parts[1]) \
                            if has_time_col else 0
                        k = (d, t)
                        existing_keys.add(k)
                        if max_key is None or k > max_key:
                            max_key = k
                        if not slashed:
                            slashed = "/" in parts[0]
            except OSError:
                has_time_col = None

        if has_time_col is None:               # 全新文件
            has_time_col = suffix != "d"
        want_daily_key = (not has_time_col)

        def key(r):
            return (int(r["date"]),
                    0 if want_daily_key else int(r.get("time", 0)))

        todo = [r for r in rows if key(r) not in existing_keys
                and (max_key is None or key(r) > max_key)]
        if not todo:
            return 0
        todo.sort(key=key)
        header = self._HDR_M if has_time_col else self._HDR_D
        need_header = not os.path.exists(csv_path) or \
            os.path.getsize(csv_path) == 0
        with open(csv_path, "a", encoding="utf-8") as f:
            if need_header:
                f.write(header + "\n")
            for r in todo:
                dt = self._fmt_date(int(r["date"]), slashed)
                cells = [dt]
                if has_time_col:
                    cells.append(self._fmt_time(int(r.get("time", 0))))
                cells += [str(r["open"]), str(r["high"]), str(r["low"]),
                          str(r["close"]), str(r.get("volume", 0)),
                          "0", str(r.get("open_interest", 0))]
                f.write(",".join(cells) + "\n")
        return len(todo)
