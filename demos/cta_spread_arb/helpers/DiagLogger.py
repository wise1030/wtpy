"""
DiagLogger — 结构化诊断日志

功能:
1. 交易诊断 — 记录每笔交易决策链(因子→信号→风控→执行)
2. 风控诊断 — 记录风控触发事件
3. 因子诊断 — 记录因子异常/失效
4. 统一格式 — JSON结构化，方便后续分析

使用:
    logger = DiagLogger(log_dir='./logs', level='INFO')
    logger.trade('rb-i', action='open', direction=1, signal=0.72,
                 factors={'coint_residual': 0.8}, risk_checks=[])
    logger.risk('rb-i', rule='profit_take', detail='peak=5000, current=2500')
    logger.factor('coint_residual', event='refit_failed', detail='OLS error')
"""
import json
import os
import time
import threading
from datetime import datetime


class DiagLogger:
    """结构化诊断日志器"""

    LEVELS = {'DEBUG': 0, 'INFO': 1, 'WARN': 2, 'ERROR': 3}

    def __init__(self, log_dir: str = './logs', level: str = 'INFO',
                 to_file: bool = True, to_console: bool = True):
        self._log_dir = log_dir
        self._level = self.LEVELS.get(level, 1)
        self._to_file = to_file
        self._to_console = to_console
        self._buffers = {
            'trade': [],
            'risk': [],
            'factor': [],
        }
        self._flush_interval = 50  # 每50条刷新一次
        self._lock = threading.Lock()  # 线程安全保护
        if to_file:
            os.makedirs(log_dir, exist_ok=True)

    def _emit(self, category: str, data: dict):
        """内部: 写入日志(线程安全)"""
        data['_ts'] = datetime.now().isoformat()
        data['_cat'] = category

        if self._to_console:
            self._print_one(data)

        if self._to_file:
            with self._lock:
                self._buffers[category].append(data)
                if len(self._buffers[category]) >= self._flush_interval:
                    self._flush_locked(category)

    def _print_one(self, data: dict):
        """控制台输出(紧凑格式)"""
        cat = data['_cat']
        ts = data['_ts'][11:19]  # HH:MM:SS
        pair = data.get('pair', data.get('factor', '?'))
        if cat == 'trade':
            action = data.get('action', '?')
            d = {1: '多', -1: '空', 0: '无'}.get(data.get('direction', 0), '?')
            sig = data.get('signal', 0)
            print(f"[{ts}] TRADE {pair} {action} {d} sig={sig:.3f}")
        elif cat == 'risk':
            rule = data.get('rule', '?')
            detail = data.get('detail', '')
            print(f"[{ts}] RISK  {pair} {rule}: {detail}")
        elif cat == 'factor':
            event = data.get('event', '?')
            detail = data.get('detail', '')
            print(f"[{ts}] FACTOR {pair} {event}: {detail}")

    def _flush(self, category: str):
        """刷新缓冲区到文件(外部调用，自动加锁)"""
        with self._lock:
            self._flush_locked(category)

    def _flush_locked(self, category: str):
        """刷新缓冲区到文件(调用方已持有锁)"""
        if not self._buffers[category]:
            return
        path = os.path.join(self._log_dir, f"{category}.jsonl")
        with open(path, 'a') as f:
            for item in self._buffers[category]:
                f.write(json.dumps(item, ensure_ascii=False) + '\n')
        self._buffers[category] = []

    def flush_all(self):
        """刷新所有缓冲区"""
        for cat in self._buffers:
            self._flush(cat)

    # ── 公开API ──

    def trade(self, pair: str, action: str, direction: int = 0,
              signal: float = 0.0, factors: dict = None,
              risk_checks: list = None, pnl: float = None,
              level: str = 'INFO'):
        """交易诊断日志"""
        if self.LEVELS.get(level, 1) < self._level:
            return
        data = {
            'pair': pair, 'action': action, 'direction': direction,
            'signal': signal,
        }
        if factors:
            data['factors'] = factors
        if risk_checks:
            data['risk_checks'] = risk_checks
        if pnl is not None:
            data['pnl'] = pnl
        self._emit('trade', data)

    def risk(self, pair: str, rule: str, detail: str = '',
             level: str = 'WARN'):
        """风控诊断日志"""
        if self.LEVELS.get(level, 1) < self._level:
            return
        self._emit('risk', {'pair': pair, 'rule': rule, 'detail': detail})

    def factor(self, factor_name: str, event: str, detail: str = '',
               level: str = 'INFO'):
        """因子诊断日志"""
        if self.LEVELS.get(level, 1) < self._level:
            return
        self._emit('factor', {'factor': factor_name, 'event': event, 'detail': detail})

    def debug(self, pair: str, msg: str, **kwargs):
        """通用调试日志"""
        data = {'pair': pair, 'msg': msg, **kwargs}
        self._emit('trade', data)
