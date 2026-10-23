"""执行层订单状态机 + 订单号归因 (2026-10-21 实验, 纯逻辑零框架依赖)

背景: CTA 策略层 (BaseCtaStrategy) 只有信号提交接口, 看不到订单/成交
回报 —— 订单状态机只在 HFT 层暴露。但 C++ 引擎 (WtCore/EventNotifier.cpp)
会把全量订单/成交事件广播到消息总线 (TRD_ORDER / TRD_TRADE 主题,
payload 见 orderToJson/tradeToJson):

  TRD_ORDER : trader, time, localid, code, islong, isopen, istoday,
              canceled, total, left, traded, price, state
  TRD_TRADE : trader, time, localid, code, islong, isopen, istoday,
              volume, price

localid + trader + code 构成完整的"订单→成交"归因链。本模块是纯逻辑:

- OrderTracker: 订单状态机 (在途/部成/全成/已撤) + 成交挂接 + 重复
  事件去重 (总线 at-least-once 语义, 以完全相同的连续事件为判据)。
- ExecAttributor: 决策价 → 订单成交质量归因 (滑点/成交率/耗时),
  按 (code, islong, isopen) + 时间窗匹配最近的决策记录。

旁路侧车 scripts/exec_journal.py 订阅同一总线落盘 JSONL, 交易进程
零侵入 (与 BarJournal/LiveTruth 同一设计哲学)。当前 StraTurtle 的
_reanchor_actual_fill 按"净持仓增量+时序分配"归因成交价, 无订单粒度;
本实验验证订单级归因链可用, 为后续执行质量监控 (拒单率/部成率/滑点)
提供数据基础。
"""


class OrderTracker:
    """订单状态机: (trader, localid) 唯一键, 成交事件挂接。"""

    def __init__(self):
        self.orders = {}      # (trader, localid) -> dict
        self.bad_trades = []  # 挂不到订单的成交 (订单事件缺失,  stub 订单兜底)

    # ------------------------------------------------------------------
    def on_order(self, ev: dict):
        """TRD_ORDER 事件。返回状态跃迁描述 dict; 完全重复的连续事件
        (总线重发) 返回 None。"""
        key = (ev.get("trader", ""), int(ev.get("localid", 0)))
        o = self.orders.get(key)
        snap = self._snap(ev)
        if o is not None and o.get("_last_snap") == snap:
            return None                       # at-least-once 去重
        if o is None:
            o = {"trades": [], "first_ts": ev.get("time", 0),
                 "unknown": False}
            self.orders[key] = o
        prev_state = o.get("state", "new")
        o.update({k: ev.get(k) for k in (
            "time", "code", "islong", "isopen", "istoday", "canceled",
            "total", "left", "traded", "price", "state")})
        o["_last_snap"] = snap
        o["last_ts"] = ev.get("time", 0)
        state = self._state(o)
        o["state"] = state
        if state != prev_state:
            return {"key": key, "from": prev_state, "to": state,
                    "code": o.get("code")}
        return {"key": key, "from": prev_state, "to": state,
                "code": o.get("code"), "quiet": True}

    def on_trade(self, ev: dict):
        """TRD_TRADE 事件挂到对应订单; 订单事件缺失时建 stub 兜底。
        完全重复的连续成交事件 (同一订单同 time/volume/price) 返回 None。"""
        key = (ev.get("trader", ""), int(ev.get("localid", 0)))
        o = self.orders.get(key)
        if o is None:
            o = {"trades": [], "first_ts": ev.get("time", 0),
                 "unknown": True, "code": ev.get("code"),
                 "islong": ev.get("islong"), "isopen": ev.get("isopen"),
                 "total": None, "left": None, "traded": None,
                 "canceled": None, "state": "unknown"}
            self.orders[key] = o
        t = (ev.get("time", 0), float(ev.get("volume", 0)),
             float(ev.get("price", 0)))
        if o["trades"] and o["trades"][-1] == t:
            return None                       # at-least-once 去重
        o["trades"].append(t)
        return {"key": key, "code": o.get("code"),
                "cum_vol": sum(x[1] for x in o["trades"])}

    # ------------------------------------------------------------------
    @staticmethod
    def _snap(ev: dict) -> tuple:
        return tuple(ev.get(k) for k in (
            "time", "code", "islong", "isopen", "istoday", "canceled",
            "total", "left", "traded", "price", "state"))

    @staticmethod
    def _state(o: dict) -> str:
        if o.get("canceled"):
            return "canceled"
        if o.get("left") is not None and float(o.get("left") or 0) == 0 \
                and o.get("total"):
            return "filled"
        if o.get("traded"):
            return "partial"
        return "alive"

    def snapshot(self) -> list:
        """全部订单快照 (去除内部字段)。"""
        out = []
        for (trader, localid), o in sorted(self.orders.items()):
            row = {k: v for k, v in o.items() if not k.startswith("_")}
            row["trader"], row["localid"] = trader, localid
            row["trade_cnt"] = len(o["trades"])
            out.append(row)
        return out

    def stats(self) -> dict:
        """状态分布 + 成交率 (traded/total 按订单等权)。"""
        dist = {"alive": 0, "partial": 0, "filled": 0, "canceled": 0,
                "unknown": 0}
        ratios, done = [], 0
        for o in self.orders.values():
            st = o.get("state", "unknown")
            dist[st] = dist.get(st, 0) + 1
            if st in ("filled", "canceled", "partial"):
                done += 1
                if o.get("total"):
                    ratios.append(float(o.get("traded") or 0)
                                  / float(o["total"]))
        return {"orders": len(self.orders), "by_state": dist,
                "done": done,
                "avg_fill_ratio": (sum(ratios) / len(ratios))
                if ratios else 0.0}


