import json
import os
import tempfile
from wtpy.ExtToolDefs import BaseIndexWriter, BaseDataReporter


class ArbIndexWriter(BaseIndexWriter):
    """
    套利指标输出工具

    负责将策略指标数据写入文件，供数据报告器读取
    """

    def __init__(self):
        super().__init__()
        self._output_dir = "./generated"
        self._file_handles = {}  # 缓存文件句柄，避免频繁open/close

    def write_indicator(self, id: str, tag: str, time: int, data: dict):
        """
        写入指标数据到JSON文件

        使用原子写入（tempfile + os.replace）防止崩溃时数据丢失
        """
        dir_path = os.path.join(self._output_dir, tag)
        os.makedirs(dir_path, exist_ok=True)
        filepath = os.path.join(dir_path, f"{id}.json")

        # 原子写入: tempfile + os.replace
        try:
            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.json',
                dir=dir_path, delete=False) as tmp:
                json.dump(data, tmp, ensure_ascii=False)
                tmp_path = tmp.name
            os.replace(tmp_path, filepath)
        except Exception:
            pass

    def write_arb_config(self, strategy_id: str, sub_strategies: list):
        """写入套利配置到marker.json"""
        dir_path = os.path.join(self._output_dir)
        os.makedirs(dir_path, exist_ok=True)
        filepath = os.path.join(dir_path, "marker.json")

        marks = [f"{strategy_id}_{sub['name']}" for sub in sub_strategies]
        data = {"marks": marks}

        try:
            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.json',
                dir=dir_path, delete=False) as tmp:
                json.dump(data, tmp, ensure_ascii=False)
                tmp_path = tmp.name
            os.replace(tmp_path, filepath)
        except Exception:
            pass

    def close(self):
        """关闭所有缓存的文件句柄"""
        for handle in self._file_handles.values():
            try:
                handle.close()
            except Exception:
                pass
        self._file_handles.clear()


