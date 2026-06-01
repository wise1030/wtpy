"""
信号诊断回测 - 在策略中注入信号日志，分析信号过滤链
"""
import sys
import os
import yaml
import json
import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# 确保基线参数
with open(os.path.join(BASE_DIR, 'config/factor_config.yaml'), 'r') as f:
    fc = yaml.safe_load(f)
fc['signal_aggregation']['entry_threshold'] = 0.30
fc['signal_aggregation']['exit_threshold'] = 0.05
with open(os.path.join(BASE_DIR, 'config/factor_config.yaml'), 'w') as f:
    yaml.dump(fc, f, default_flow_style=False)

with open(os.path.join(BASE_DIR, 'config/strategy_config.yaml'), 'r') as f:
    sc = yaml.safe_load(f)
sc['risk_control']['max_loss_per_pair'] = 10000
sc['risk_control']['max_hold_bars'] = 30
sc['risk_control']['cooldown_bars'] = 12
sc['risk_control']['trailing_decay_ratio'] = 0.5
with open(os.path.join(BASE_DIR, 'config/strategy_config.yaml'), 'w') as f:
    yaml.dump(sc, f, default_flow_style=False)

# Monkey-patch策略的on_bar来记录信号
from strategies.SpreadArbStrategy import SpreadArbStrategy
orig_on_bar = SpreadArbStrategy.on_bar

signal_log = []

def patched_on_bar(self, curBar):
    # 调用原始on_bar
    orig_on_bar(self, curBar)
    
    # 记录信号
    if hasattr(self, '_last_signal') and self._last_signal is not None:
        sig = self._last_signal
        signal_log.append({
            'bar': curBar.bartime if hasattr(curBar, 'bartime') else 0,
            'total_score': sig.get('total_score', 0),
            'entry_signal': sig.get('entry_signal', 0),
            'should_trade': sig.get('should_trade', False),
            'should_exit': sig.get('should_exit', False),
            'regime': sig.get('regime', 'unknown'),
            'direction': sig.get('direction', 0),
        })

SpreadArbStrategy.on_bar = patched_on_bar

# 也patch策略的信号生成
orig_generate_signal = None
for name, method in SpreadArbStrategy.__dict__.items():
    if name == '_generate_signal':
        orig_generate_signal = method
        break

# 运行回测
from wtpy import WtBtEngine, WtGate
from wtpy.WtCoreDefs import EngineType

engine = WtBtEngine()
engine.configBTStorage(mode="csv", path="../storage/")
engine.set_time_range(201901010900, 202001011500)
engine.init(os.path.join(BASE_DIR, 'configbt.yaml'), 'spread_arb', SpreadArbStrategy(), None)
engine.run_backtest()

# 分析信号
print(f"\n=== 信号诊断结果 ===")
print(f"总信号记录数: {len(signal_log)}")

if signal_log:
    # 过滤非零信号
    nonzero = [s for s in signal_log if abs(s['entry_signal']) > 0.01]
    print(f"非零entry_signal: {len(nonzero)}")
    
    # 信号强度分布
    if nonzero:
        signals = [abs(s['entry_signal']) for s in nonzero]
        print(f"entry_signal |mean|: {np.mean(signals):.4f}")
        print(f"entry_signal |max|: {np.max(signals):.4f}")
        print(f"entry_signal |median|: {np.median(signals):.4f}")
        
        # 超过阈值的
        above_03 = [s for s in nonzero if abs(s['entry_signal']) > 0.30]
        above_02 = [s for s in nonzero if abs(s['entry_signal']) > 0.20]
        above_01 = [s for s in nonzero if abs(s['entry_signal']) > 0.10]
        print(f"|entry_signal| > 0.10: {len(above_01)}")
        print(f"|entry_signal| > 0.20: {len(above_02)}")
        print(f"|entry_signal| > 0.30: {len(above_03)}")
        
        # regime分布
        from collections import Counter
        regimes = Counter(s['regime'] for s in nonzero)
        print(f"\nRegime分布: {dict(regimes)}")
        
        # should_trade=True的
        trades = [s for s in signal_log if s['should_trade']]
        print(f"\nshould_trade=True: {len(trades)}")
        for t in trades[:10]:
            print(f"  bar={t['bar']} entry_signal={t['entry_signal']:.4f} regime={t['regime']} total_score={t['total_score']:.4f}")
    
    # total_score分布
    total_scores = [abs(s['total_score']) for s in signal_log]
    nonzero_ts = [ts for ts in total_scores if ts > 0.01]
    if nonzero_ts:
        print(f"\ntotal_score |mean|: {np.mean(nonzero_ts):.4f}")
        print(f"total_score |max|: {np.max(nonzero_ts):.4f}")
        above_03_ts = sum(1 for ts in nonzero_ts if ts > 0.30)
        above_02_ts = sum(1 for ts in nonzero_ts if ts > 0.20)
        print(f"|total_score| > 0.20: {above_02_ts}")
        print(f"|total_score| > 0.30: {above_03_ts}")
