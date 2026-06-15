import bisect
import numpy as np
from collections import deque


class FactorBase:
    """
    因子基类 v2
    
    架构升级:
    1. direction_type: 因子方向属性
       - "momentum": 绝对值越大→趋势越强(顺势因子)
         例: SpreadMomentum — momentum大→趋势强
       - "reversal": 绝对值越大→偏离越远→回归概率高(逆势因子)
         例: ZScore — |z|大→偏离远→MR信号强
       - "neutral": 无明确方向属性(如半衰期强度)
    
    2. 归一化路径 (v2 双轨, 2026-06-09):
       生产路径 (默认 use_rank_v2=False):
         - clip 归一化 (raw_value clip 到 ±clip_val 再除以 clip_val)
         - 行为与历史 backtest 完全一致 — 所有现存 profile 调参基于此
         - use_rank=True 但 _normalize 不自动维护 rank_window
           → 实际所有因子都走 clip 路径 (历史遗留行为, 保留语义稳定)
       实验路径 (use_rank_v2=True, 显式开启):
         - rank 滚动百分位归一化 (raw 在 N 窗口中的 rank → [-1,1])
         - _normalize 自动调 _append_rank_value, 前 20 个 bar fallback clip
         - 用于新策略 A/B 试验; 启用后必须重跑 WF 校准 profile

    3. 因子输出语义(纯统计，不含策略方向):
       - 正值: spread偏高/上升动量/偏离均值上方
       - 负值: spread偏低/下降动量/偏离均值下方
       - 绝对值: 偏离/动量强度
       策略方向(取反/不取反)由FactorEngine根据signal_mode统一处理
    """

    # 子类必须覆盖
    DIRECTION_TYPE = "neutral"  # momentum / reversal / neutral

    def __init__(self, name: str, params: dict):
        self._name = name
        self._params = params
        # rank标准化: 滚动窗口存储raw_value
        rank_window = params.get('rank_window', 200)
        self._rank_window = deque(maxlen=rank_window)
        self._rank_sorted = []
        # use_rank: 历史字段, 仅用于 v1 路径判断 (是否允许走 rank 分支)
        self._use_rank = params.get('use_rank', True)
        # use_rank_v2: 显式开关, 默认 False 保持生产行为
        # 启用后 _normalize 才会自动维护 rank_window 真正走 rank 路径
        self._use_rank_v2 = params.get('use_rank_v2', False)

    @property
    def name(self) -> str:
        return self._name

    @property
    def params(self) -> dict:
        return self._params

    @property
    def direction_type(self) -> str:
        return self.DIRECTION_TYPE

    def calculate(self, spread: np.ndarray, **kwargs) -> float:
        raise NotImplementedError

    def update_params(self, params: dict):
        self._params.update(params)

    def _normalize(self, raw_value: float, clip_val: float = 3.0) -> float:
        """
        标准化输出到[-1, 1]

        路径选择 (2026-06-09 双轨化):
          - use_rank_v2=True: 实验路径, 自动维护 rank_window 并走 rank 归一化
            (前 20 bar 窗口不足时 fallback clip)
          - 否则: 生产路径 (历史行为), 始终走 clip 归一化
            use_rank=True 也不再自动预热, 与历史 backtest 完全一致
        """
        # v2 实验路径: 真激活 rank 归一化
        if self._use_rank_v2:
            self._append_rank_value(raw_value)
            if len(self._rank_window) >= 20:
                return self._rank_normalize(raw_value)
            # 窗口不足: fallback clip
            clipped = np.clip(raw_value, -clip_val, clip_val)
            return float(clipped / clip_val)

        # 生产路径 (v1, 默认): clip 归一化
        # use_rank=True 但 _rank_window 永不预热 → 旧 _rank_normalize 永不触发
        # 这一行为被 test_rank_window_preserves_legacy_no_auto_warmup_behavior 显式锁定
        if self._use_rank and len(self._rank_window) >= 20:
            return self._rank_normalize(raw_value)

        # fallback: clip归一化
        clipped = np.clip(raw_value, -clip_val, clip_val)
        return float(clipped / clip_val)

    def _append_rank_value(self, raw_value: float):
        """维护原始顺序窗口和排序窗口。"""
        if self._rank_window.maxlen is not None and len(self._rank_window) == self._rank_window.maxlen:
            oldest = self._rank_window[0]
            idx = bisect.bisect_left(self._rank_sorted, oldest)
            if idx < len(self._rank_sorted) and self._rank_sorted[idx] == oldest:
                self._rank_sorted.pop(idx)

        self._rank_window.append(raw_value)
        bisect.insort(self._rank_sorted, raw_value)

    def _rank_normalize(self, raw_value: float) -> float:
        """
        排名百分位标准化 — 维护有序窗口，避免每bar重新sorted()
        
        当前raw_value在历史窗口中的排名 → 映射到[-1, 1]
        排名最高(最大) → +1, 排名最低(最小) → -1, 中位数 → 0
        """
        n = len(self._rank_sorted)
        if n == 0:
            return 0.0

        rank = bisect.bisect_right(self._rank_sorted, raw_value)
        
        # 映射到[-1, 1]: rank/n → percentile → [-1, 1]
        percentile = rank / n
        normalized = 2.0 * percentile - 1.0
        return float(np.clip(normalized, -1.0, 1.0))

    def reset_rank_window(self):
        """重置排名窗口，供on_session_begin调用"""
        self._rank_window.clear()
        self._rank_sorted.clear()