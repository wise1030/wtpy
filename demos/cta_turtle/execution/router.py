"""执行路由层 (A1-1, 2026-10-22): 从 StraTurtle 迁出的门控/执行链路。

ExecutionRouter 持有策略回指 (self.s), 方法体与迁出前逐字等价
(self.X → self.h.X 机械重写); StraTurtle 保留同名薄转发, 测试与
调用方接口零变化。黄金回测 (research/golden_bt.py) 对该迁移做
端到端哈希级回归。

后续 (A1 后续步): 逐步把 self.s 回指换成显式依赖注入
(risk/sizer/filters/emitter), 当前先完成"代码归位"。
"""
from core.TurtleEngine import (DEC_ENTER, DEC_ADD, DEC_EXIT, DEC_ARM,
                           DEC_SKIP)
from filters import FilterContext


class ExecutionRouter:
    """入场/加仓门控链 + 单/双系统执行路由 + 武装时过滤裁决"""

    # 显式依赖声明 (A1 尾项): 模块全部读写依赖白名单; 访问未声明名
    # 立即抛 AttributeError (隐式耦合 loud-failure, 替代静默降级)
    DEPS = (
        "_account_available",
        "_base",
        "_chain",
        "_combined_target",
        "_daily_blocked",
        "_engine",
        "_ghost_blocked",
        "_inst",
        "_limit_lock",
        "_log_account_gate",
        "_mark_fill_pending",
        "_risk",
        "_safe_mark",
        "_sizer",
        "_sub_lots",
        "_sub_share",
        "_subs",
        "_sync_risk_book",
        "_veto_log",
        "_volscale",
        "name",
    )

    def __init__(self, host):
        self.h = host   # StrategyHost 显式依赖面 (A1 尾项, 见 host.py)     # StraTurtle 回指 (过渡期; 逐步收敛为显式依赖)

    def on_arm(self, context, engine, fctx):
        """武装落地: 过滤链对入场方向逐一裁决, 否决方向当日解除武装"""
        for d, lv in engine.entry_levels().items():
            if lv is None:
                continue
            dec = {"price": lv, "direction": d}
            if not self.run_filter_chain(context, d, dec, fctx):
                engine.disarm_entry(d)
    def size_and_cap(self, context, kind: str, n: float,
                      price: float, tag: str):
        """入场/加仓共用的 定手数+风控闸门 段 (P1-8: 合并原 ENTER/ADD
        两处近重复门控链, 否决日志 key/文案口径保持不变)。

        kind = "enter"|"add"; tag = enter 时方向 d, add 时当前层数
        (用于否决 key 与日志, 与原实现逐字一致)。
        返回 (final_lots, reasons); final_lots<=0 时已打印否决日志并
        reject_pending, 调用方直接返回。
        """
        if self.h._ghost_blocked(context):
            self.h._veto_log(context, f"ghost{tag}",
                           f"veto {kind} @{price:.2f}: 本日 ghost trim "
                           f"后禁止再触发 (订单链路易再次幽灵)")
            self.reject_trigger(self.h._engine)
            return 0, []
        if self.h._daily_blocked:
            self.h._veto_log(context, f"daily{tag}",
                           f"veto {kind} @{price:.2f}: 日损闸已触发 "
                           f"(当日回撤 <= -"
                           f"{self.h._risk.cfg.daily_loss_stop_frac:.2%})")
            self.reject_trigger(self.h._engine)
            return 0, []
        lots = self.h._sizer.layer_lots(n, self.h._volscale, self.h._inst.units)
        if lots <= 0:
            if kind == "enter":
                msg = (f"veto enter {tag}@{price:.2f}: "
                       f"lots=0 (N={n:.2f}×volscale={self.h._volscale} "
                       f"> budget)")
                key = f"lot{tag}"
            else:
                msg = f"veto add @{price:.2f}: lots=0"
                key = f"add{tag}"
            self.h._veto_log(context, key, msg)
            self.reject_trigger(self.h._engine)
            return 0, []
        code = self.h._inst.code
        equity = self.h._risk.equity(context, sid=self.h.name())
        final_lots, reasons = self.h._risk.cap_entry(
            code, lots=lots, n=n, volscale=self.h._volscale,
            price=price, stop_mult=self.h._base.stop_atr_mult,
            equity=equity, group=self.h._inst.group,
            avail=self.h._account_available(), sid=self.h.name())
        rkey = f"risk{tag}" if kind == "enter" else f"addrisk{tag}"
        self.h._log_account_gate(context, rkey, reasons, lots, final_lots)
        if final_lots <= 0:
            rpl = n * self.h._volscale * self.h._base.stop_atr_mult
            self.h._veto_log(
                context, rkey,
                f"veto {kind} @{price:.2f}: risk "
                f"({'/'.join(reasons)}), equity={equity:.0f}, "
                f"want={lots}, rpl={rpl:.0f}, vs={self.h._volscale}")
            self.reject_trigger(self.h._engine)
            return 0, reasons
        if final_lots < lots:
            skey = f"rscale{tag}" if kind == "enter" else f"addscale{tag}"
            if kind == "enter":
                msg = (f"risk scaled {lots}->{final_lots} lots "
                       f"({'/'.join(reasons)}), equity={equity:.0f}")
            else:
                msg = (f"risk scaled add {lots}->{final_lots} "
                       f"({'/'.join(reasons)})")
            self.h._veto_log(context, skey, msg)
        return final_lots, reasons
    def run_filter_chain(self, context, sig_dir: int,
                          dec: dict, fctx: FilterContext = None) -> bool:
        """信号过滤链: 任一过滤器否决即拒绝 (不触碰引擎状态)"""
        for flt in self.h._chain:
            try:
                if fctx is not None:
                    ok, why = flt.allow_entry(fctx.with_direction(sig_dir))
                else:   # 直测路径 (_execute 无 bar 上下文)
                    ok, why = flt.allow_entry(FilterContext(sig_dir=sig_dir))
            except Exception as e:
                # 异常策略由过滤器自身声明 (2026-10-20): 辅助过滤器默认
                # allow 保持旧行为; 风险类过滤器可配 on_error=deny, 避免
                # "过滤器自身 bug 反而放行"。
                policy = getattr(flt, "on_error", "allow")
                context.stra_log_text(
                    f"{self.h.name()} filter {flt.name} error "
                    f"({policy}): {e}", level=2)
                if policy == "deny":
                    self.h._veto_log(
                        context, f"f_{flt.name}_error",
                        f"veto enter {'LONG' if sig_dir > 0 else 'SHORT'}@"
                        f"{dec['price']:.2f}: filter={flt.name} "
                        f"error({e}) -> deny")
                    return False
                continue
            if not ok:
                self.h._veto_log(
                    context, f"f_{flt.name}_{sig_dir}",
                    f"veto enter {'LONG' if sig_dir > 0 else 'SHORT'}@"
                    f"{dec['price']:.2f}: filter={flt.name} ({why})")
                return False
        return True
    def reject_trigger(self, engine):
        """trigger 模式: 策略层否决已触发决策 → 引擎解除当日武装"""
        if engine.entry_mode == "trigger":
            engine.reject_pending()
    def execute(self, context, dec: dict, n: float,
                 fctx: FilterContext = None):
        code = self.h._inst.code
        kind = dec["decision"]

        if kind == DEC_ARM:
            # trigger 模式: 武装时过滤链前置裁决入场方向
            self.on_arm(context, self.h._engine, fctx)
            a = self.h._engine.armed
            context.stra_log_text(
                f"{self.h.name()} armed L={a['long']} S={a['short']} "
                f"stop={a['stop']} add={a['add']} exit={a['exit']}")
            return

        if kind == DEC_SKIP:
            context.stra_log_text(
                f"{self.h.name()} S1 filter skip {dec['direction']}@"
                f"{dec['price']:.2f} (last winner), fail-safe armed")
            return

        if kind == DEC_EXIT:
            old_pos = context.stra_get_position(code)
            context.stra_set_position(code, 0, "Exit")
            self.h._engine.confirm_exit(dec["price"])
            self.h._risk.on_closed(code)
            self.h._safe_mark(context, dec["price"], "exit", self.h.name())
            context.stra_log_text(
                f"{self.h.name()} exit {old_pos}@{dec['price']:.2f} "
                f"reason={dec['reason']}")
            return

        if kind == DEC_ENTER:
            d = dec["direction"]
            # === 过滤链 (隐式方向闸 + 配置过滤器): 否决不提交引擎 ===
            # trigger 模式触发决策已在武装时裁决 (prefiltered), 跳过
            if not dec.get("prefiltered") and \
                    not self.run_filter_chain(context, d, dec, fctx):
                self.reject_trigger(self.h._engine)
                return
            final_lots, _reasons = self.size_and_cap(
                context, "enter", n, dec["price"], str(d))
            if final_lots <= 0:
                return
            context.stra_set_position(code, d * final_lots, "Enter")
            # 记账价 = 决策价 (触发位/收盘价/tick 价)。
            # 不再从框架 avgpx 反推: stra_get_position/avgpx 是**信号账面**
            # (set_position 即变, 真实成交在下一次 tick 落地, 见
            # core/LiveTruth 模块头), confirm 时反推得到的是"新信号仓
            # × 旧成交均价"的混合值 (加仓时甚至等于加仓前的旧均价)。
            # 真实成交价由 _reanchor_actual_fill 回写: 逐笔成交优先,
            # 净均价兜底。
            fill = float(dec["price"])
            # n: 本层冻结 N (P0-2) — 止损/加仓不再随当根 N 漂移
            self.h._engine.confirm_enter(d, final_lots, fill, n)
            self.h._mark_fill_pending(context, self.h._subs[0]["name"],
                                    self.h._engine)
            # 风控账本按**当前实际止损位**记账 (P1 口径修复): 2N 口径
            # 会虚高数倍并占满组合预算 → 加仓全被否决 (2026-09-16 实测)
            self.h._sync_risk_book(n)
            self.h._safe_mark(context, dec["price"],
                                   "buy" if d > 0 else "sell", self.h.name())
            fs = " [FAIL-SAFE]" if dec.get("failsafe") else ""
            context.stra_log_text(
                f"{self.h.name()} enter {'LONG' if d > 0 else 'SHORT'} "
                f"{final_lots} lots @~{fill:.2f}, N={n:.2f}, VaR/layer="
                f"{n * self.h._volscale * final_lots:.0f}{fs}")
            return

        if kind == DEC_ADD:
            if self.h._limit_lock.get(code):
                # 涨跌停锁死疑似: 暂停加仓 (出场/止损不受影响), 告警
                self.h._veto_log(context, "limitlock",
                               f"veto add @{dec['price']:.2f}: "
                               f"limit-locked (price pinned "
                               f">{self.h._base.limit_lock_secs}s)")
                self.reject_trigger(self.h._engine)
                return
            d = self.h._engine.direction
            final_lots, _reasons = self.size_and_cap(
                context, "add", n, dec["price"],
                str(len(self.h._engine.layers)))
            if final_lots <= 0:
                return
            old_pos = context.stra_get_position(code)
            context.stra_set_position(code, old_pos + d * final_lots, "Add")
            fill = float(dec["price"])   # 同入场: 记账价=决策价, 待回写修正
            self.h._engine.confirm_add(final_lots, fill, n)
            self.h._mark_fill_pending(context, self.h._subs[0]["name"],
                                    self.h._engine)
            self.h._sync_risk_book(n)
            self.h._safe_mark(context, dec["price"],
                                   "buy" if d > 0 else "sell", self.h.name())
            context.stra_log_text(
                f"{self.h.name()} add layer{len(self.h._engine.layers)} "
                f"{final_lots} lots @~{fill:.2f}, N={n:.2f}")
            return
    def execute_dual(self, context, dec_map: dict, n: float,
                      fctx: FilterContext = None):
        """逐子系统消费决策 (只改引擎态), 批次结束统一落一次净仓单。

        成交价口径: 引擎按决策价(bar close)确认 — 回测撮合价即当根
        收盘, 实盘经执行单元的偏差由运行时对账兜底。
        """
        code = self.h._inst.code
        for sub in self.h._subs:
            for dec in dec_map.get(sub["name"], []):
                kind = dec["decision"]
                e = sub["engine"]
                tag = f"[{sub['name']}]"
                if kind == DEC_ARM:
                    self.on_arm(context, e, fctx)
                    continue
                if kind == DEC_SKIP:
                    context.stra_log_text(
                        f"{self.h.name()}{tag} S1 filter skip "
                        f"{dec['direction']}@{dec['price']:.2f} "
                        f"(last winner), fail-safe armed")
                    continue
                if kind == DEC_EXIT:
                    old = self.h._sub_lots(sub) * \
                        (1 if e.direction > 0 else -1)
                    e.confirm_exit(dec["price"])
                    self.h._safe_mark(context, dec["price"], "exit",
                                           self.h.name())
                    context.stra_log_text(
                        f"{self.h.name()}{tag} exit {old}"
                        f"@{dec['price']:.2f} reason={dec['reason']}")
                    continue
                if kind == DEC_ENTER:
                    d = dec["direction"]
                    lots = self.apply_enter_gates(context, sub, d,
                                                   dec, n, fctx)
                    if lots <= 0:
                        self.reject_trigger(e)
                        continue
                    e.confirm_enter(d, lots, dec["price"], n)
                    self.h._mark_fill_pending(context, sub["name"], e)
                    self.h._safe_mark(context,
                                    dec["price"],
                                    "buy" if d > 0 else "sell",
                                    self.h.name())
                    fs = " [FAIL-SAFE]" if dec.get("failsafe") else ""
                    context.stra_log_text(
                        f"{self.h.name()}{tag} enter "
                        f"{'LONG' if d > 0 else 'SHORT'} {lots} "
                        f"@~{dec['price']:.2f}, N={n:.2f}{fs}")
                    continue
                if kind == DEC_ADD:
                    lots = self.apply_add_gates(context, sub, dec, n)
                    if lots <= 0:
                        self.reject_trigger(e)
                        continue
                    e.confirm_add(lots, dec["price"], n)
                    self.h._mark_fill_pending(context, sub["name"], e)
                    dmark = "buy" if e.direction > 0 else "sell"
                    self.h._safe_mark(context, dec["price"], dmark,
                                           self.h.name())
                    context.stra_log_text(
                        f"{self.h.name()}{tag} add "
                        f"layer{len(e.layers)} {lots} "
                        f"@~{dec['price']:.2f}, N={n:.2f}")

        new_t = self.h._combined_target()
        cur = int(round(context.stra_get_position(code)))
        if new_t != cur:
            context.stra_set_position(code, new_t, "Dual")
        self.h._sync_risk_book(n)
    def apply_enter_gates(self, context, sub: dict, d: int,
                           dec: dict, n: float,
                           fctx: FilterContext = None) -> int:
        """入场三道闸: 过滤链 → VaR 手数 → 组合风控。返回最终手数(0=否决)"""
        code = self.h._inst.code
        tag = f"[{sub['name']}]"
        if self.h._ghost_blocked(context):
            self.h._veto_log(context, f"ghost{d}",
                           f"{tag} veto enter {d}@{dec['price']:.2f}: "
                           f"本日 ghost trim 后禁止再触发")
            self.reject_trigger(sub["engine"])
            return 0
        if self.h._daily_blocked:
            self.h._veto_log(context, f"daily{d}",
                           f"{tag} veto enter {d}@{dec['price']:.2f}: "
                           f"日损闸已触发 (只减不加)")
            self.reject_trigger(sub["engine"])
            return 0
        if not dec.get("prefiltered") and \
                not self.run_filter_chain(context, d, dec, fctx):
            return 0
        lots = self.h._sizer.layer_lots(n, self.h._volscale, sub["units"])
        if lots <= 0:
            self.h._veto_log(context, f"lot{d}",
                           f"{tag} veto enter {d}@{dec['price']:.2f}: "
                           f"lots=0 (units={sub['units']}, "
                           f"N={n:.2f}×volscale={self.h._volscale})")
            return 0
        equity = self.h._risk.equity(context, sid=self.h.name())
        final_lots, reasons = self.h._risk.cap_entry(
            code, lots=lots, n=n, volscale=self.h._volscale,
            price=dec["price"], stop_mult=self.h._base.stop_atr_mult,
            equity=equity, group=self.h._inst.group,
            share=self.h._sub_share(sub), avail=self.h._account_available(),
            sid=self.h.name())
        self.h._log_account_gate(context, f"{tag}enter", reasons, lots,
                               final_lots)
        if final_lots <= 0:
            self.h._veto_log(
                context, f"risk{d}",
                f"{tag} veto enter {d}@{dec['price']:.2f}: risk "
                f"({'/'.join(reasons)}), equity={equity:.0f}")
            return 0
        if final_lots < lots:
            self.h._veto_log(
                context, f"rscale{d}",
                f"{tag} risk scaled {lots}->{final_lots} lots "
                f"({'/'.join(reasons)}), equity={equity:.0f}")
        return final_lots
    def apply_add_gates(self, context, sub: dict,
                         dec: dict, n: float) -> int:
        e = sub["engine"]
        tag = f"[{sub['name']}]"
        if self.h._ghost_blocked(context):
            self.h._veto_log(context, f"ghost{len(e.layers)}",
                           f"{tag} veto add @{dec['price']:.2f}: 本日 "
                           f"ghost trim 后禁止再触发")
            self.reject_trigger(e)
            return 0
        if self.h._daily_blocked:
            self.h._veto_log(context, f"daily{len(e.layers)}",
                           f"{tag} veto add @{dec['price']:.2f}: "
                           f"日损闸已触发 (只减不加)")
            self.reject_trigger(e)
            return 0
        lots = self.h._sizer.layer_lots(n, self.h._volscale, sub["units"])
        if lots <= 0:
            self.h._veto_log(context, f"add{len(e.layers)}",
                           f"{tag} veto add @{dec['price']:.2f}: lots=0")
            return 0
        equity = self.h._risk.equity(context, sid=self.h.name())
        final_lots, reasons = self.h._risk.cap_entry(
            self.h._inst.code, lots=lots, n=n, volscale=self.h._volscale,
            price=dec["price"], stop_mult=self.h._base.stop_atr_mult,
            equity=equity, group=self.h._inst.group,
            share=self.h._sub_share(sub), avail=self.h._account_available(),
            sid=self.h.name())
        self.h._log_account_gate(context, f"{tag}add{len(e.layers)}",
                               reasons, lots, final_lots)
        if final_lots <= 0:
            self.h._veto_log(
                context, f"addrisk{len(e.layers)}",
                f"{tag} veto add @{dec['price']:.2f}: risk "
                f"({'/'.join(reasons)})")
            return 0
        return final_lots
