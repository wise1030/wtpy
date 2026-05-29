"""
投研服务化 only-read data service / notebook adapter

只读契约：本模块绝不写任何文件。所有方法都是读取已有派生视图。

用法（Jupyter / 研究脚本）：
    from research.adapter import open_research_view

    view = open_research_view("/path/to/output/research")
    print(view.list_datasets())

    bundle = view.load("SHFE.rb.HOT", "day")
    print(bundle["rows"][0])            # dict
    print(bundle["lineage"]["format_used"])  # "csv" or "parquet"

    # 一步到位拿 DataFrame
    df = view.load("SHFE.rb.HOT", "day", as_dataframe=True)

    # 溯源
    info = view.describe("SHFE.rb.HOT", "day")
    print(info["source_csv"])
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["open_research_view", "ResearchView"]

_METADATA_FILENAME = "research_view_metadata.json"


@dataclass
class _DatasetEntry:
    std_code: str
    period: str
    source_csv: str
    derived_csv: str
    rows: int
    parquet_status: str
    derived_parquet: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "_DatasetEntry":
        return cls(
            std_code=d["std_code"],
            period=d["period"],
            source_csv=d.get("source_csv", ""),
            derived_csv=d.get("derived_csv", ""),
            rows=d.get("rows", 0),
            parquet_status=d.get("parquet_status", "skipped"),
            derived_parquet=d.get("derived_parquet"),
        )


@dataclass
class ResearchView:
    """只读投研派生视图句柄。"""

    metadata_path: str
    metadata: Dict[str, Any] = field(repr=False)
    _datasets: List[_DatasetEntry] = field(repr=False, default_factory=list)

    def __post_init__(self):
        for d in self.metadata.get("datasets", []):
            self._datasets.append(_DatasetEntry.from_dict(d))

    # ---------- public properties ----------

    @property
    def scenario(self) -> str:
        return self.metadata.get("scenario", "")

    @property
    def derived_view_only(self) -> bool:
        return self.metadata.get("derived_view_only", True)

    @property
    def authority_source(self) -> str:
        return self.metadata.get("authority_source", "")

    @property
    def lineage(self) -> Dict[str, Any]:
        """顶层 lineage（authority_source / inputs / parquet status）。"""
        return {
            "authority_source": self.authority_source,
            "derived_view_only": self.derived_view_only,
            "inputs": self.metadata.get("inputs", {}),
            "parquet": self.metadata.get("parquet", {}),
        }

    # ---------- public methods ----------

    def list_datasets(self) -> List[Tuple[str, str, str]]:
        """返回 [(std_code, period, best_fmt)] 元组列表。

        best_fmt: 'parquet' 若 derived_parquet 存在且文件存在，否则 'csv'。
        """
        result = []
        for ds in self._datasets:
            fmt = "csv"
            if ds.derived_parquet and os.path.isfile(ds.derived_parquet):
                fmt = "parquet"
            result.append((ds.std_code, ds.period, fmt))
        return result

    def load(self, std_code: str, period: str, *,
             fmt: str = "auto",
             as_dataframe: bool = False) -> Any:
        """加载指定数据集。

        Parameters
        ----------
        std_code, period : str
            目标数据集标识。
        fmt : str
            'auto' — 优先 parquet，无则 csv；
            'csv' — 强制 csv；
            'parquet' — 强制 parquet，无则 raise。
        as_dataframe : bool
            True 则返回 pandas.DataFrame（pandas 软依赖，不可用时 raise）。

        Returns
        -------
        dict 或 pandas.DataFrame
            dict keys: rows (List[Dict]), columns (List[str]), lineage (Dict)。
        """
        ds = self._find_dataset(std_code, period)

        # 确定实际格式
        has_parquet = ds.derived_parquet and os.path.isfile(ds.derived_parquet)
        if fmt == "auto":
            actual_fmt = "parquet" if has_parquet else "csv"
        elif fmt == "parquet":
            if not has_parquet:
                raise FileNotFoundError(
                    f"parquet not available for {std_code}/{period}; "
                    f"use fmt='csv' or 'auto'"
                )
            actual_fmt = "parquet"
        elif fmt == "csv":
            actual_fmt = "csv"
        else:
            raise ValueError(f"unknown fmt={fmt!r}; expected 'auto'/'csv'/'parquet'")

        if actual_fmt == "parquet":
            assert ds.derived_parquet is not None  # validated by has_parquet check above
            rows, columns = self._load_parquet(ds.derived_parquet)
        else:
            rows, columns = self._load_csv(ds.derived_csv)

        lineage = {
            "std_code": ds.std_code,
            "period": ds.period,
            "source_csv": ds.source_csv,
            "derived_csv": ds.derived_csv,
            "derived_parquet": ds.derived_parquet,
            "parquet_status": ds.parquet_status,
            "rows_in_metadata": ds.rows,
            "format_used": actual_fmt,
        }

        if as_dataframe:
            return self._rows_to_dataframe(rows, columns)

        return {"rows": rows, "columns": columns, "lineage": lineage}

    def describe(self, std_code: str, period: str) -> Dict[str, Any]:
        """返回单个数据集的 lineage 元数据（不读取数据行）。"""
        ds = self._find_dataset(std_code, period)
        has_parquet = ds.derived_parquet and os.path.isfile(ds.derived_parquet)
        return {
            "std_code": ds.std_code,
            "period": ds.period,
            "source_csv": ds.source_csv,
            "derived_csv": ds.derived_csv,
            "derived_parquet": ds.derived_parquet,
            "parquet_status": ds.parquet_status,
            "parquet_available": has_parquet,
            "rows": ds.rows,
        }

    # ---------- private ----------

    def _find_dataset(self, std_code: str, period: str) -> _DatasetEntry:
        for ds in self._datasets:
            if ds.std_code == std_code and ds.period == period:
                return ds
        available = [(d.std_code, d.period) for d in self._datasets]
        raise KeyError(
            f"dataset {std_code}/{period} not found; "
            f"available: {available}"
        )

    @staticmethod
    def _load_csv(path: str) -> Tuple[List[Dict[str, str]], List[str]]:
        rows: List[Dict[str, str]] = []
        columns: List[str] = []
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            columns = list(reader.fieldnames or [])
            for row in reader:
                rows.append(dict(row))
        return rows, columns

    @staticmethod
    def _load_parquet(path: str) -> Tuple[List[Dict[str, Any]], List[str]]:
        import pyarrow.parquet as pq
        table = pq.read_table(path)
        columns = table.column_names
        rows = table.to_pylist()
        return rows, columns

    @staticmethod
    def _rows_to_dataframe(rows: List[Dict], columns: List[str]) -> Any:
        try:
            import pandas as pd
        except ImportError:
            raise ImportError(
                "pandas is required for as_dataframe=True; "
                "install it with: pip install pandas"
            )
        df = pd.DataFrame(rows)
        # 保留 metadata 列顺序，缺失列补 NaN
        for col in columns:
            if col not in df.columns:
                df[col] = None
        return df[list(columns)] if columns else df


def open_research_view(path: str) -> ResearchView:
    """打开投研派生视图。

    Parameters
    ----------
    path : str
        - 若指向 research_view_metadata.json 文件，直接读取；
        - 若指向包含该文件的目录，自动拼接。

    Returns
    -------
    ResearchView

    Raises
    ------
    FileNotFoundError
        metadata 文件不存在。
    """
    path = os.path.abspath(path)
    if os.path.isdir(path):
        metadata_path = os.path.join(path, _METADATA_FILENAME)
    else:
        metadata_path = path

    if not os.path.isfile(metadata_path):
        raise FileNotFoundError(
            f"research_view_metadata.json not found at {metadata_path}"
        )

    with open(metadata_path, "r", encoding="utf-8") as f:
        metadata = json.load(f)

    return ResearchView(metadata_path=metadata_path, metadata=metadata)
