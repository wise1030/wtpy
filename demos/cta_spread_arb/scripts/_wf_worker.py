#!/usr/bin/env python3
"""临时回测脚本 — 被 walk_forward.py 子进程调用"""
import os, sys, json

PROJECT_ROOT = os.environ.get('WF_PROJECT_ROOT', '.')
for _p in (PROJECT_ROOT,
           os.path.join(PROJECT_ROOT, 'strategies'),
           os.path.join(PROJECT_ROOT, 'helpers'),
           os.path.join(PROJECT_ROOT, 'factors')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.chdir(PROJECT_ROOT)

from wtpy import WtBtEngine, EngineType
from SpreadArbStrategy import SpreadArbStrategy

# 参数从环境变量读取
experiment = os.environ['WF_EXPERIMENT']
config_root = os.environ['WF_CONFIG_ROOT']
start = int(os.environ['WF_START'])
end = int(os.environ['WF_END'])
slippage = int(os.environ.get('WF_SLIPPAGE', '0'))

engine = WtBtEngine(EngineType.ET_CTA)
engine.init('./common/', "configbt.yaml")
engine.configBacktest(start, end)
engine.configBTStorage(mode="csv", path="./storage/")
engine.commitBTConfig()

stra = SpreadArbStrategy.from_experiment(
    name='wf_sub',
    experiment=experiment,
    config_root=config_root,
    is_backtest=True,
)
engine.set_cta_strategy(stra, slippage=slippage)
engine.run_backtest(bAsync=False)

result = {
    'realized_pnl': stra.core.rm._realized_pnl,
    'name': stra.core.name,
}
engine.release_backtest()

print("WF_RESULT=" + json.dumps(result))
