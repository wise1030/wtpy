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

N 口径 (stop_n_mode, P0-2 2026-09-15):
  止损距离与加仓步长用哪个 N, 决定风控记账是否成立。当根 N 会漂移
  (SHFE.rb 日线实测: 持有 20 根 N 比值 p95=1.40、极值 2.14 倍):
  用当根 N 时止损会被动放宽 → 单层实际止损亏损可达 RiskManager 记账
  2N 的 2 倍以上; 且 N 缩小时加仓位下移 → 价格未上涨也会触发加仓。
  三口径:
  - entry  : 该层入/加仓时点冻结 N (layer_ns)。经典口径, 与风控 2N
             记账严格一致;
  - current: 当根 N (2026-09-15 前行为), 上述两个问题均存在, 仅用于
             A/B 复算与回退;
  - ratchet: 当根 N 但只收紧不放宽 (默认) — 多头止损只上移、加仓位
             只上移 (空头反之), 两次加仓之间单调, 加仓时按新单位重锚。
             既保留 N 收缩时的动态收紧, 又不放宽风险; 组合矩阵实测最优
             (12 品种 2019-01~2026-08: 70.2% vs entry 63.1% / current 64.9%)。
  HOT 换月的价格漂移由 shift_prices/reanchor_last_price 平移解决, 与 N
  无关; 被跳过的 sim 单本来就按入场时点 N 记账, 实单口径与此对齐。

入场模式 (entry_mode):
- close  : 收盘确认, 决策在 bar 收盘产出 (原始口径, 默认)
- trigger: 盘中触发 (经典海龟原生)。on_bar 只做日线评估并武装
  价位 (_armed), 入场/加仓/止损/通道出场全部由 on_price(tick)
  或 check_bar(回测bar触碰仿真) 在价格穿越时触发。
  过滤器与风控在武装时(收盘)预评估/在触发时(风控)复核;
  触发价 = 通道位, 跳空 = 开盘价 (check_bar 仿真口径)。

