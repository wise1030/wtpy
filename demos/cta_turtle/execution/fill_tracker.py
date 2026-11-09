"""成交确认/回写/幽灵自愈层 (A1-3, 2026-10-22): 从 StraTurtle 迁出。

FillTracker 持有策略回指 (self.s), 方法体与迁出前逐字等价
(self.X → self.h.X 机械重写; _fill_pending 状态仍在策略实例上)。
黄金回测端到端回归覆盖。
"""
import datetime as _dt
import os
import time
from execution.paths import GROUP_DIR as _GROUP_DIR
from execution.batch import TargetIntent


class FillTracker:
    """分批成交确认 (真值>逐笔>fees 证据链) / 均价回写 / 幽灵 trim /
    执行单元自愈"""

    # 显式依赖声明 (A1 尾项): 模块全部读写依赖白名单; 访问未声明名
    # 立即抛 AttributeError (隐式耦合 loud-failure, 替代静默降级)
    DEPS = (
        "_alert",
        "_attr",
        "_attr_feed",
        "_attr_progress",
        "_base",
        "_combined_target",
        "_exit_orders",
        "_fill_pending",
        "_ghost_trim_date",
        "_inst",
        "_is_dual",
        "_last_n",
        "_persist",
        "_pricetick",
        "_replay_deferred_decisions",
        "_risk",
        "_subs",
        "_sync_risk_book",
        "_truth_mode",
        "_trade_journal_source",
        "_truth_net",
        "_truth_source",
        "_truth_stale_now",
        "name",
    )

    def __init__(self, host):
        self.h = host   # StrategyHost 显式依赖面 (A1 尾项, 见 host.py)     # StraTurtle 回指 (过渡期)

    @staticmethod
    def _journal_cursor(journal):
        """业务游标：带文件身份；旧 journal 桩只实现 cursor() 时兼容。"""
        if journal is None:
            return None
        getter = getattr(journal, "cursor_key", None)
        return getter() if getter is not None else journal.cursor()

    def journal_cursor(self, journal):
        """公开给 outbox：同一业务游标口径。"""
        return self._journal_cursor(journal)

    def mark_fill_pending(self, context, key: str, engine):
        """登记"成交均价待回写" (按子系统分槽, 幂等覆盖同槽)。

        同时记录逐笔成交游标 + 本层手数: 回写时优先从 trades.csv 取
        **该层真实成交均价** (P2), 避免"多笔待回写时只有净均价方程 →
        欠定"的问题。
        """
        j = self.h._trade_journal_source()
        cursor = self._journal_cursor(j)
        # 成交确认的真值基线 (增量口径): 登记时刻的账户持仓, 之后只看
        # **增量**是否等于本层手数 — 绝对值在共享账户下含其他策略仓位,
        # 不可用 (2026-09-15 实测: CZCE.TA 账户 112 vs 本策略 64)
        truth0 = self.h._truth_net(self.h._inst.code)
        # 同槽覆盖时结转前一笔手数: 同一成交窗口内先确认的手数必须在
        # 逐笔成交里先被消费, 否则末层会取到首笔的成交价 (P2)
        prev = self.h._fill_pending.get(key)
        carry = 0.0
        if prev is not None and prev.get("cursor") == cursor:
            carry = float(prev.get("skip") or 0.0) + \
                float(prev.get("lots") or 0.0)
            if truth0 is None:
                truth0 = prev.get("truth0")
        self.h._fill_pending[key] = {
            "engine": engine, "ts": time.time(),
            "fees0": self.get_fees(context),
            "lots": engine.layers[-1][0] if engine.layers else 0,
            "cursor": cursor, "skip": carry, "truth0": truth0}
    def begin_dual_exit(self, context, intent: TargetIntent, engines: list):
        """登记多 profile 共享净出场批次，不按 profile 分别假确认。"""
        journal = self.h._trade_journal_source()
        self.h._exit_orders["__dual__"] = {
            "batch": True, "intent": intent, "engines": list(engines),
            "ts": time.time(),
            "cursor": self._journal_cursor(journal),
            "filled": 0.0,
        }
        # 订单级归因: mixed/反向批次由 localid 链消解, 不再一律 UNKNOWN。
        if self.h._attr_feed(context):
            self.h._attr.on_intent("__dual__", before=int(intent.before),
                                   after=int(intent.after),
                                   ts=float(self.h._exit_orders["__dual__"]["ts"]))
        self.h._persist(context)
        return True

    def cancel_dual_exit(self, context):
        fp = self.h._exit_orders.pop("__dual__", None)
        if fp:
            for engine in fp["engines"]:
                engine.cancel_exit()
            if self.h._attr is not None:
                self.h._attr.attr.discard("__dual__")
            self.h._persist(context)

    def _finish_dual_exit(self, context, source: str, vwap=None):
        fp = self.h._exit_orders.get("__dual__")
        if not fp:
            return
        for engine in fp["engines"]:
            price = vwap if vwap is not None \
                else engine._exit_pending["price"]
            engine.confirm_exit(price)
        self.h._sync_risk_book(self.h._last_n)
        self.h._exit_orders.pop("__dual__", None)
        if self.h._attr is not None:
            self.h._attr.attr.complete("__dual__")
        context.stra_log_text(
            f"{self.h.name()} dual exit confirmed evidence={source}; "
            "all batch engines settled")
        self.h._persist(context)
        # 批次确认是"下一事件"：此时重放此前因混合净目标延后的
        # enter/add。先弹出当前批次，避免重放队列状态仍指向旧批次。
        self.h._replay_deferred_decisions(context)

    def _process_dual_exit(self, context, fp: dict):
        intent = fp["intent"]
        code = intent.code
        # 订单级归因证据优先: localid 链全成 (含跨零 mixed, 其拆单由
        # 期望签名逐一认领) → 精确 vwap 确认, 不再 UNKNOWN 封锁。
        if self.h._attr_feed(context):
            ap = self.h._attr_progress("__dual__")
            if ap is not None and ap.get("done"):
                self._finish_dual_exit(context, "orders",
                                       float(ap["vwap"]))
                return
        if intent.offset == "mixed":
            if not fp.get("alerted"):
                fp["alerted"] = True
                self.h._alert("dual_exit_mixed",
                              f"{code} cross-zero exit needs split orders; "
                              "kept UNKNOWN")
            return
        if intent.delta == 0:
            self._finish_dual_exit(context, "net_zero")
            return
        volume = 0.0
        vwap = None
        journal = self.h._trade_journal_source()
        if journal is not None and fp.get("cursor") is not None:
            volume, vwap = journal.fill_stats(
                code, fp["cursor"], side=intent.side, offset="close")
        fp["filled"] = min(intent.lots, float(volume))
        if volume >= intent.lots:
            self._finish_dual_exit(context, "trades", vwap)
            return
        if self.h._truth_mode == "rebuild":
            broker = self.h._truth_net(code)
            if broker is not None and int(round(broker)) == intent.after:
                self._finish_dual_exit(context, "truth_target", vwap)
                return
        waited = time.time() - float(fp.get("ts", 0))
        # 出场超时走分档口径 (Phase 0): 保护性订单默认 120s, 远早于入场
        if waited > self.h._risk.cfg.exit_fill_timeout_secs \
                and not fp.get("alerted"):
            fp["alerted"] = True
            txt = (f"{code} dual exit unconfirmed {waited:.0f}s: "
                   f"{volume:g}/{intent.lots} closed; risk/reentry blocked")
            context.stra_log_text(f"{self.h.name()} {txt}", level=3)
            self.h._alert("exit_unconfirmed", txt)

    def begin_exit_order(self, context, key: str, engine, dec: dict):
        """登记出场目标及证据游标；真实平仓前不清引擎/风险账。"""
        if key in self.h._exit_orders:
            return True
        reason = str(dec.get("reason", "exit"))
        price = float(dec.get("price", 0.0))
        if not engine.begin_exit(reason, price):
            return False
        journal = self.h._trade_journal_source()
        self.h._exit_orders[key] = {
            "engine": engine,
            "ts": time.time(),
            "lots": sum(l for l, _ in engine.layers),
            "direction": engine.direction,
            "decision_price": price,
            "reason": reason,
            "cursor": self._journal_cursor(journal),
            "filled": 0.0,
        }
        # 订单级归因: 净出场意图入队 (簿镜像在执行单元簿记口径下推进)。
        if self.h._attr_feed(context):
            d = 1 if engine.direction > 0 else -1
            lots = int(round(float(self.h._exit_orders[key]["lots"])))
            before = int(self.h._combined_target())
            self.h._attr.on_intent(str(key), before=before,
                                   after=before - d * lots,
                                   ts=float(self.h._exit_orders[key]["ts"]))
        # 意图先于目标单持久化；崩溃后可恢复 UNKNOWN，而不是当作已平。
        self.h._persist(context)
        return True

    def cancel_exit_order(self, context, key: str, engine):
        """仅用于目标提交异常：撤销未知态并继续管理原持仓。"""
        self.h._exit_orders.pop(key, None)
        engine.cancel_exit()
        if self.h._attr is not None:
            self.h._attr.attr.discard(str(key))
        self.h._persist(context)

    def _finish_exit_order(self, context, key: str, fp: dict,
                           price: float, source: str):
        engine = fp["engine"]
        engine.confirm_exit(float(price))
        self.h._risk.on_closed(self.h._inst.code)
        self.h._exit_orders.pop(key, None)
        if self.h._attr is not None:
            self.h._attr.attr.complete(str(key))
        context.stra_log_text(
            f"{self.h.name()}[{key}] exit confirmed @{float(price):.2f} "
            f"evidence={source}; engine/risk released")
        self.h._persist(context)

    def process_exit_orders(self, context):
        """按平仓成交证据推进 exit pending；超时只告警，绝不假确认。

        证据优先级：订单级 localid 归因 (精确到意图) → 本交易通道逐笔
        平仓 (方向+开平匹配) → 独占账户 (rebuild) 的券商零仓。共享 alert
        模式不使用账户绝对仓或净增量作为本策略成交证明。部分成交只更
        新进度，保留全部风险直到全成。
        """
        code = self.h._inst.code
        attr_on = self.h._attr_feed(context)
        for key, fp in list(self.h._exit_orders.items()):
            if fp.get("batch"):
                self._process_dual_exit(context, fp)
                continue
            engine = fp.get("engine")
            if engine is None or engine.direction == 0:
                self.h._exit_orders.pop(key, None)
                self.h._risk.on_closed(code)
                continue
            lots = float(fp.get("lots") or 0)
            if lots <= 0:
                continue
            direction = 1 if engine.direction > 0 else -1
            # 平多=卖出(-1)，平空=买入(+1)，与持仓方向相反。
            trade_side = -direction
            # 证据 0: localid 归因链 (跨零/反向场景的订单级精确证据)
            if attr_on:
                ap = self.h._attr_progress(str(key))
                if ap is not None and ap.get("done"):
                    self._finish_exit_order(context, key, fp,
                                            float(ap["vwap"]), "orders")
                    continue
            volume = 0.0
            vwap = None
            journal = self.h._trade_journal_source()
            if journal is not None and fp.get("cursor") is not None:
                volume, vwap = journal.fill_stats(
                    code, fp["cursor"], side=trade_side, offset="close")
            fp["filled"] = min(lots, float(volume))
            if volume >= lots:
                self._finish_exit_order(
                    context, key, fp,
                    vwap if vwap is not None else fp["decision_price"],
                    "trades")
                continue
            # 仅独占账户允许用券商绝对零仓作补充证据；共享 alert 下
            # 账户含其他策略仓位，不能用于本策略自动确认。
            if self.h._truth_mode == "rebuild":
                broker = self.h._truth_net(code)
                if broker == 0:
                    self._finish_exit_order(
                        context, key, fp,
                        vwap if vwap is not None else fp["decision_price"],
                        "truth_zero")
                    continue
                if broker is not None:
                    fp["broker_remaining"] = abs(float(broker))
            waited = time.time() - float(fp.get("ts", 0))
            # 出场超时走分档口径 (Phase 0, 默认 120s)
            if waited > self.h._risk.cfg.exit_fill_timeout_secs \
                    and not fp.get("alerted"):
                fp["alerted"] = True
                txt = (f"{code}[{key}] exit unconfirmed {waited:.0f}s: "
                       f"close fills {volume:g}/{lots:g}; target flat but "
                       "engine/risk retained, new entries blocked")
                context.stra_log_text(f"{self.h.name()} {txt}", level=3)
                self.h._alert("exit_unconfirmed", txt)

    def journal_layer_price(self, code: str, fp: dict, engine,
                             skip: float = 0.0):
        """本层真实成交均价 (逐笔成交口径); 不可用/未成交齐 → None。

        skip = 同组 (同合约同游标) 前序记录已分配的手数。
        """
        j = self.h._trade_journal_source()
        if j is None or fp.get("cursor") is None or not engine.layers:
            return None
        lots = fp.get("lots") or engine.layers[-1][0]
        try:
            return j.layer_price(
                code, fp["cursor"], float(lots),
                skip=float(fp.get("skip") or 0.0) + skip,
                side=engine.direction, offset="open")
        except Exception:
            return None
    def get_fees(self, context) -> float:
        """累计手续费 (flag=3)。

        降级用代理: 若**真值源不可用**, 只能用 fees 近似"真实成交"。
        注意其口径存疑 (2026-09-15 源码复核): fees 在
        do_set_position 内累加 (CtaStraBaseCtx.cpp:1469), 而该函数由
        on_tick 消费信号时触发 (:811), 并非券商成交回报回调 → 源码
        口径下"信号落地即计费", 代理可能失效 (fail-open); 反之费率
        未配置/为 0 的品种 (实测 INE.SC fees 恒 0) 表现为 fail-closed
        (永久疑似幽灵)。故新增通道真值源 (core/LiveTruth) 优先,
        fees 仅在真值缺失时兜底, 且超时后放行 (见 _fill_confirmed)。
        接口不可用返回 None (降级不检测)。
        """
        try:
            v = context.stra_get_fund_data(3)
            if isinstance(v, (int, float)) and v == v:
                return float(v)
        except Exception:
            pass
        return None
    def fill_confirmed(self, context, fp: dict, code: str,
                        target: int) -> tuple:
        """真实成交是否已到位 → (confirmed, src, pos)。

        src: "truth" = 交易通道真值(增量)判定; "trades" = 逐笔成交
        (trades.csv 只随真实成交回报增长, 与信号口径无关的真实证据);
        "framework" = 框架信号口径 (fees 代理)。

        证据链（2026-10-26 收紧）：
        1. 账户净仓增量仅在 `truth_rebuild_mode=rebuild`（显式独占账户）
           可用；共享 alert 下第三方同品种成交可能造成相同增量，不能归属。
        2. 本通道逐笔成交按品种 + open/close + 买卖方向匹配。CNL/CNS
           是平今真实成交，不是撤单。
        3. fees 是信号口径，只在环境没有真值源时作降级代理。真值存在时
           不用 fees 或墙钟超时替代真实成交；600s 告警后仍等待证据，
           避免把执行卡死误确认成已成交。
        """
        pos = float(context.stra_get_position(code))
        engine = fp.get("engine")
        sign = 1 if (engine is not None and engine.direction > 0) else -1
        if engine is None or engine.direction == 0:
            sign = 1
        lots = abs(float(fp.get("lots") or 0))
        t0 = fp.get("truth0")
        if t0 is not None and lots > 0 \
                and self.h._truth_mode == "rebuild" \
                and not self.h._is_dual:
            # 账户净增量只在本策略独占账户时有归属意义；共享 alert
            # 模式下第三方同品种成交可能恰好等于本层手数，不能确认。
            now = self.h._truth_net(code)
            if now is not None:
                delta = now - t0
                if delta * sign >= lots:
                    return True, "truth", now
                return False, "truth", now      # 增量未到位 (或未成交)
            # 真值中途失效 → 落逐笔成交/fees 代理
        if int(round(pos)) != target:
            return False, "framework", pos
        pos_src = "framework"       # 真值不可用 → 逐笔/fees 兜底口径
        # === 证据 2: 逐笔成交 (真实成交回报, 独立于真值链路) ===
        j = self.h._trade_journal_source()
        if j is not None and lots > 0 and fp.get("cursor") is not None:
            actual = j.layer_price(
                code, fp.get("cursor"), lots,
                float(fp.get("skip") or 0.0),
                side=sign, offset="open")
            if actual is not None:
                return True, "trades", pos
        # === 证据 3: fees 代理 (只有环境无真值时才可信) ===
        # 真值存在时，无论新鲜或陈旧，都不能用信号口径 fees 或墙钟
        # 超时替代真实成交；否则第三方交易/执行卡死会再次造成假确认。
        truth_present = self.h._truth_source() is not None
        fees0 = fp.get("fees0")
        if fees0 is None:
            return (False, pos_src, pos) if truth_present \
                else (True, pos_src, pos)
        fees = self.get_fees(context)
        fees_increased = fees is None or fees > fees0
        if fees_increased and not truth_present \
                and not self.h._truth_stale_now():
            return True, pos_src, pos
        # 以下两种情况都落入超时窗口 (600s 告警 / 2x 放行):
        # a) fees 未增 (免费/未配置费率品种, 旧行为);
        # b) fees 增但真值陈旧 — fees 是信号口径 (信号落地即计费),
        #    fail-open 会把未成交瞬间误确认 (2026-09-21 CF 47+47/
        #    p 29 手幽灵根因) → 视为未验证, 保持窗口等待真值恢复/
        #    真实成交落 journal, 超时后放行 (记账价保持决策价)
        waited = time.time() - fp.get("ts", 0)
        # Phase 0: 窗口从配置读取 (默认 600=旧行为); fees 未配置/免费的
        # 品种给 3x (旧 1800=600*3 等价口径)
        limit = self.h._risk.cfg.fill_timeout_secs \
            if fees0 > 0 else self.h._risk.cfg.fill_timeout_secs * 3
        if waited > limit * 2 and not truth_present:
            if not fp.get("unblocked"):
                fp["unblocked"] = True
                context.stra_log_text(
                    f"{self.h.name()} fills unverified {waited:.0f}s "
                    f"(truth stale & 无逐笔成交) — 超时放行, "
                    f"记账价保持决策价", level=3)
            return True, pos_src, pos
        return False, pos_src, pos
    def reanchor_actual_fill(self, context):
        """成交均价回写: 逐子系统用框架 avgpx 反推最后确认层的实际价。

        P2: 待回写记录按**子系统分槽** (`_fill_pending`, 原单槽会被同批
        后确认的子系统覆盖 → 先确认那层永不修正); 同批两次确认 (S1+S2
        入场, 或入场后紧跟加仓) 现在都能回写。

        confirm 时分批窗口未闭合 → 记账价 fallback 触发价; 待持仓量
        达到目标(分批完成)后, 从 avgpx 增量反推最后一层实际价修正,
        stop/add/exit 武装位随引擎 rearm 自动跟随 (偏差>0.5tick 才动)。
        与 _reanchor_on_roll 协同: 本方法先精确修最后一层, 换月检测
        的 our_avg vs fw_avg 差随之收敛, 不误报 hot roll。
        仅单方向持仓时数学可解 (双系统反向净额化无法归因单层, 跳过)。
        """
        alloc = {}      # (code, 游标) → 本轮已分配手数 (同组按确认顺序)
        for key in list(self.h._fill_pending):
            fp = self.h._fill_pending.get(key)
            if fp is not None:
                # 同合约多笔待回写: 按确认顺序逐笔分配成交手数 (见
                # TradeJournal.layer_price 的口径说明)
                code = self.h._inst.code
                gkey = (code, fp.get("cursor"))
                used = self.reanchor_one_fill(context, key, fp,
                                               alloc.get(gkey, 0.0))
                if used:
                    alloc[gkey] = alloc.get(gkey, 0.0) + used
    def reanchor_one_fill(self, context, key: str, fp: dict,
                           alloc: float = 0.0):
        """单个子系统的成交价回写 (队列元素处理, 完成即出队)。

        alloc: 同合约在本轮已分配给前序记录的成交手数 (逐笔成交口径)。
        返回本轮消耗的手数 (用了逐笔成交时), 否则 None。
        """
        e = fp["engine"]
        if e.direction == 0 or not e.layers:
            self.h._fill_pending.pop(key, None)
            return
        code = self.h._inst.code
        target = self.h._combined_target()
        confirmed, pos_src, pos = self.fill_confirmed(context, fp, code,
                                                       target)
        if not confirmed or pos == 0:
            # 分批窗口超时 (默认 600s 未达目标仓位) → 告警一次
            # (执行单元放弃追单/涨跌停无法成交的可观测代理, P0-2;
            #  真值源可用时这是"订单未到柜台"的直接证据)
            waited = time.time() - fp.get("ts", 0)
            if not fp.get("alerted") \
                    and waited > self.h._risk.cfg.fill_timeout_secs:
                fp["alerted"] = True
                # 真值源判定 = 券商侧确实没有这笔仓位 (幽灵仓直接证据)
                evt = "ghost_fill" if pos_src == "truth" else "fill_timeout"
                txt = (f"target={target} vs broker={pos} [{pos_src}], "
                       f"{waited:.0f}s 未达标")
                context.stra_log_text(
                    f"{self.h.name()} {evt}: {txt}", level=3)
                self.h._alert(evt, txt)
                if evt == "ghost_fill":
                    self.maybe_exec_recover(context, txt)
                    self.maybe_ghost_trim(context, key, fp, pos)
            return                # 分批成交中/未成交, 下次再试
        # F2 幽灵仓位判定已在 _fill_confirmed 完成: 真值源优先 (订单
        # 真没到柜台), fees 仅在真值缺失时兜底; 故此处到得仓已确认,
        # 可以用 avgpx 反推成交均价
        last_lots, recorded = e.layers[-1]
        # === 口径 1 (P2, 首选): 逐笔成交 → 本层真实成交均价 ===
        # 每层独立可解, 不受"多笔待回写"影响 (净均价方程欠定)
        actual = self.journal_layer_price(code, fp, e, alloc)
        src = "trades"
        used = (fp.get("lots") or last_lots) if actual is not None else None
        if actual is None:
            actual, src = self.avgpx_layer_price(context, e, code, pos), \
                "avgpx"
            if actual is None:
                return            # 净均价不可用 → 继续等待 (不猜)
        self.h._fill_pending.pop(key, None)
        if actual <= 0:
            return                # 反推异常(持仓干预等), 放弃修正
        if self.h._pricetick and abs(actual - recorded) > 0.5 * self.h._pricetick:
            e.reanchor_last_price(actual)
            self.h._sync_risk_book(self.h._last_n)
            context.stra_log_text(
                f"{self.h.name()} fill re-anchor layer{len(e.layers)} "
                f"{recorded:.2f} -> {actual:.2f} ({src}), "
                f"stop/add levels shifted")
        return used
    def avgpx_layer_price(self, context, engine, code: str, pos: float):
        """净均价反推本层成交价 (逐笔成交不可用时的兜底口径)。

        数学限制: N 个待回写层 → N 个未知价, 只有净均价 1 个方程 →
        仅当**只有一层未知**(其余层已确认)时精确; 该口径下其余层按
        记账价处理 (与升级前一致)。多笔并存时优先用逐笔成交。
        """
        if len(self.h._fill_pending) > 1:
            return None          # 欠定: 交由逐笔成交/聚合漂移修正
        fw_avg = context.stra_get_position_avgpx(code)
        if fw_avg == 0:
            return None
        dirs = {s["engine"].direction for s in self.h._subs
                if s["engine"].direction != 0}
        if len(dirs) > 1:
            return None          # 反向并存, 净仓成本无法归因
        sign = 1 if engine.direction > 0 else -1
        last_lots = engine.layers[-1][0]
        rest = 0.0                # 除最后确认层外全部层的带符号成本
        for s in self.h._subs:
            se = s["engine"]
            if se.direction == 0:
                continue
            ss = 1 if se.direction > 0 else -1
            layers = se.layers[:-1] if se is engine else se.layers
            for l, p in layers:
                rest += ss * l * p
        if not last_lots:
            return None
        return (fw_avg * pos - rest) / (sign * last_lots)
    def maybe_ghost_trim(self, context, key: str, fp: dict,
                          broker_net: float):
        """ghost_fill 自愈 (P0-3, 2026-09-17 CF 34 手幽灵仓事故)。

        真值源确认"订单未到柜台"后若只告警不处置, 引擎会永久按未持仓
        手数管理风险。自动裁剪会改变真实目标仓，因此仅允许在
        `truth_rebuild_mode=rebuild` 的显式独占账户中执行；共享 alert
        下账户绝对净仓含其他策略，必须告警人工处理，不能宣称共享免疫。
        裁剪仍遵循只缩量、尾部层优先、不同向不动作；base.ghost_auto_trim
        =False 时无论账户模式都只告警。
        """
        if not self.h._base.ghost_auto_trim:
            return
        if self.h._truth_mode != "rebuild":
            # 共享/未知归属账户：绝对净仓含其他策略，自动裁剪可能卖掉
            # 本策略真实仓。仅告警人工处置；独占账户才可自动 trim。
            txt = (f"{self.h._inst.code} ghost trim blocked: "
                   "shared/alert account lacks strategy fill attribution")
            context.stra_log_text(f"{self.h.name()} {txt}", level=3)
            self.h._alert("ghost_trim_blocked", txt)
            return
        broker_net = float(broker_net)
        tot = self.h._combined_target()
        if tot == 0:
            return                     # 无信号持仓 → 不动作
        if broker_net != 0 and (tot > 0) != (broker_net > 0):
            return                     # 方向相反 → 不动作 (告警已发)
        target = abs(int(round(broker_net)))
        cur = abs(tot)
        if target >= cur:
            return                     # 券商不少于信号 → 非本策略幽灵
        code = self.h._inst.code
        drop = cur - target
        trimmed = 0
        for sub in reversed(self.h._subs):      # 尾部层=最后加仓=最易幽灵
            if drop <= 0:
                break
            e = sub["engine"]
            sub_lots = sum(l for l, _ in e.layers)
            if sub_lots <= 0:
                continue
            d = min(drop, sub_lots)
            dropped = e.trim_to_lots(sub_lots - d)
            if dropped > 0:
                drop -= dropped
                trimmed += dropped
                context.stra_log_text(
                    f"{self.h.name()}[{sub['name']}] ghost trim: -{dropped} "
                    f"lots -> {sum(l for l, _ in e.layers)} (broker="
                    f"{broker_net})", level=3)
        if trimmed <= 0:
            return
        self.h._fill_pending.pop(key, None)     # 被裁层无需再等待回写
        # 当日禁止该层再触发: trim 后引擎层数回落, 武装位仍在, 若不消费
        # 会在价格条件仍满足时同秒 re-trigger → ghost 死循环
        # (2026-10-09 IF 每 ~10min trim→re-add 循环事故)
        get_date = getattr(context, "stra_get_date", None)
        if get_date is not None:
            try:
                self.h._ghost_trim_date = int(get_date())
            except Exception:
                pass
        new_target = self.h._combined_target()
        context.stra_set_position(code, new_target, "GhostTrim")
        self.h._sync_risk_book(self.h._last_n)
        self.h._alert("ghost_trim",
                    f"{code} signal trimmed {tot}->{new_target} "
                    f"to broker {broker_net} ({trimmed} lots ghost)")
    def maybe_exec_recover(self, context, detail: str):
        """执行单元卡死的分级自愈 (P1, 默认只告警)。

        真值源确认"券商侧仓位未达目标"后 (ghost_fill), 框架层没有任何
        自我修复入口: MiniImpact 执行单元要求 `_last_tick != NULL`
        (WtMinImpactExeUnit.cpp:315) 且只接受**严格同码** tick
        (:197-199), 而 WtLocalExecuter::on_tick 对"单元尚不存在"的
        合约直接丢 tick (:444-449), 单元只在首次目标变化时懒创建
        (:282 getUnit 自动创建) — 一旦懒创建发生在该合约 tick 断供/
        代码错配之后, 单元会永久停在 "execute later", 持仓裸奔
        (2026-09-09 实测 cu/al 卡死一天、MA 幽灵加仓)。

        已知可靠恢复手段是**重启进程** (启动期创建的单元正常 — F1
        预热已验证), 故按 WT_EXEC_RECOVER 环境变量分级:
          ""/"0" (默认) 只告警, 人工处置
          "restart"     触发进程自退, WatchDog guard 拉起后按真值源
                        重新收养持仓 (有界: 每日至多一次, 用 marker
                        文件跨进程限制, 防重启风暴)
        注意 restart 仅适用于真值源新鲜的场景 (排除真值本身陈旧造成
        的误判), 且退出后到重启完成之间持仓无保护 — 权衡见文档。
        """
        mode = os.environ.get("WT_EXEC_RECOVER", "").strip().lower()
        if mode != "restart":
            return
        t = self.h._truth_source()
        if t is None or t.stale():
            context.stra_log_text(
                f"{self.h.name()} exec recover skipped: 真值源缺失/陈旧",
                level=3)
            return
        day = _dt.datetime.now().strftime("%Y%m%d")
        marker = os.path.join(os.environ.get("WT_RECOVER_DIR") or _GROUP_DIR,
                              f".exec_recover_{day}")
        if os.path.exists(marker):
            context.stra_log_text(
                f"{self.h.name()} exec recover skipped: 本日已重启过一次 "
                f"({marker})", level=3)
            self.h._alert("exec_recover_skip",
                        "ghost_fill 仍在, 但本日已自愈重启过 → 需人工"
                        "处置 (查执行单元/通道)")
            return
        try:
            with open(marker, "w", encoding="utf-8") as f:
                f.write(f"{self.h.name()} {detail}\n")
        except OSError:
            pass
        context.stra_log_text(
            f"{self.h.name()} exec recover: 进程自退触发 WatchDog 重启 "
            f"(ghost_fill 未消解: {detail})", level=3)
        self.h._alert("exec_recover",
                    f"exec unit stalled, restarting process: {detail}")
        os._exit(1)