class ExecAttributor:
    """决策价 → 订单成交质量归因。

    CTA 层提交信号时不带 usertag, 订单归因只能按 (code, islong,
    isopen) + 时间窗匹配最近决策 (与 _reanchor_actual_fill 的时序
    分配同一哲学, 但这里是订单粒度: 滑点/部成/耗时都可算)。
    """

    def __init__(self, window_secs: float = 600.0):
        self.window = float(window_secs)
        self.decisions = []   # {tag, code, islong, isopen, price, ts}

    def record_decision(self, tag: str, code: str, islong: bool,
                        isopen: bool, price: float, ts):
        self.decisions.append({"tag": tag, "code": code,
                               "islong": bool(islong),
                               "isopen": bool(isopen),
                               "price": float(price), "ts": ts})

    def _match(self, code, islong, isopen, first_ts):
        best = None
        for d in self.decisions:
            if d["code"] != code or d["islong"] != bool(islong) \
                    or d["isopen"] != bool(isopen):
                continue
            dt = first_ts - d["ts"]
            if dt < 0 or dt > self.window:
                continue
            if best is None or dt < best[0]:
                best = (dt, d)
        return best[1] if best else None

    def attribute(self, tracker: OrderTracker) -> list:
        """逐订单归因: 返回 [{order, decision_tag, vwap, slippage,
        fill_ratio, elapsed}]; 滑点=不利方向为正 (买入 vwap-dec, 卖出
        dec-vwap)。"""
        rows = []
        for (trader, localid), o in tracker.orders.items():
            if not o["trades"]:
                continue
            dec = self._match(o.get("code"), o.get("islong"),
                              o.get("isopen"), o.get("first_ts", 0))
            vol = sum(t[1] for t in o["trades"])
            vwap = sum(t[1] * t[2] for t in o["trades"]) / vol if vol else 0.0
            buy = bool(o.get("islong")) == bool(o.get("isopen"))
            total = o.get("total") or vol
            row = {"trader": trader, "localid": localid,
                   "code": o.get("code"), "state": o.get("state"),
                   "vol": vol, "vwap": round(vwap, 6),
                   "fill_ratio": round(vol / float(total), 4)
                   if total else None,
                   "trade_cnt": len(o["trades"]),
                   "unknown_order": bool(o.get("unknown"))}
            if dec is not None:
                slip = (vwap - dec["price"]) if buy \
                    else (dec["price"] - vwap)
                row.update({"decision_tag": dec["tag"],
                            "decision_price": dec["price"],
                            "slippage": round(slip, 6),
                            "elapsed": o.get("first_ts", 0) - dec["ts"]})
            rows.append(row)
        return rows
