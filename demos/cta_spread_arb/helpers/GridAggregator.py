"""
GridAggregator — 汇总 grid point 回测结果为 leaderboard.csv
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional


class GridAggregator:
    SUMMARY_CANDIDATES = (
        "outputs_bt/summary.json",
        "outputs_bt/spread_arb_rbhc/summary.json",
        "outputs_bt/spread_arb/summary.json",
        "summary.json",
    )

    # v10: WT outputs to outputs_bt/spread_arb_<experiment>/summary.json
    # 兜底glob已在_find_summary中覆盖

    def __init__(self, grid_root: str | Path, sort_key: str = "calmar_ratio"):
        self.grid_root = Path(grid_root).resolve()
        self.sort_key = sort_key

    @staticmethod
    def _read_json(path: Path) -> Optional[dict]:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    @staticmethod
    def _count_csv_rows(path: Path) -> Optional[int]:
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8", newline="") as f:
                return max(sum(1 for _ in f) - 1, 0)
        except UnicodeDecodeError:
            with path.open("r", encoding="gbk", newline="") as f:
                return max(sum(1 for _ in f) - 1, 0)
        except Exception:
            return None

    def _find_summary(self, workdir: Path) -> Optional[Path]:
        for rel in self.SUMMARY_CANDIDATES:
            path = workdir / rel
            if path.exists():
                return path
        # v10: WT outputs to outputs_bt/spread_arb_<experiment>/summary.json
        matches = list(workdir.glob("outputs_bt/**/summary.json"))
        return matches[0] if matches else None

    def _find_summary_v10(self, workdir: Path, experiment: str) -> Optional[Path]:
        """v10: 根据experiment名精确定位summary.json"""
        stra_name = f'spread_arb_{experiment}'
        path = workdir / 'outputs_bt' / stra_name / 'summary.json'
        if path.exists():
            return path
        return self._find_summary(workdir)

    def _find_trades_count(self, workdir: Path) -> Optional[int]:
        candidates = [workdir / "outputs_bt" / "trades.csv"]
        candidates.extend(workdir.glob("outputs_bt/**/trades.csv"))
        for path in candidates:
            count = self._count_csv_rows(path)
            if count is not None:
                return count
        return None

    def collect(self) -> List[Dict[str, Any]]:
        manifest_path = self.grid_root / "points_manifest.json"
        manifest = self._read_json(manifest_path)
        if not manifest:
            raise FileNotFoundError(f"missing/invalid points manifest: {manifest_path}")

        rows: List[Dict[str, Any]] = []
        for point in manifest.get("points", []):
            workdir = Path(point["workdir"])
            experiment = point.get("experiment", "")
            # v10: 优先用experiment名精确定位
            summary_path = self._find_summary_v10(workdir, experiment) if experiment else self._find_summary(workdir)
            summary = self._read_json(summary_path) if summary_path else None
            status_path = workdir / "status.json"
            status = self._read_json(status_path) or {}
            params = point.get("params", {}) or {}

            row: Dict[str, Any] = {
                "rank": "",
                "point_id": point.get("point_id"),
                "status": status.get("status", "ok" if summary else "missing_summary"),
                "return_code": status.get("return_code", ""),
                "elapsed_sec": status.get("elapsed_sec", ""),
                "summary_path": str(summary_path) if summary_path else "",
                "total_return": "",
                "annual_return": "",
                "max_falldown": "",
                "calmar_ratio": "",
                "sharpe_ratio": "",
                "sortino_ratio": "",
                "win_rate": "",
                "trades": self._find_trades_count(workdir) or "",
            }
            row.update({f"param_{k}": v for k, v in params.items()})

            if summary:
                for k in (
                    "total_return", "annual_return", "max_falldown", "calmar_ratio",
                    "sharpe_ratio", "sortino_ratio", "win_rate",
                ):
                    row[k] = summary.get(k, "")

            rows.append(row)

        def sort_val(row: Dict[str, Any]) -> float:
            try:
                v = row.get(self.sort_key, "")
                return float(v) if v != "" else float("-inf")
            except Exception:
                return float("-inf")

        rows.sort(key=sort_val, reverse=True)
        for i, row in enumerate(rows, start=1):
            row["rank"] = i
        return rows

    def write_csv(self, rows: List[Dict[str, Any]], out_path: str | Path | None = None) -> Path:
        out = Path(out_path) if out_path else self.grid_root / "leaderboard.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        fieldnames: List[str] = []
        preferred = [
            "rank", "point_id", "status", "return_code", "elapsed_sec",
            "calmar_ratio", "total_return", "annual_return", "max_falldown",
            "sharpe_ratio", "sortino_ratio", "win_rate", "trades",
        ]
        for name in preferred:
            if any(name in r for r in rows):
                fieldnames.append(name)
        param_keys = sorted({k for r in rows for k in r if k.startswith("param_")})
        fieldnames.extend(param_keys)
        for r in rows:
            for k in r:
                if k not in fieldnames:
                    fieldnames.append(k)

        with out.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return out

    def aggregate(self, out_path: str | Path | None = None) -> Path:
        rows = self.collect()
        return self.write_csv(rows, out_path)
