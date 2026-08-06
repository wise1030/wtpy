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

    # B-5(2026-07): 品种经济性准入门槛 — fee/avg_win 上限。
    # C2诊断(PROJECT_ASSESSMENT_20260609 §2): rb/hc fee/avg_win=1.27%~1.47%
    # 结构性不可盈利(盈亏平衡胜率>实际胜率), l/pp=0.49% 健康。
    # 0.8% 为两者之间的人性化截断, 可按需调整。
    FEE_RATIO_MAX = 0.008

    def __init__(self, data_dir: str = None):
        """
        Args:
            data_dir: 历史数据目录(可选，用于从文件加载数据)
        """
        self._data_dir = data_dir

    @staticmethod
    def check_fee_economics(fee_per_round: float, avg_win: float,
                            max_ratio: float = None) -> tuple:
        """B-5: 品种经济性检查 — 手续费占平均盈利比是否可接受。

        Args:
            fee_per_round: 单轮(开+平, 双腿合计)手续费(元)
            avg_win: 历史/预估平均每笔盈利(元)
            max_ratio: fee/avg_win 上限(默认 FEE_RATIO_MAX=0.008)

        Returns:
            (ok, ratio, msg): ok=True 表示经济性达标
        """
        max_ratio = max_ratio if max_ratio is not None else PairScreener.FEE_RATIO_MAX
        if avg_win <= 0:
            return False, float('inf'), f"avg_win={avg_win}<=0, 无法评估"
        ratio = fee_per_round / avg_win
        if ratio >= max_ratio:
            return False, ratio, (
                f"fee/avg_win={ratio:.2%} >= {max_ratio:.2%}, "
                f"品种经济性不达标(参考: rb/hc=1.27%亏损, l/pp=0.49%健康)")
        return True, ratio, f"fee/avg_win={ratio:.2%} < {max_ratio:.2%}, OK"

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
               min_corr: float = 0.3,
               fee_estimates: dict = None,
               fee_ratio_max: float = None,
               exclude_fee_fail: bool = False) -> list:
        """
        批量筛选品种组合

        Args:
            candidates: [(code_a, code_b), ...] 候选组合列表
            min_coint_p: 协整p值阈值
            min_half_life: 最小半衰期
            max_half_life: 最大半衰期
            min_corr: 最小相关系数
            fee_estimates: B-5(2026-07): 可选, {pair_name: (fee_per_round, avg_win)}
                提供时对每个组合做品种经济性检查, 结果附 fee_ratio/fee_ok/fee_msg
            fee_ratio_max: fee/avg_win 上限(默认 FEE_RATIO_MAX)
            exclude_fee_fail: True 时经济性不达标的组合直接剔除

        Returns:
            按score降序排列的评估结果列表
        """
        results = []
        for pair in candidates:
            if len(pair) != 2:
                continue
            code_a, code_b = pair
            pair_name = f"{code_a}/{code_b}"
            # 从数据文件加载(如果有data_dir)
            prices_a = self._load_prices(code_a)
            prices_b = self._load_prices(code_b)
            if prices_a is None or prices_b is None:
                results.append({
                    "name": pair_name,
                    "error": "no data",
                    "score": 0
                })
                continue

            eval_result = self.evaluate(prices_a, prices_b, name=pair_name)

            # B-5: 品种经济性准入检查
            if fee_estimates and pair_name in fee_estimates:
                fee_per_round, avg_win = fee_estimates[pair_name]
                ok, ratio, msg = self.check_fee_economics(
                    fee_per_round, avg_win, fee_ratio_max)
                eval_result['fee_ratio'] = ratio
                eval_result['fee_ok'] = ok
                eval_result['fee_msg'] = msg
                if exclude_fee_fail and not ok:
                    continue

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
        print(f"\n{'='*78}")
        print(f"{'品种组合':<25} {'评分':>6} {'ADF p':>8} {'半衰期':>8} {'相关':>6} {'协整':>4} {'fee比':>8}")
        print(f"{'-'*78}")
        for r in results:
            if 'error' in r:
                print(f"{r['name']:<25} {r['error']}")
                continue
            coint_mark = "Y" if r.get('cointegrated', False) else "N"
            # B-5: fee_ratio 列(无数据时 '-', 不达标加 '!')
            if 'fee_ratio' in r:
                fee_mark = f"{r['fee_ratio']:>7.2%}" + (" " if r['fee_ok'] else "!")
            else:
                fee_mark = "     -  "
            print(f"{r['name']:<25} {r['score']:>6.3f} {r['adf_p']:>8.4f} "
                  f"{r['half_life']:>8.1f} {r['corr']:>6.3f} {coint_mark:>4} {fee_mark}")
        print(f"{'='*78}\n")
