"""海龟信号状态机 (纯逻辑, 无框架依赖, 可单测)

职责:
- S1: 20 突破入场 / 10 突破出场 + 盈利跳过过滤 + 55 fail-safe
- S2: 55 突破入场 / 20 突破出场, 无过滤
- 加仓: 每盈利 step×N 加一层, 上限 max_layers
- 止损: 最后加仓价 ∓ stop_mult×N, bar 内 low/high 触碰判定

信号边沿触发 (复核修正):
- 入场信号 = 收盘"新"突破通道 (close > upper 且 prev_close <= prev_upper)
- 收盘持续站上通道不重复触发 — 否则被跳过的信号每 bar 重写 sim 单,
  且持仓出场后会立即原地重新入场
- fail-safe 必须带状态 (_fs_armed): 价格先破 20(被跳过) 数日后再破 55 时,
  55 突破 bar 通常没有新的 20 边沿, 无状态实现会漏掉强制入场

决策-确认两段式:
  on_bar() 返回决策列表, 策略层执行(方向闸/手数/下单)后回调
  confirm_enter/confirm_add/confirm_exit 提交状态。
  被方向闸或手数不足否决的入场不提交 → 不影响 S1 过滤器状态。

S1 过滤器口径(复核确认):
- 跟踪该市场上一次 S1 突破(不分方向), 模拟其 2N 止损 / 10 通道出场判定输赢
- 被跳过的信号记为 sim 单 + _fs_armed; sim 止损/出场后过滤器与 fail-safe 一并解除
"""

DEC_ENTER = "enter"
DEC_ADD = "add"
DEC_EXIT = "exit"
DEC_SKIP = "skip"      # S1 过滤器跳过 (引擎已记录 sim 单并武装 fail-safe)


