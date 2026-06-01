"""
PairScreener — 品种组合筛选与评估工具

功能:
1. 评估品种组合的协整质量(ADF p值, 半衰期, 相关系数)
2. 按协整质量排序，推荐最佳品种组合
3. 支持批量扫描候选组合
4. 输出评估报告供策略配置参考

使用:
    screener = PairScreener(data_dir='/path/to/hist_data')
    results = screener.screen([
        ('SHFE.rb.HOT', 'DCE.i.HOT'),
        ('SHFE.rb.HOT', 'SHFE.hc.HOT'),
        ('DCE.m.HOT', 'DCE.y.HOT'),
    ])
    for pair, score, adf_p, hl in results:
        print(f"{pair}: score={score:.3f}, adf_p={adf_p:.4f}, hl={hl:.1f}")
"""
import numpy as np
import os
import json


class PairScreener:
    """品种组合筛选器"""

    def __init__(self, data_dir: str = None):
        """
        Args:
            data_dir: 历史数据目录(可选，用于从文件加载数据)
        """
        self._data_dir = data_dir

    def evaluate(self, prices_a: np.ndarray, prices_b: np.ndarray,
                 name: str = "pair") -> dict:
        """
        评估单个品种组合的协整质量

        Args:
            prices_a: 品种A价格序列
            prices_b: 品种B价格序列
            name: 组合名称

        Returns:
            评估结果dict: {name, adf_p, half_life, corr, score, coint_beta, coint_c}
        """
        min_len = min(len(prices_a), len(prices_b))
        if min_len < 60:
            return {"name": name, "error": "insufficient data", "score": 0}

        p1 = np.array(prices_a[-min_len:], dtype=float)
        p2 = np.array(prices_b[-min_len:], dtype=float)

        # 1. 相关系数
        corr = float(np.corrcoef(p1, p2)[0, 1])

        # 2. OLS回归
        matrix = np.vstack([p2, np.ones(len(p2))]).T
        try:
            result = np.linalg.lstsq(matrix, p1, rcond=None)
            beta = float(result[0][0])
            c = float(result[0][1])
        except Exception:
            return {"name": name, "error": "OLS failed", "score": 0}

        # 3. 残差
        resid = p1 - beta * p2 - c

        # 4. ADF检验
        adf_p = 1.0
        try:
            from statsmodels.tsa.stattools import adfuller
            adf_result = adfuller(resid, maxlag=1)
            adf_p = float(adf_result[1])
        except (ImportError, Exception):
            pass

        # 5. 半衰期
        half_life = 999.0
        dy = np.diff(resid)
        y_lag = resid[:-1]
        if np.std(y_lag) > 0:
            try:
                matrix_hl = np.vstack([y_lag, np.ones(len(y_lag))]).T
                result_hl = np.linalg.lstsq(matrix_hl, dy, rcond=None)
                beta_hl = result_hl[0][0]
                if beta_hl < 0:
                    half_life = float(-np.log(2) / beta_hl)
            except Exception:
                pass

        # 6. 综合评分: 协整显著 + 半衰期短 + 相关性高 → 高分
        coint_score = max(0, 1.0 - adf_p)  # p值越小越好
        hl_score = 0.0
        if 1 <= half_life <= 30:
            hl_score = 1.0 - (half_life - 1) / 29.0  # 半衰期越短越好
        elif half_life < 1:
            hl_score = 1.0
        corr_score = max(0, abs(corr))  # 相关性越高越好

        score = coint_score * 0.5 + hl_score * 0.3 + corr_score * 0.2

        return {
            "name": name,
            "adf_p": adf_p,
            "half_life": half_life,
            "corr": corr,
            "coint_beta": beta,
            "coint_c": c,
            "score": score,
            "cointegrated": adf_p < 0.05,
        }

    def screen(self, candidates: list, min_coint_p: float = 0.05,
               min_half_life: float = 1.0, max_half_life: float = 30.0,
               min_corr: float = 0.3) -> list:
        """
        批量筛选品种组合

        Args:
            candidates: [(code_a, code_b), ...] 候选组合列表
            min_coint_p: 协整p值阈值
            min_half_life: 最小半衰期
            max_half_life: 最大半衰期
            min_corr: 最小相关系数

        Returns:
            按score降序排列的评估结果列表
        """
        results = []
        for pair in candidates:
            if len(pair) != 2:
                continue
            code_a, code_b = pair
            # 从数据文件加载(如果有data_dir)
            prices_a = self._load_prices(code_a)
            prices_b = self._load_prices(code_b)
            if prices_a is None or prices_b is None:
                results.append({
                    "name": f"{code_a}/{code_b}",
                    "error": "no data",
                    "score": 0
                })
                continue

            eval_result = self.evaluate(prices_a, prices_b, name=f"{code_a}/{code_b}")
            results.append(eval_result)

        # 按score降序排列
        results.sort(key=lambda x: -x.get('score', 0))
        return results

    def _load_prices(self, code: str) -> np.ndarray:
        """从数据文件加载价格序列(简化版)"""
        if self._data_dir is None:
            return None
        # 尝试加载CSV或npy
        safe_name = code.replace('.', '_')
        csv_path = os.path.join(self._data_dir, f"{safe_name}.csv")
        npy_path = os.path.join(self._data_dir, f"{safe_name}.npy")

        if os.path.exists(npy_path):
            return np.load(npy_path)
        elif os.path.exists(csv_path):
            data = np.genfromtxt(csv_path, delimiter=',', skip_header=1)
            if len(data) > 0 and data.ndim == 2:
                return data[:, -1]  # 最后一列为收盘价
        return None

    @staticmethod
    def print_report(results: list):
        """打印评估报告"""
        print(f"\n{'='*70}")
        print(f"{'品种组合':<25} {'评分':>6} {'ADF p':>8} {'半衰期':>8} {'相关':>6} {'协整':>4}")
        print(f"{'-'*70}")
        for r in results:
            if 'error' in r:
                print(f"{r['name']:<25} {r['error']}")
                continue
            coint_mark = "Y" if r.get('cointegrated', False) else "N"
            print(f"{r['name']:<25} {r['score']:>6.3f} {r['adf_p']:>8.4f} "
                  f"{r['half_life']:>8.1f} {r['corr']:>6.3f} {coint_mark:>4}")
        print(f"{'='*70}\n")