S1 过滤器口径(复核确认):
- 跟踪该市场上一次 S1 突破(不分方向), 模拟其 2N 止损 / 10 通道出场判定输赢
- 被跳过的信号记为 sim 单 + _fs_armed; sim 止损/出场后过滤器与 fail-safe 一并解除
- trigger 模式下 sim 单由盘中价格推进 (touch 口径), 与实单一致
"""

DEC_ENTER = "enter"
DEC_ADD = "add"
DEC_EXIT = "exit"
DEC_SKIP = "skip"      # S1 过滤器跳过 (引擎已记录 sim 单并武装 fail-safe)
DEC_ARM = "arm"        # trigger 模式: 日线评估完成, 价位已武装 (信息性)


class TurtleEngine:
    def __init__(self, system: str = "S1",
                 stop_atr_mult: float = 2.0,
                 pyramid_layers: int = 4,
                 pyramid_step_atr: float = 0.5,
                 enable_s1_filter: bool = True,
                 entry_mode: str = "close",
                 stop_n_mode: str = "ratchet"):
        assert system in ("S1", "S2")
        assert entry_mode in ("close", "trigger")
        assert stop_n_mode in ("entry", "current", "ratchet")
        self.system = system
        self.stop_mult = stop_atr_mult
        self.max_layers = max(1, pyramid_layers)
        self.step_atr = pyramid_step_atr
        self.entry_mode = entry_mode
        # N 口径 (P0-2):
        #   entry  = 入/加仓时点冻结 N (经典海龟, 与 RiskManager 2N 记账一致)
        #   current= 当根 N (2026-09-14 前旧行为; N 放大时止损被放宽,
        #            实际止损亏损可超记账 2 倍 — 仅用于 A/B 复算与回退)
        #   ratchet= 当根 N 但**只收紧不放宽** (止损单调向有利方向 /
        #            加仓位单调向有利方向): 保留 N 收缩时的动态收紧,
        #            同时保证单层最大亏损不超过首次锚定的 2N
        self.stop_n_mode = stop_n_mode
        # 当前有效止损/加仓位 (ratchet 模式的棘轮棘齿; 其余模式=当次算值)
        self._stop_level = None
        self._add_level = None
        # S1 盈利跳过过滤开关 (P0-1): False 时 S1 退化为裸 20/10 突破。
        # 注意: sim 单推进与该过滤器深度耦合, 关闭后 _record_sim/
        # _advance_sim 自然不会被触发 (无 skip 即无 sim 单)。
        self.enable_s1_filter = bool(enable_s1_filter)

        self.direction = 0                    # 0 平 / +1 多 / -1 空
        self.layers = []                      # [[lots, price], ...]
        # 各层冻结 N (与 layers 等长; None/缺失时回退最近评估 N — 兼容
        # 旧快照 v4 及直测路径)
        self.layer_ns = []
        # S1 过滤器: {direction, entry_price, n, taken, status, result}
        self.s1_last = None
        self._fs_armed = 0                    # fail-safe 武装方向 (S1)

        # === trigger 模式状态 ===
        self._armed = {"long": None, "short": None,
                       "fs_long": None, "fs_short": None,
                       "stop": None, "add": None, "exit": None}
        self._pending = None      # 已发出未确认的触发决策 {"kind","dir"}
        self._n = None            # 最近日线评估的 N (武装/触发决策载荷)
        self._entry_ch = None     # (upper, lower) 入场通道
        self._exit_ch = None      # (upper, lower) 出场通道
        self._fs_ch = None        # (upper, lower) fail-safe 通道

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
        # 最近评估 N: close 模式不经 _arm_daily, 在此刷新 (冻结 N 来源)
        self._n = n

        if self.entry_mode == "trigger":
            # 盘中触发模式: bar 收盘只做日线评估与价位武装,
            # 入场/加仓/止损/出场全部由 on_price/check_bar 触发。
            return self._arm_daily(high, low, n, channels)

        decisions = []
        exited_this_bar = False   # 两段式确认: 本 bar 决策出场后按空仓评估入场

        # === 持仓管理 ===
        # 有方向但层数据为空 = 脏快照/外部干预后的不可管理态: 不做
        # 持仓判定 (避免 IndexError), 由策略层对账/收养重建
        if self.direction != 0 and self.layers:
            stop_price = self._stop_price()
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
                # === 两段式第一阶段: 出场决策产出即结清过滤器记录 ===
                # 必须早于本 bar 的入场评估: 被跳过的入场决策会在评估
                # 阶段改写 s1_last (sim 单), 若等到 confirm_exit 才记录
                # 真实输赢, 结果就永久丢失 (2026-09-14 审计, 两层多仓
                # 均价 115/出场 112 属 loss, 却按"上一笔盈利"跳过)
                self._settle_s1_exit(exit_price)
                decisions.append({
                    "decision": DEC_EXIT, "reason": exit_reason,
                    "price": exit_price})
                exited_this_bar = True
                # 出场后本 bar 继续评估入场 (同 bar 反转)
            else:
                # 加仓: 未触发离场时才评估 (水平触发, 以最后加仓价为锚)
                if len(self.layers) < self.max_layers:
                    add_px = self._next_add_price()
                    if self.direction > 0 and close >= add_px:
                        decisions.append({"decision": DEC_ADD, "price": close})
                    elif self.direction < 0 and close <= add_px:
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
        if self.system == "S2" or not self.enable_s1_filter:
            return [{"decision": DEC_ENTER, "direction": sig_dir,
                     "price": close, "n": n, "failsafe": False}]
        # S1: 过滤判定
        if not self._s1_filter_skip(close):
            return [{"decision": DEC_ENTER, "direction": sig_dir,
                     "price": close, "n": n, "failsafe": False}]
        # 过滤命中 → fail-safe 检查。
        # 口径注释 (两路径刻意不对称, 复核确认):
        # - 此处 (20 边沿与 55 突破同 bar): 用 close 同时高于当根与
        #   前根 55 上轨的"已越过"判定, 容忍隔夜跳空直接越过 55 的场景
        #   —— 跳空 bar 往往不构成严格边沿 (prev_close 已在通道上方),
        #   若按边沿会漏掉强制入场;
        # - _fs_armed 武装路径 (on_bar 内): 必须严格边沿
        #   (prev_close <= prev_fs_upper), 因为被跳过后价格持续在 55
        #   上方运行时, 无边沿约束会导致每 bar 重复触发入场。
        # 两路径互补: 同 bar 跳空走宽松口径, 滞后突破走严格边沿。
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
    def confirm_enter(self, direction: int, lots: int, price: float,
                      n: float = None):
        """确认入场; n = 本层冻结 N (缺省回退最近评估 N)"""
        self.update_n(n)          # 显式 n 同时视为当前市场 N (current/ratchet)
        self.direction = direction
        self.layers = [[lots, price]]
        self.layer_ns = [self._frozen(n)]
        self._reset_levels()      # 新仓 → 重设止损/加仓锚 (2N / 0.5N)
        self._fs_armed = 0
        self._pending = None
        if self.system == "S1":
            # 实盘入场成为新的 s1_last (taken, 持仓中)
            self.s1_last = {
                "direction": direction, "entry_price": price, "n": None,
                "taken": True, "status": "open", "result": None}
        if self.entry_mode == "trigger":
            self._rearm()   # 立即武装止损/加仓/出场 (不等下一交易日)

    def confirm_add(self, lots: int, price: float, n: float = None):
        """确认加仓; n = 本层冻结 N (缺省回退最近评估 N)"""
        self.update_n(n)
        if self.direction != 0:
            self.layers.append([lots, price])
            self.layer_ns.append(self._frozen(n))
            # 加仓重设棘轮锚 (经典口径: 止损移到最后单位的 2N)。
            # 实测 (rb 2018-2023): 重设 32.9% vs 不重设(保留更紧止损)
            # 22.6% — 棘轮只在"两次加仓之间"生效, 加仓时按新单位重锚。
            self._reset_levels()
        self._pending = None
        if self.entry_mode == "trigger":
            self._rearm()

    def confirm_exit(self, price: float):
        if self.direction == 0:
            self._pending = None
            return
        # 实单结果 → S1 过滤器 (按手数加权均价判定输赢)。
        # close 模式已在 on_bar 决策阶段结清 → 此处幂等跳过;
        # trigger 模式无"同 bar 出场+入场"交错, 在此结清;
        # 层数据缺失(脏快照)时跳过滤镜判定, 只做状态复位。
        self._settle_s1_exit(price)
        self.direction = 0
        self.layers = []
        self.layer_ns = []
        self._reset_levels()
        self._fs_armed = 0
        self._pending = None
        if self.entry_mode == "trigger":
            self._rearm()   # 空仓 → 重新武装入场位 (允许新穿越再入场)

    def _settle_s1_exit(self, price: float) -> bool:
        """按出场价结清 S1 过滤器记录 (两段式第一阶段, 幂等)。

        仅对"实单持仓中"(taken 且 status=open) 的记录生效; 已被 sim 单
        或其他状态取代时返回 False, 避免覆盖后到的记录。
        """
        if self.system != "S1" or not self.layers or self.s1_last is None:
            return False
        if not self.s1_last.get("taken") \
                or self.s1_last.get("status") != "open":
            return False
        tot_lots = sum(l for l, _ in self.layers)
        avg_entry = sum(l * p for l, p in self.layers) / tot_lots
        pnl = (price - avg_entry) * self.direction
        self.s1_last["status"] = "closed"
        self.s1_last["result"] = "win" if pnl > 0 else "loss"
        return True

    def adopt_position(self, direction: int, lots: int, price: float,
                       n: float = None) -> int:
        """按外部真值 (券商持仓/运行时对账) 覆盖式重建为单层持仓。

        返回生效层手数 (0 = 视为空仓并复位)。旧版策略层直接赋值
        direction/layers, 会绕过 layer_ns 造成两列表脱节 — 统一走本
        方法。price 必须为正 (框架空头 avgpx 为负, 调用方先取绝对值)。
        """
        lots = int(abs(int(lots)))
        if direction == 0 or lots <= 0 or price is None \
                or price != price or price <= 0:
            self.flatten_book()
            return 0
        self.update_n(n)          # 显式 n 同时视为当前市场 N
        self.direction = 1 if direction > 0 else -1
        self.layers = [[lots, float(price)]]
        self.layer_ns = [self._frozen(n)]
        self._reset_levels()
        self._pending = None
        if self.entry_mode == "trigger":
            self._rearm()   # 恢复即纳入止损/加仓/出场管理 (幂等)
        return lots

    def flatten_book(self):
        """清空持仓状态 (外部全平/收养失败); 仅复位, 不动 S1 过滤器"""
        self.direction = 0
        self.layers = []
        self.layer_ns = []
        self._reset_levels()

    def trim_to_lots(self, target_lots: int) -> int:
        """持仓裁剪至目标手数 (从尾部丢弃未确认层; ghost_fill 自愈)。

        2026-09-17 CF 事故: 执行单元卡死后 34 手加仓从未到达柜台,
        引擎按 99 手管理风险而券商只有 65 手, 风控预算被虚占、止损
        按未持仓手数计算。裁剪仅从**尾部**逐层丢弃 (先入场的底层
        成交确认最充分), 剩余末层的止损/加仓锚按棘轮口径重建。

        仅缩量: target >= 当前手数时不动。target=0 等价 flatten_book。
        不动 S1 过滤器记录 (真实持仓仍在, 输赢判定按剩余层加权)。
        返回实际丢弃的手数。
        """
        cur = sum(l for l, _ in self.layers)
        target = int(abs(int(target_lots)))
        if self.direction == 0 or not self.layers or target >= cur:
            return 0
        if target <= 0:
            dropped = cur
            self.flatten_book()
            if self.entry_mode == "trigger":
                self._rearm()   # 空仓 → 重新武装入场位
            return dropped
        dropped = 0
        while self.layers and cur > target:
            l, _p = self.layers.pop()
            self.layer_ns.pop()
            cur -= l
            dropped += l
        self._reset_levels()        # 末层变化 → 锚重建 (ratchet 重新冻结)
        if self.entry_mode == "trigger":
            self._rearm()
        return dropped

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def stop_price(self, n: float = None) -> float:
        """公开接口: 当前止损价 (最后加仓价 ∓ stop_mult×该层冻结 N)。

        n 参数保留仅为兼容旧调用点, 实际不参与计算 (P0-2 N 冻结口径);
        无持仓时返回 NaN (比较恒 False, 不误触发止损)。
        """
        return self._stop_price()

    def reanchor_last_price(self, price: float):
        """实际成交均价回写: 修正最后一层入场价并重武装。

        confirm 时若执行层尚未回报成交(分批窗口), 记账价 fallback
        为触发价, 与最终成交均价存在滑差; 框架 avgpx 更新后由策略
        层调用本方法修正, stop/add/exit 武装位随 _rearm 自动跟随。
        首层时同步修正 s1_last.entry_price (输赢判定基准)。"""
        if not self.layers:
            return
        self.layers[-1][1] = price
        # layer_ns 不变: 止损 = 修正后成交价 ∓ 2×入场时点 N (风险口径
        # 与成交价一致, 不随市场 N 漂移)
        self._reset_levels()      # 修正为真实成交价 → 锚重建 (同加仓口径)
        if len(self.layers) == 1 and self.s1_last \
                and self.s1_last.get("taken"):
            self.s1_last["entry_price"] = price
        if self.entry_mode == "trigger":
            self._rearm()

    def _cur_n(self) -> float:
        """最近评估 N (on_bar/update_n 刷新); 不可用返回 NaN"""
        n = self._n
        if n is not None and n == n and n > 0:
            return float(n)
        return float("nan")

    def _frozen(self, n: float = None) -> float:
        """解析冻结 N: 显式 n → 最近评估 N → None (无 N 可用)。

        存 None 而非 NaN: 快照 JSON 友好, 且 _layer_n 会回退最近评估 N。
        """
        if n is not None and n == n and n > 0:
            return float(n)
        v = self._cur_n()
        return v if v == v else None

    def _layer_n(self, idx: int = -1) -> float:
        """第 idx 层冻结 N; 缺失(旧快照/外部赋值)回退最近评估 N"""
        try:
            n = self.layer_ns[idx]
        except IndexError:
            n = None
        if n is None or n != n or n <= 0:
            return self._cur_n()
        return float(n)

    def _stop_price(self) -> float:
        """止损价 (见模块头 N 口径)。

        entry   : 最后加仓价 ∓ stop_mult × 该层冻结 N (定死)
        current : 最后加仓价 ∓ stop_mult × 当根 N (可放宽)
        ratchet : 上式取当根 N, 但只允许向有利方向移动 (多头只上移/
                  空头只下移), 故单层最大亏损不超过首次锚定的 2N
        """
        if not self.layers:
            return float("nan")
        n = self._effective_n()
        if n != n:
            return self._ratchet("stop", float("nan"))
        last_price = self.layers[-1][1]
        cand = None
        if self.direction > 0:
            cand = last_price - self.stop_mult * n
        else:
            cand = last_price + self.stop_mult * n
        return self._ratchet("stop", cand)

    def _next_add_price(self) -> float:
        """下一层加仓触发价 (N 口径同 _stop_price)"""
        if not self.layers:
            return float("nan")
        n = self._effective_n()
        if n != n:
            return self._ratchet("add", float("nan"))
        last_price = self.layers[-1][1]
        step = self.step_atr * n
        cand = None
        if self.direction > 0:
            cand = last_price + step
        else:
            cand = last_price - step
        return self._ratchet("add", cand)

    def booked_risk(self, volscale: float, stop_mult: float = None,
                    stop: float = None) -> float:
        """本引擎持仓的最坏潜亏记账值 (供 RiskManager 预算扣减)。

        两套口径:
        - 默认 (stop=None): Σ 各层手数 × 该层冻结 N × volscale ×
          stop_mult (= 2N 口径, 与加仓时点记账一致);
        - **stop=<armed 止损位>**: Σ (层价 - stop) × 方向 × 手数 ×
          volscale, 即"按当前实际止损位"的最坏亏损 — 与棘轮收紧后的
          真实止损一致 (2026-09-16 实盘实测: 2N 口径 52.7 万 vs
          止损口径 20.4 万, 虚高 2.5 倍 → 组合预算被占满, 加仓全被
          `total_risk(cap=0)` 否决)。单层贡献可为负 (止损在成本有利
          一侧 = 锁盈), 组合取净额并下限 0。
        """
        mult = self.stop_mult if stop_mult is None else float(stop_mult)
        if stop is not None and stop == stop and stop > 0 \
                and self.direction != 0:
            sign = 1.0 if self.direction > 0 else -1.0
            worst = 0.0
            for lots, px in self.layers:
                worst += (px - stop) * sign * lots * float(volscale)
            return max(0.0, worst)
        risk = 0.0
        for i, (lots, _px) in enumerate(self.layers):
            n = self._layer_n(i)
            if n == n:
                risk += lots * n * float(volscale) * mult
        return risk

    def stop_anchor(self):
        """当前止损位 (无持仓/无 N → None), 供风控按实际止损记账"""
        if self.direction == 0 or not self.layers:
            return None
        sp = self._stop_price()
        return None if sp != sp else float(sp)

    def _ratchet(self, kind: str, cand: float) -> float:
        """棘轮: 多头止损/加仓位只上移, 空头只下移 (monotone)。

        kind='stop'|'add'; non-ratchet 模式下仅缓存当次算值 (等价直算)。
        """
        attr = "_stop_level" if kind == "stop" else "_add_level"
        prev = getattr(self, attr)
        if self.stop_n_mode != "ratchet":
            setattr(self, attr, cand)
            return cand
        if cand != cand:                       # 本根 N 不可用 → 用上次值
            return prev if prev is not None else cand
        if prev is None or prev != prev:
            setattr(self, attr, cand)
        elif self.direction > 0:
            setattr(self, attr, max(prev, cand))
        else:
            setattr(self, attr, min(prev, cand))
        return getattr(self, attr)

    def _reset_levels(self):
        """重设棘轮锚点 (新层/收养/清仓/成交价修正时调用)"""
        self._stop_level = None
        self._add_level = None

    def _effective_n(self) -> float:
        """止损/加仓步长使用的 N: 冻结口径 (entry) 或当根口径 (current)"""
        if self.stop_n_mode in ("current", "ratchet"):
            return self._cur_n()
        return self._layer_n(-1)

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
    # trigger 模式: 日线武装 + 盘中触发 (纯逻辑, tick/bar 双入口)
    # ------------------------------------------------------------------
    def _arm_daily(self, high: float, low: float, n: float,
                   channels: dict) -> list:
        """日线评估: 刷新 N/通道并武装次日触发价位。

        触发位口径: Donchian 通道(不含当根) 与当根 high/low 取包络,
        即"含当根"的 N 日通道 — 与经典海龟次日挂单突破位一致。
        """
        self._n = n
        self._entry_ch = (max(channels['entry_upper'], high),
                          min(channels['entry_lower'], low))
        self._exit_ch = (max(channels['exit_upper'], high),
                         min(channels['exit_lower'], low))
        self._fs_ch = (max(channels['fs_upper'], high),
                       min(channels['fs_lower'], low))
        self._rearm()
        return [{"decision": DEC_ARM, "plan": dict(self._armed), "n": n}]

    def _rearm(self):
        """按当前状态重算武装价位 (日评估/成交确认/恢复后调用)"""
        a = self._armed
        for k in a:
            a[k] = None
        if self.direction == 0:
            if self._n is None or self._entry_ch is None:
                return
            a["long"], a["short"] = self._entry_ch
            if self._fs_armed > 0:
                a["fs_long"] = self._fs_ch[0]
            elif self._fs_armed < 0:
                a["fs_short"] = self._fs_ch[1]
        elif self.layers:
            # 持仓: 止损/加仓只需层数据 + 冻结 N (收养/对账重建后即使
            # 通道尚未评估也应纳管); 通道出场位缺通道时留空
            # N 取该层冻结值 (P0-2), 不再随当根 N 漂移
            a["stop"] = self._stop_price()
            if len(self.layers) < self.max_layers:
                a["add"] = self._next_add_price()
            if self._exit_ch is not None:
                a["exit"] = self._exit_ch[1] if self.direction > 0 \
                    else self._exit_ch[0]

    def rearm(self):
        """公开接口: 快照恢复后按需重建武装 (幂等)"""
        self._pending = None
        if self._n is not None:
            self._rearm()

    def update_n(self, n: float):
        """刷新"最近评估 N"; 策略层每次算出指标后调用。

        close 模式/intrabar 路径下引擎不经过 on_bar 就可能有持仓
        (对账重建/外部收养) 或 confirm_*, 冻结 N 需要一个新鲜来源;
        不刷新则 _cur_n() 返回 NaN, 止损判定静默失效。
        """
        if n is not None and n == n and n > 0:
            self._n = float(n)
            # legacy 层 N 回填: 旧快照/升级前的层没有 layer_ns, 其止损会
            # 一直回退到"当根 N"(棘轮仍防放宽, 但与"逐层冻结"有偏差)。
            # 首次拿到评估 N 时以近似值回填, 之后即按冻结口径走。
            if self.layers and len(self.layer_ns) == len(self.layers):
                for i, ln in enumerate(self.layer_ns):
                    if ln is None or ln != ln or ln <= 0:
                        self.layer_ns[i] = float(n)
            # 持仓但止损尚未武装 (启动收养/对账重建后的窗口) → 定向补武装;
            # 此时 _rearm 只写 stop/add/exit, 不会恢复被过滤器否决的入场位
            stop = self._armed.get("stop")
            if self.entry_mode == "trigger" and self.direction != 0 \
                    and self.layers and (stop is None or stop != stop):
                self._rearm()

    def disarm_entry(self, sig_dir: int):
        """解除某方向的入场武装 (含 fail-safe): 武装时被过滤器否决的方向"""
        if sig_dir > 0:
            self._armed["long"] = self._armed["fs_long"] = None
        else:
            self._armed["short"] = self._armed["fs_short"] = None

    def entry_levels(self) -> dict:
        """当前入场武装位 {+1: level_or_None, -1: level_or_None}
        (含 fail-safe; 双位并存时取更近者 — 同向主/备同时武装的语义)"""
        a = self._armed
        out = {1: None, -1: None}
        for d, keys in ((1, ("long", "fs_long")), (-1, ("short", "fs_short"))):
            lv = [a[k] for k in keys if a[k] is not None]
            if lv:
                out[d] = min(lv) if d > 0 else max(lv)
        return out

    @property
    def armed(self) -> dict:
        """当前武装价位快照 (只读用途)"""
        return dict(self._armed)

    def on_price(self, px: float) -> list:
        """实盘 tick 驱动: 价格穿越武装位即触发 (边沿语义由武装位本身保证:
        触发后该位即撤除/重武装, pending 期间静默)"""
        if self.entry_mode != "trigger" or self._n is None:
            return []
        self._advance_sim_price(px)
        if self._pending is not None:
            return []
        a = self._armed
        d = self.direction
        if d != 0:
            if a["stop"] is not None and \
                    (px <= a["stop"] if d > 0 else px >= a["stop"]):
                return self._emit_exit("stop", a["stop"])
            if a["exit"] is not None and \
                    (px <= a["exit"] if d > 0 else px >= a["exit"]):
                return self._emit_exit("channel", a["exit"])
            if a["add"] is not None and len(self.layers) < self.max_layers \
                    and (px >= a["add"] if d > 0 else px <= a["add"]):
                self._pending = {"kind": "add", "dir": d}
                return [{"decision": DEC_ADD, "price": a["add"],
                         "n": self._n}]
            return []
        if a["long"] is not None and px >= a["long"]:
            return self._trigger_entry(1, px)
        if a["short"] is not None and px <= a["short"]:
            return self._trigger_entry(-1, px)
        if a["fs_long"] is not None and px >= a["fs_long"]:
            return self._trigger_fs(1, px)
        if a["fs_short"] is not None and px <= a["fs_short"]:
            return self._trigger_fs(-1, px)
        return []

    def check_bar(self, o: float, h: float, l: float, c: float) -> list:
        """回测 bar 触碰仿真 (trigger 模式专用):
        跳空过位按开盘价成交, 否则按武装位成交;
        持仓时按 止损→通道出场→加仓 顺序, 每次调用至多一个决策;
        同 bar 出场后不再评估入场 (路径未知, 防乐观偏差)。
        """
        if self.entry_mode != "trigger" or self._n is None:
            return []
        self._advance_sim_bar(o, h, l)
        if self._pending is not None:
            return []
        a = self._armed
        d = self.direction
        if d != 0:
            if a["stop"] is not None:
                if d > 0:
                    if o <= a["stop"]:
                        return self._emit_exit("stop", o)
                    if l <= a["stop"]:
                        return self._emit_exit("stop", a["stop"])
                else:
                    if o >= a["stop"]:
                        return self._emit_exit("stop", o)
                    if h >= a["stop"]:
                        return self._emit_exit("stop", a["stop"])
            if a["exit"] is not None:
                if d > 0:
                    if o <= a["exit"]:
                        return self._emit_exit("channel", o)
                    if l <= a["exit"]:
                        return self._emit_exit("channel", a["exit"])
                else:
                    if o >= a["exit"]:
                        return self._emit_exit("channel", o)
                    if h >= a["exit"]:
                        return self._emit_exit("channel", a["exit"])
            if a["add"] is not None and len(self.layers) < self.max_layers:
                if d > 0:
                    if o >= a["add"]:
                        return self._emit_add(o)
                    if h >= a["add"]:
                        return self._emit_add(a["add"])
                else:
                    if o <= a["add"]:
                        return self._emit_add(o)
                    if l <= a["add"]:
                        return self._emit_add(a["add"])
            return []
        if a["long"] is not None:
            if o >= a["long"]:
                return self._trigger_entry(1, o)
            if h >= a["long"]:
                return self._trigger_entry(1, a["long"])
        if a["short"] is not None:
            if o <= a["short"]:
                return self._trigger_entry(-1, o)
            if l <= a["short"]:
                return self._trigger_entry(-1, a["short"])
        if a["fs_long"] is not None:
            if o >= a["fs_long"]:
                return self._trigger_fs(1, o)
            if h >= a["fs_long"]:
                return self._trigger_fs(1, a["fs_long"])
        if a["fs_short"] is not None:
            if o <= a["fs_short"]:
                return self._trigger_fs(-1, o)
            if l <= a["fs_short"]:
                return self._trigger_fs(-1, a["fs_short"])
        return []

    def _emit_exit(self, reason: str, price: float) -> list:
        self._pending = {"kind": "exit", "dir": self.direction}
        return [{"decision": DEC_EXIT, "reason": reason, "price": price}]

    def _emit_add(self, price: float) -> list:
        self._pending = {"kind": "add", "dir": self.direction}
        return [{"decision": DEC_ADD, "price": price, "n": self._n}]

    def _trigger_entry(self, sig_dir: int, px: float) -> list:
        """入场通道触发: S1 过滤 / S2 直通; skip 则记 sim 并武装 fail-safe。
        只撤同向入场位 (反向保持武装, 否决后可独立触发)"""
        if self.system == "S2" or not self.enable_s1_filter \
                or not self._s1_filter_skip(px):
            self._pending = {"kind": "enter", "dir": sig_dir}
            if sig_dir > 0:
                self._armed["long"] = None
            else:
                self._armed["short"] = None
            return [{"decision": DEC_ENTER, "direction": sig_dir,
                     "price": px, "n": self._n, "failsafe": False}]
        self._record_sim(sig_dir, px, self._n)
        if sig_dir > 0:
            self._armed["long"] = None
            self._armed["fs_long"] = self._fs_ch[0]
        else:
            self._armed["short"] = None
            self._armed["fs_short"] = self._fs_ch[1]
        return [{"decision": DEC_SKIP, "direction": sig_dir,
                 "reason": "s1_last_winner", "price": px}]

    def _trigger_fs(self, sig_dir: int, px: float) -> list:
        self._pending = {"kind": "enter", "dir": sig_dir}
        if sig_dir > 0:
            self._armed["fs_long"] = None
        else:
            self._armed["fs_short"] = None
        return [{"decision": DEC_ENTER, "direction": sig_dir,
                 "price": px, "n": self._n, "failsafe": True}]

    def clear_pending(self):
        """清除孤儿 pending (不解武装): 进程重启/跨日评估时, 挂起的
        触发决策其成交状态已不可知 (决策可能在策略层丢失, 从未下单)。
        与 reject_pending 不同: 保留武装位, 价格仍在位上方时下一
        tick/bar 立即重新触发 (重启补票语义); 若决策实际已成交而
        confirm 前崩溃, 持仓对账按券商实际持仓重建层数兜底。"""
        p = self._pending
        self._pending = None
        return p

    def reject_pending(self):
        """策略层否决已发出的触发决策 (过滤/风控): 清挂起并
        按方向解除当日武装 (防 tick 级重复触发刷屏)"""
        p = self._pending
        self._pending = None
        if p is None:
            return
        if p["kind"] == "enter":
            if p["dir"] > 0:
                self._armed["long"] = self._armed["fs_long"] = None
            else:
                self._armed["short"] = self._armed["fs_short"] = None
        elif p["kind"] == "add":
            self._armed["add"] = None

    def _advance_sim_price(self, px: float):
        """trigger 模式 sim 单盘中推进 (touch 口径, 与实单一致)"""
        last = self.s1_last
        if last is None or last.get("taken") or last["status"] != "open":
            return
        n0 = last["n"]
        d = last["direction"]
        e = last["entry_price"]
        closed = False
        if d > 0:
            if px <= e - self.stop_mult * n0:
                last["status"], last["result"] = "closed", "loss"
                closed = True
            elif self._exit_ch and px <= self._exit_ch[1]:
                last["status"] = "closed"
                last["result"] = "win" if self._exit_ch[1] > e else "loss"
                closed = True
        else:
            if px >= e + self.stop_mult * n0:
                last["status"], last["result"] = "closed", "loss"
                closed = True
            elif self._exit_ch and px >= self._exit_ch[0]:
                last["status"] = "closed"
                last["result"] = "win" if self._exit_ch[0] < e else "loss"
                closed = True
        if closed:
            self._fs_armed = 0
            self._armed["fs_long"] = self._armed["fs_short"] = None

    def _advance_sim_bar(self, o: float, h: float, l: float):
        """check_bar 版 sim 推进: 止损优先 (悲观), 再通道出场"""
        last = self.s1_last
        if last is None or last.get("taken") or last["status"] != "open":
            return
        n0 = last["n"]
        d = last["direction"]
        e = last["entry_price"]
        closed = False
        if d > 0:
            if l <= e - self.stop_mult * n0:
                last["status"], last["result"] = "closed", "loss"
                closed = True
            elif self._exit_ch and l <= self._exit_ch[1]:
                last["status"] = "closed"
                last["result"] = "win" if self._exit_ch[1] > e else "loss"
                closed = True
        else:
            if h >= e + self.stop_mult * n0:
                last["status"], last["result"] = "closed", "loss"
                closed = True
            elif self._exit_ch and h >= self._exit_ch[0]:
                last["status"] = "closed"
                last["result"] = "win" if self._exit_ch[0] < e else "loss"
                closed = True
        if closed:
            self._fs_armed = 0
            self._armed["fs_long"] = self._armed["fs_short"] = None


    # ------------------------------------------------------------------
    # 持久化 / 换月
    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "system": self.system,
            "direction": self.direction,
            "layers": [list(x) for x in self.layers],
            "layer_ns": list(self.layer_ns),   # v4+: 各层冻结 N
            "stop_level": self._stop_level,    # v4+: 棘轮止损/加仓位
            "add_level": self._add_level,
            "s1_last": self.s1_last,
            "fs_armed": self._fs_armed,
            # trigger 模式武装状态 (v4): 崩溃重启后立即恢复触发能力
            "armed": dict(self._armed),
            "pending": self._pending,
            "n": self._n,
            "entry_ch": self._entry_ch,
            "exit_ch": self._exit_ch,
            "fs_ch": self._fs_ch,
        }

    def restore(self, state: dict):
        self.direction = state.get("direction", 0)
        self.layers = [list(x) for x in state.get("layers", [])]
        # 旧快照无 layer_ns → 补 None (由 _layer_n 回退最近评估 N, 行为
        # 与升级前一致); 长度脱节时同样按层对齐
        ns = list(state.get("layer_ns") or [])
        if len(ns) != len(self.layers):
            ns = (ns + [None] * len(self.layers))[:len(self.layers)]
        self.layer_ns = ns
        # 棘轮位 (旧快照无 → None, 下次计算重建)
        self._stop_level = state.get("stop_level")
        self._add_level = state.get("add_level")
        self.s1_last = state.get("s1_last")
        self._fs_armed = state.get("fs_armed", 0)
        # trigger 武装状态 (旧版快照无此键 → None, 下次日评估重建)
        armed = state.get("armed")
        if armed:
            for k in self._armed:
                self._armed[k] = armed.get(k)
        self._pending = state.get("pending")
        self._n = state.get("n")
        self._entry_ch = state.get("entry_ch")
        self._exit_ch = state.get("exit_ch")
        self._fs_ch = state.get("fs_ch")

    def shift_prices(self, delta: float):
        """HOT 换月 re-anchor: 各层价格、武装价位、通道与 sim 价平移。

        只平移价格, 不动 layer_ns (换月不改变各层入场时点 N)。"""
        if delta == 0:
            return
        for layer in self.layers:
            layer[1] += delta
        if self.s1_last and self.s1_last.get("entry_price") is not None:
            self.s1_last["entry_price"] += delta
        for k in self._armed:
            if self._armed[k] is not None:
                self._armed[k] += delta
        for attr in ("_entry_ch", "_exit_ch", "_fs_ch"):
            ch = getattr(self, attr)
            if ch:
                setattr(self, attr, (ch[0] + delta, ch[1] + delta))
        for attr in ("_stop_level", "_add_level"):
            v = getattr(self, attr)
            if v is not None and v == v:
                setattr(self, attr, v + delta)