class TurtleEngine:
    def __init__(self, system: str = "S1",
                 stop_atr_mult: float = 2.0,
                 pyramid_layers: int = 4,
                 pyramid_step_atr: float = 0.5):
        assert system in ("S1", "S2")
        self.system = system
        self.stop_mult = stop_atr_mult
        self.max_layers = max(1, pyramid_layers)
        self.step_atr = pyramid_step_atr

        self.direction = 0                    # 0 平 / +1 多 / -1 空
        self.layers = []                      # [[lots, price], ...]
        # S1 过滤器: {direction, entry_price, n, taken, status, result}
        self.s1_last = None
        self._fs_armed = 0                    # fail-safe 武装方向 (S1)

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def on_bar(self, high: float, low: float, close: float,
               prev_close: float, n: float, channels: dict) -> list:
        """评估单 bar, 返回决策列表 (按执行顺序)。

        channels: {entry_upper, entry_lower, exit_upper, exit_lower,
                   fs_upper, fs_lower,
                   prev_entry_upper, prev_entry_lower,
                   prev_fs_upper, prev_fs_lower}
        通道值不含当前 bar; prev_* 为上一 bar 的通道值 (边沿判定用)。
        """
        if n is None or n != n or n <= 0:
            return []
        if any(v is None or v != v for v in channels.values()):
            return []

        decisions = []
        exited_this_bar = False   # 两段式确认: 本 bar 决策出场后按空仓评估入场

        # === 持仓管理 ===
        if self.direction != 0:
            stop_price = self._stop_price(n)
            exit_reason = None
            exit_price = None
            if self.direction > 0:
                if low <= stop_price:
                    exit_reason, exit_price = "stop", stop_price
                elif close < channels['exit_lower']:
                    exit_reason, exit_price = "channel", close
            else:
                if high >= stop_price:
                    exit_reason, exit_price = "stop", stop_price
                elif close > channels['exit_upper']:
                    exit_reason, exit_price = "channel", close

            if exit_reason:
                decisions.append({
                    "decision": DEC_EXIT, "reason": exit_reason,
                    "price": exit_price})
                exited_this_bar = True
                # 出场后本 bar 继续评估入场 (同 bar 反转)
            else:
                # 加仓: 未触发离场时才评估 (水平触发, 以最后加仓价为锚)
                if len(self.layers) < self.max_layers:
                    last_price = self.layers[-1][1]
                    if self.direction > 0 and \
                            close >= last_price + self.step_atr * n:
                        decisions.append({"decision": DEC_ADD, "price": close})
                    elif self.direction < 0 and \
                            close <= last_price - self.step_atr * n:
                        decisions.append({"decision": DEC_ADD, "price": close})
                return decisions   # 持仓未出, 不评估入场

        # === S1 sim 单推进 (独立于持仓) ===
        self._advance_sim(high, low, close, channels)

        # === 入场评估 (当前空仓或本 bar 已决策出场, 边沿触发) ===
        if self.direction == 0 or exited_this_bar:
            sig_dir = 0
            if close > channels['entry_upper'] and \
                    prev_close <= channels['prev_entry_upper']:
                sig_dir = 1
            elif close < channels['entry_lower'] and \
                    prev_close >= channels['prev_entry_lower']:
                sig_dir = -1

            if sig_dir != 0:
                decisions.extend(self._eval_entry(sig_dir, close, n, channels))
            elif self.system == "S1" and self._fs_armed != 0:
                # fail-safe: 无新 20 边沿但武装中, 检查 55 边沿
                d = self._fs_armed
                fs_hit = (close > channels['fs_upper'] and
                          prev_close <= channels['prev_fs_upper']) if d > 0 \
                    else (close < channels['fs_lower'] and
                          prev_close >= channels['prev_fs_lower'])
                if fs_hit:
                    decisions.append({
                        "decision": DEC_ENTER, "direction": d,
                        "price": close, "n": n, "failsafe": True})

        return decisions

    def _eval_entry(self, sig_dir: int, close: float, n: float,
                    channels: dict) -> list:
        """新鲜入场信号的 S1/S2 分流"""
        if self.system == "S2":
            return [{"decision": DEC_ENTER, "direction": sig_dir,
                     "price": close, "n": n, "failsafe": False}]
        # S1: 过滤判定
        if not self._s1_filter_skip(close):
            return [{"decision": DEC_ENTER, "direction": sig_dir,
                     "price": close, "n": n, "failsafe": False}]
        # 过滤命中 → fail-safe 检查 (55 同时被新鲜突破则强制入场)
        fs_hit = (close > channels['fs_upper'] and
                  close > channels['prev_fs_upper']) if sig_dir > 0 \
            else (close < channels['fs_lower'] and
                  close < channels['prev_fs_lower'])
        if fs_hit:
            return [{"decision": DEC_ENTER, "direction": sig_dir,
                     "price": close, "n": n, "failsafe": True}]
        self._record_sim(sig_dir, close, n)
        return [{"decision": DEC_SKIP, "direction": sig_dir,
                 "reason": "s1_last_winner", "price": close}]

    # ------------------------------------------------------------------
    # 确认回调 (策略层执行后调用)
    # ------------------------------------------------------------------
    def confirm_enter(self, direction: int, lots: int, price: float):
        self.direction = direction
        self.layers = [[lots, price]]
        self._fs_armed = 0
        if self.system == "S1":
            # 实盘入场成为新的 s1_last (taken, 持仓中)
            self.s1_last = {
                "direction": direction, "entry_price": price, "n": None,
                "taken": True, "status": "open", "result": None}

    def confirm_add(self, lots: int, price: float):
        if self.direction != 0:
            self.layers.append([lots, price])

    def confirm_exit(self, price: float):
        if self.direction == 0:
            return
        # 实单结果 → S1 过滤器 (按手数加权均价判定输赢)
        if self.system == "S1" and self.s1_last and self.s1_last.get("taken"):
            tot_lots = sum(l for l, _ in self.layers)
            avg_entry = sum(l * p for l, p in self.layers) / tot_lots
            pnl = (price - avg_entry) * self.direction
            self.s1_last["status"] = "closed"
            self.s1_last["result"] = "win" if pnl > 0 else "loss"
        self.direction = 0
        self.layers = []
        self._fs_armed = 0

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _stop_price(self, n: float) -> float:
        last_price = self.layers[-1][1]
        if self.direction > 0:
            return last_price - self.stop_mult * n
        return last_price + self.stop_mult * n

    def _s1_filter_skip(self, close: float) -> bool:
        """上一次 S1 突破盈利(或浮盈中) → True 跳过"""
        last = self.s1_last
        if last is None:
            return False
        if last["status"] == "closed":
            return last["result"] == "win"
        # open 且未 taken = sim 单: 当前浮盈 > 0 视为 winner
        unrealized = (close - last["entry_price"]) * last["direction"]
        return unrealized > 0

    def _record_sim(self, sig_dir: int, price: float, n: float):
        """被跳过的信号记为 sim 单 (替换旧记录, 经典口径只跟踪最近一次),
        并武装 fail-safe"""
        self.s1_last = {
            "direction": sig_dir, "entry_price": price, "n": n,
            "taken": False, "status": "open", "result": None}
        self._fs_armed = sig_dir

    def _advance_sim(self, high: float, low: float, close: float,
                     channels: dict):
        """推进被跳过 sim 单的止损/出场判定 (S1 出场通道);
        sim 终结时解除 fail-safe (本轮被跳过的行情已结束)"""
        last = self.s1_last
        if last is None or last.get("taken") or last["status"] != "open":
            return
        n0 = last["n"]
        d = last["direction"]
        e = last["entry_price"]
        closed = False
        if d > 0:
            if low <= e - self.stop_mult * n0:
                last["status"], last["result"] = "closed", "loss"
                closed = True
            elif close < channels['exit_lower']:
                last["status"] = "closed"
                last["result"] = "win" if close > e else "loss"
                closed = True
        else:
            if high >= e + self.stop_mult * n0:
                last["status"], last["result"] = "closed", "loss"
                closed = True
            elif close > channels['exit_upper']:
                last["status"] = "closed"
                last["result"] = "win" if close < e else "loss"
                closed = True
        if closed:
            self._fs_armed = 0

    # ------------------------------------------------------------------
    # 持久化 / 换月
    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "system": self.system,
            "direction": self.direction,
            "layers": [list(x) for x in self.layers],
            "s1_last": self.s1_last,
            "fs_armed": self._fs_armed,
        }

    def restore(self, state: dict):
        self.direction = state.get("direction", 0)
        self.layers = [list(x) for x in state.get("layers", [])]
        self.s1_last = state.get("s1_last")
        self._fs_armed = state.get("fs_armed", 0)

    def shift_prices(self, delta: float):
        """HOT 换月 re-anchor: 各层价格与 sim 入场价平移 delta"""
        if delta == 0:
            return
        for layer in self.layers:
            layer[1] += delta
        if self.s1_last and self.s1_last.get("entry_price") is not None:
            self.s1_last["entry_price"] += delta
