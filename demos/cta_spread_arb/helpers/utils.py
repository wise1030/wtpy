"""
utils — 公共工具函数

提取项目内多处重复使用的工具方法。
"""
import numpy as np


def simple_regime(spread, coint_p: float = 1.0) -> dict:
    """简单regime判断(无RegimeDetector时的fallback)

    逻辑:
    - spread长度不足60 → range
    - MA10 vs MA30的z-score > 0.8 → trend
    - 协整p < 0.05 → mr(均值回归)
    - 其他 → range

    Args:
        spread: 价差序列(numpy array或list)
        coint_p: 协整p值

    Returns:
        dict with keys: regime, confidence, direction
    """
    if spread is None or len(spread) < 60:
        return {'regime': 'range', 'confidence': 0.0, 'direction': 0}
    ma10 = np.mean(spread[-10:])
    ma30 = np.mean(spread[-30:])
    std = np.std(spread[-60:])
    if std == 0:
        return {'regime': 'range', 'confidence': 0.0, 'direction': 0}
    slope_z = (ma10 - ma30) / std
    if abs(slope_z) > 0.8:
        direction = 1 if slope_z > 0 else -1
        return {'regime': 'trend', 'confidence': min(1.0, abs(slope_z)), 'direction': direction}
    elif coint_p < 0.05:
        return {'regime': 'mr', 'confidence': 0.6, 'direction': 0}
    else:
        return {'regime': 'range', 'confidence': 0.3, 'direction': 0}
