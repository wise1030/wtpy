import json
import os
import tempfile
from wtpy.ExtToolDefs import BaseIndexWriter, BaseDataReporter


class ArbIndexWriter(BaseIndexWriter):
    """
    套利指标输出工具

    负责将策略指标数据写入文件，供数据报告器读取

    P-4(2026-07): 缓冲批量写 — 原每bar一次 makedirs+NamedTemporaryFile+
    json.dump+os.replace(真实文件创建+写盘+rename)。输出语义是"最新快照
    覆盖"(文件名={id}.json), 因此缓存最新data定期flush即可, 无信息损失。
    """

    def __init__(self, flush_every: int = 10):
        super().__init__()
        self._output_dir = "./generated"
        self._flush_every = max(1, flush_every)
        self._pending = {}      # (id, tag) -> data (只留最新快照)
        self._write_count = 0

    def write_indicator(self, id: str, tag: str, time: int, data: dict):
        """缓存指标数据, 每 flush_every 次批量落盘"""
        self._pending[(id, tag)] = data
        self._write_count += 1
        if self._write_count >= self._flush_every:
            self.flush()

    def flush(self):
        """把缓存的最新快照批量落盘(原子写入)"""
        for (id, tag), data in self._pending.items():
            self._write_one(id, tag, data)
        self._pending.clear()
        self._write_count = 0

    def _write_one(self, id: str, tag: str, data: dict):
        """原子写入单个指标文件(tempfile + os.replace)"""
        dir_path = os.path.join(self._output_dir, tag)
        os.makedirs(dir_path, exist_ok=True)
        filepath = os.path.join(dir_path, f"{id}.json")

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
        """写入套利配置到marker.json(单策略, 覆盖写)"""
        self.write_arb_config_multi([(strategy_id, sub_strategies)])

    def write_arb_config_multi(self, strategy_subs: list):
        """A-4(2026-07): 多策略合并写 marker.json。

        Args:
            strategy_subs: [(strategy_id, sub_strategies), ...]
                sub 元素兼容 dict(取 sub['name']) 和对象(取 .name, 如 ArbCore)。
        """
        dir_path = os.path.join(self._output_dir)
        os.makedirs(dir_path, exist_ok=True)
        filepath = os.path.join(dir_path, "marker.json")

        marks = []
        for strategy_id, subs in strategy_subs:
            for sub in subs:
                # 兼容 dict 与 ArbCore 对象(原 sub['name'] 对对象会 TypeError)
                name = sub.get('name') if isinstance(sub, dict) else getattr(sub, 'name', None)
                if name:
                    marks.append(f"{strategy_id}_{name}")

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
        """P-4: 关闭前 flush 剩余缓存(原 _file_handles 是从未写入的死代码)"""
        self.flush()
