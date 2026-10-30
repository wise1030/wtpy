"""订单级成交归因链 (2026-10-29, 零 C++ 改动, 替代 userTag 方案)

设计论证见 docs/ORDER_ATTRIBUTION_NO_CPP.md。核心论断: 框架持仓簿是
每 code 一个净标量, 多 profile 引擎层是 gross 向量 —— 净增量证据
无法分解向量变化 (同向部分出场/反向并存/跨零 mixed/共享账户串扰)。
订单身份 localid 是唯一把"柜台事实"钉到"策略意图"上的键。

三条已存在的零侵入数据源拼成完整闭环:
  1. outbox 意图 (_open_intents/_exit_orders, v4 快照): 按什么顺序
     提交了什么净变化 (profile 向量语义);
  2. EventNotifier 侧车 exec_YYYYMMDD.jsonl (scripts/exec_journal.py,
     10-21 已部署生产): TRD_ORDER/TRADE 自带 localid/islong/isopen;
  3. trades.csv (TraderAdapter 进程内同步落盘): action 类型化的权威
     成交兜底, 覆盖总线丢包 (R3)。

绑定依据: 信号 FIFO 消费 + 每策略独立执行器 ⇒ 同 code 第 k 个未匹配
订单事件 == 第 k 个未匹配意图 (提交序==建单序), 不需要 userTag 也
不需要时间窗猜测。残余风险登记 (R1 撤单重挂/R4 同签名) 与守恒校验
见文档 §4; 匹配失败一律保持 UNKNOWN, 回落 fill_tracker 现有证据链。

本模块纯逻辑零框架依赖 (与 TradeJournal 同哲学), 全部行为可单测。
"""
import glob
import os


# ---------------------------------------------------------------------
# 执行单元多空簿镜像 + 净变化 → 订单签名预测
# ---------------------------------------------------------------------

def _signatures(book_long: float, book_short: float, after_net: int):
    """(多簿, 空簿, 新净目标) → 期望订单签名列表 [(islong, isopen, vol)]。

    簿记规则 (WtMinImpactExeUnit 语义镜像): 目标净>0 → 多簿=净/空簿=0;
    净<0 对偶; 净=0 两簿清零。每个簿的差量翻译为一张订单:
      多簿增加 → 开多 (True, True);  多簿减少 → 平多 (True, False);
      空簿增加 → 开空 (False, True);  空簿减少 → 平空 (False, False)。
    平今/平昨 (istoday) 由事件自带, 预测不打这个维度。
    """
    al = float(after_net)
    new_long = max(0.0, al)
    new_short = max(0.0, -al)
    out = []
    d_long = new_long - float(book_long)
    d_short = new_short - float(book_short)
    if d_long > 0:
        out.append((True, True, int(round(d_long))))
    elif d_long < 0:
        out.append((True, False, int(round(-d_long))))
    if d_short > 0:
        out.append((False, True, int(round(d_short))))
    elif d_short < 0:
        out.append((False, False, int(round(-d_short))))
    return out


class OrderAttributor:
    """意图队列 × 订单事件 → per-intent 成交进度 (localid 归因)。

    用法:
        attr = OrderAttributor()
        attr.init_book("SHFE.rb.HOT", long=10, short=4)   # 券商两簿镜像
        attr.on_intent("tx#1", "SHFE.rb.HOT", before=6, after=-4, ts=...)
        for ev in feed.poll():        # OrderFeed 归一化事件
            attr.on_order(ev) / attr.on_trade(ev)
        attr.progress("tx#1") → {"filled", "lots", "vwap", "done"}

    语义保证:
    - 每 code FIFO: 订单只与队首意图的当前期望签名匹配 (提交序==建单序);
      签名不匹配的订单视为外部/其他策略订单, 跳过且不消耗队列。
    - 跨零意图的多个期望签名按订单到达顺序逐一认领 (mixed 场景由此
      自动消解, 不再需要一律 UNKNOWN)。
    - 撤单解绑 localid 但保留签名待重挂订单认领 (R1)。
    - 无订单事件的成交 (总线丢包, R3) 按 localid 建 stub 绑定, 或
      在签名唯一时按 (islong/isopen) 直接认领。
    - 无法匹配的成交进 unmatched (调用方守恒校验 → 保持 UNKNOWN)。
    """

    BIND_SLACK = 30.0        # 事件 ts 早于意图 ts 超过该值视为旧事件

    def __init__(self):
        self._books = {}         # code -> {"long": x, "short": y} (镜像随
                                 #  on_intent 推进; 初始值由券商两簿/引擎
                                 #  净仓喂入, 未知时按意图 before 校准)
        self._queues = {}        # code -> [intent dict] (FIFO)
        self._by_iid = {}        # iid -> intent
        self._by_localid = {}    # (code, localid) -> intent
        self.unmatched = []      # 无法匹配的成交 (守恒校验告警用)

    # ------------------------------------------------------------------
    def init_book(self, code: str, long: float = 0.0, short: float = 0.0):
        self._books[code] = {"long": float(long), "short": float(short)}

    def on_intent(self, iid: str, code: str, before: int, after: int,
                  ts: float = 0.0):
        """登记一个净目标变化意图; 镜像簿随之推进并产出期望签名。

        幂等: 同一 iid 且同 (before, after) 的重复登记直接返回既有
        签名 (确保路径先重放已持久化意图、注册路径再次登记时安全)。
        """
        old = self._by_iid.get(str(iid))
        if old is not None and int(old["before"]) == int(before) \
                and int(old["after"]) == int(after):
            return list(old["sigs"])
        if old is not None:
            self.complete(str(iid))
        book = self._books.setdefault(code, {"long": 0.0, "short": 0.0})
        # 镜像漂移校准: 若镜像净仓与意图 before 不符, 以意图为准重置
        # (恢复路径/手工干预后镜像可能落后)。
        if int(round(book["long"] - book["short"])) != int(before):
            book["long"] = max(0.0, float(before))
            book["short"] = max(0.0, float(-before))
        sigs = _signatures(book["long"], book["short"], int(after))
        al = float(after)
        book["long"], book["short"] = max(0.0, al), max(0.0, -al)
        # lots 取净变化绝对值 —— 与签名如何分解无关, 跨零/簿漂移下都精确。
        lots = abs(float(after) - float(before))
        rec = {"iid": str(iid), "code": code, "before": int(before),
               "after": int(after), "ts": float(ts or 0.0),
               "sigs": sigs, "sig_idx": 0,
               "bound": {},        # localid -> {"filled": x, "amount": y,
                                   #              "vol": 期望, "sig": 序号}
               "filled": 0.0, "amount": 0.0, "lots": float(lots),
               "done": False}
        self._queues.setdefault(code, []).append(rec)
        self._by_iid[rec["iid"]] = rec
        return sigs

    def discard(self, iid: str):
        """意图撤销/过期: 出队并把镜像簿回滚到 before。"""
        rec = self._by_iid.pop(str(iid), None)
        if rec is None:
            return
        q = self._queues.get(rec["code"], [])
        if rec in q:
            q.remove(rec)
        ab = float(rec["after"])
        b = float(rec["before"])
        book = self._books.setdefault(rec["code"],
                                      {"long": 0.0, "short": 0.0})
        # 仅当镜像仍停在 after 时回滚 (后续意图已推进则保持)
        if int(round(book["long"] - book["short"])) == int(ab):
            book["long"], book["short"] = max(0.0, b), max(0.0, -b)
        for lid in list(rec["bound"]):
            self._by_localid.pop((rec["code"], lid), None)

    def complete(self, iid: str):
        """意图已确认 (运行时同步路径): 出队但不回滚簿镜像 —— 持仓确实
        到达了 after, 镜像必须保留给后续意图的签名预测。"""
        rec = self._by_iid.pop(str(iid), None)
        if rec is None:
            return
        q = self._queues.get(rec["code"], [])
        if rec in q:
            q.remove(rec)
        for lid in list(rec["bound"]):
            self._by_localid.pop((rec["code"], lid), None)

    # ------------------------------------------------------------------
    @staticmethod
    def _ev_ts(ev: dict) -> float:
        return float(ev.get("ts") or 0.0)

    def _current_sig(self, rec: dict):
        if rec["sig_idx"] < len(rec["sigs"]):
            return rec["sigs"][rec["sig_idx"]]
        return None

    def _bind(self, rec: dict, localid, ev_ts: float, expect_vol: int,
              sig: int = None):
        key = (rec["code"], localid)
        if key in self._by_localid:
            return
        b = {"filled": 0.0, "amount": 0.0, "vol": int(expect_vol),
             "sig": int(rec["sig_idx"] if sig is None else sig),
             "ts": ev_ts}
        rec["bound"][localid] = b
        self._by_localid[key] = rec

    def _infer_book(self, rec: dict, ev: dict) -> bool:
        """用观测订单反推执行单元真实多空簿, 重算该意图剩余签名。

        触发条件: 订单方向/开平与期望签名一致但手数不符 (镜像从净仓
        初值无法区分 gross 多空拆分, 如真实簿 L10/S4 vs 镜像 L6/S0)。
        反推: 平多 V → 簿多 = 目标多簿 + V; 开空 V → 簿空 = 目标空簿 - V;
        另一簿由 before 净额解出。不一致 (负手数/签名数不够) 返回 False,
        调用方维持原签名绑定 (提交序不变量兜底), 不毒化队列。
        """
        after = float(rec["after"])
        new_long, new_short = max(0.0, after), max(0.0, -after)
        L = bool(ev.get("islong"))
        O = bool(ev.get("isopen"))
        try:
            v = float(ev.get("total") or 0.0)
        except (TypeError, ValueError):
            return False
        if v <= 0:
            return False
        if L and not O:
            bl = new_long + v
            bs = bl - float(rec["before"])
        elif L and O:
            bl = new_long - v
            bs = bl - float(rec["before"])
        elif not L and not O:
            bs = new_short + v
            bl = float(rec["before"]) + bs
        else:
            bs = new_short - v
            bl = float(rec["before"]) + bs
        if bl < 0 or bs < 0:
            return False
        rest = _signatures(bl, bs, int(rec["after"]))
        consumed = int(rec["sig_idx"])
        if len(rest) <= consumed:
            return False
        rec["sigs"] = rest
        book = self._books.setdefault(rec["code"],
                                      {"long": 0.0, "short": 0.0})
        book["long"], book["short"] = bl, bs
        return True

    def _find_sig(self, rec: dict, islong, isopen):
        """意图中方向/开平匹配且未绑定饱和的第一个签名序号。

        从当前签名起向后找: 执行单元多张订单同时在途, 后序签名的订单
        事件可能先于前序签名成交完成到达。饱和判据 = 该签名已绑定订单
        的委托量合计 ≥ 期望手数 (委托在手不等于已成交, 只约束不再
        把新订单绑到它上面)。
        """
        for j in range(rec["sig_idx"], len(rec["sigs"])):
            sig = rec["sigs"][j]
            if bool(sig[0]) != bool(islong) or bool(sig[1]) != bool(isopen):
                continue
            committed = sum(b["vol"] for b in rec["bound"].values()
                            if b["sig"] == j)
            if committed + 1e-9 < sig[2]:
                return j
        return None

    def on_order(self, ev: dict):
        """TRD_ORDER 事件: 绑定 localid → 队首意图的匹配签名。

        ev: {code, localid, islong, isopen, total, canceled, ts}
        撤单 → 解绑 (重挂订单将携带新 localid 重新认领同一签名)。
        手数以订单事件为准; 簿镜像 gross 拆分漂移在成交超量时惰性推断
        (不在绑定时猜, 避免外部订单触发错误簿重写)。
        """
        code = str(ev.get("code") or "")
        localid = ev.get("localid")
        if localid is None:
            return
        localid = str(localid)
        key = (code, localid)
        ev_ts = self._ev_ts(ev)
        if ev.get("canceled"):
            rec = self._by_localid.pop(key, None)
            if rec is not None:
                rec["bound"].pop(localid, None)
            return
        if key in self._by_localid:
            return
        for rec in self._queues.get(code, []):
            if rec["done"]:
                continue
            if ev_ts and rec["ts"] and \
                    ev_ts < rec["ts"] - self.BIND_SLACK:
                continue                 # 早于意图的事件, 判给更早意图
            j = self._find_sig(rec, ev.get("islong"), ev.get("isopen"))
            if j is None:
                continue
            # 提交序==建单序是不变锚: 按方向/开平绑定, 手数以订单为准
            self._bind(rec, localid, ev_ts,
                       int(ev.get("total") or rec["sigs"][j][2]), sig=j)
            return

    def on_trade(self, ev: dict):
        """TRD_TRADE / trades.csv 成交: 累进归属意图进度。

        ev: {code, localid?, volume, price, islong?, isopen?, ts}
        localid 已绑定 → 直接累进; 未绑定 → stub 按签名认领 (R3 兜底)。
        """
        code = str(ev.get("code") or "")
        vol = float(ev.get("volume") or 0.0)
        price = float(ev.get("price") or 0.0)
        if vol <= 0 or price <= 0:
            return
        localid = ev.get("localid")
        localid = None if localid is None else str(localid)
        ev_ts = self._ev_ts(ev)
        rec = self._by_localid.get((code, localid)) if localid else None
        if rec is None:
            # 无订单事件的成交: 找签名匹配的待签名意图 (方向可用时按
            # 方向, 否则按未饱和签名顺序 —— R3 总线丢包兜底)
            for cand in self._queues.get(code, []):
                if cand["done"]:
                    continue
                if ev_ts and cand["ts"] and \
                        ev_ts < cand["ts"] - self.BIND_SLACK:
                    continue
                j = self._find_sig(cand, ev.get("islong"), ev.get("isopen")) \
                    if ev.get("islong") is not None else None
                if j is None and ev.get("islong") is None:
                    for k in range(cand["sig_idx"], len(cand["sigs"])):
                        committed = sum(b["vol"] for b in cand["bound"].values()
                                        if b["sig"] == k)
                        if committed + 1e-9 < cand["sigs"][k][2]:
                            j = k
                            break
                if j is None:
                    continue
                rec = cand
                if localid:
                    self._bind(rec, localid, ev_ts, rec["sigs"][j][2],
                               sig=j)
                break
        if rec is None:
            self.unmatched.append({"code": code, "localid": localid,
                                   "volume": vol, "price": price})
            return
        if localid:
            b = rec["bound"].get(localid)
            if b is not None:
                b["filled"] += vol
                b["amount"] += vol * price
        rec["filled"] += vol
        rec["amount"] += vol * price
        # 簿镜像 gross 拆分漂移的惰性纠正: 任一签名绑定订单的成交已
        # **超过**其期望手数 (真实簿多空拆分与净仓初值不同, 如真实
        # L10/S4 vs 镜像 L6/S0) → 用最大订单反推真实簿并重算剩余签名。
        for j in range(rec["sig_idx"], len(rec["sigs"])):
            want = rec["sigs"][j][2]
            overs = [b for b in rec["bound"].values()
                     if b["sig"] == j and b["filled"] > want + 1e-9]
            if overs:
                top = max(overs, key=lambda b: b["vol"])
                if self._infer_book(rec, {"islong": rec["sigs"][j][0],
                                          "isopen": rec["sigs"][j][1],
                                          "total": top["vol"]}):
                    break                     # 签名已重算, 继续常规推进
        # 签名推进: 该签名的全部绑定 localid 合计达到期望手数
        while rec["sig_idx"] < len(rec["sigs"]):
            want = rec["sigs"][rec["sig_idx"]][2]
            got = sum(b["filled"] for b in rec["bound"].values()
                      if b["sig"] == rec["sig_idx"])
            if got + 1e-9 < want:
                break
            rec["sig_idx"] += 1
        if rec["sig_idx"] >= len(rec["sigs"]):
            rec["done"] = True

    # ------------------------------------------------------------------
    def progress(self, iid: str):
        """意图成交进度 → None (无进度) 或 dict。

        done=True 时 vwap 为该意图全部真实成交的加权均价; 调用方据此
        精确 confirm_enter/add/exit。done=False 时给部分进度观测。
        """
        rec = self._by_iid.get(str(iid))
        if rec is None:
            return None
        return {"iid": rec["iid"], "filled": rec["filled"],
                "lots": rec["lots"], "vwap": (rec["amount"] / rec["filled"])
                if rec["filled"] > 0 else None,
                "done": bool(rec["done"]),
                "localids": sorted(rec["bound"], key=str)}

    def pending(self, code: str = None) -> list:
        out = []
        for q in self._queues.values():
            out.extend(r for r in q if not r["done"])
        if code is not None:
            out = [r for r in out if r["code"] == code]
        return out


# ---------------------------------------------------------------------
# 事件源: 侧车 JSONL + trades.csv 双源增量读取 (进程内零侵入消费)
# ---------------------------------------------------------------------

# TraderAdapter::formatAction → (islong, isopen); 与 LiveTruth._ACTION_INFO
# 同源的订单视角映射 (OL=开多/...), 用于 trades.csv 兜底行。
_CSV_ACTION = {"OL": (True, True), "OS": (False, True),
               "CL": (True, False), "CNL": (True, False),
               "CS": (False, False), "CNS": (False, False)}


class OrderFeed:
    """归一化订单/成交事件的增量供给。

    数据源 (全部已存在, 零 C++ 改动):
      - exec_*.jsonl: 侧车订阅总线的 TRD_ORDER/TRADE 落盘 (含 localid/
        islong/isopen/istoday/状态); 文件按日轮转, 按 (path, 行号) 增量。
      - trades.csv: TraderAdapter 同步落盘 (进程内不丢), 列
        localid,...,action,...,volume,price —— 作为总线丢包时的权威
        成交兜底; 与 JSONL 按 (localid,volume,price) 去重。

    无数据文件 (回测/CI/未部署侧车) → poll() 恒空 → 归因链静默不激活,
    fill_tracker 现有证据链行为逐字不变。
    """

    def __init__(self, journal_dir: str = "", trades_csv: str = ""):
        self.journal_dir = journal_dir or ""
        self.trades_csv = trades_csv or ""
        self._files = {}         # path -> 已读行数
        self._seen_trades = set()  # (localid, volume, price) 跨源去重
        self.prime()             # 存量积压只登记游标/去重键, 不回放事件

    def prime(self):
        """首启/重启时吞掉存量积压: 记录各文件当前行数并把已有成交登记
        进跨源去重键 —— 归因只从初始时刻起的新事件开始, 否则无待归因
        意图时历史成批 unmatched (2026-10-30 生产部署实测 7130 笔告警)。"""
        if self.journal_dir:
            for path in sorted(glob.glob(os.path.join(self.journal_dir,
                                                      "exec_*.jsonl"))):
                evs, end = self._read_jsonl(path, 0)
                for ev in evs:
                    if ev.get("type") == "trade":
                        self._seen_trades.add(
                            (str(ev.get("localid")),
                             float(ev.get("volume") or 0),
                             float(ev.get("price") or 0)))
                self._files[path] = end
        if self.trades_csv:
            evs, end = self._read_csv(self.trades_csv, 0)
            self._files[self.trades_csv] = end

    # ------------------------------------------------------------------
    def _read_jsonl(self, path: str, start: int):
        out = []
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            return out, start
        i = start
        while i < len(lines):
            line = lines[i]
            i += 1
            if i == len(lines) and not line.endswith(("\n", "\r")):
                i -= 1            # 半行不消费 (与 TradeJournal 同口径)
                break
            import json
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            out.append(ev)
        return out, i

    def _poll_jsonl(self):
        events = []
        if not self.journal_dir:
            return events
        paths = sorted(glob.glob(os.path.join(self.journal_dir,
                                              "exec_*.jsonl")))
        for path in paths:
            start = self._files.get(path, 0)
            evs, end = self._read_jsonl(path, start)
            self._files[path] = end
            for ev in evs:
                events.append(ev)
        return events

    def _poll_csv(self):
        """trades.csv 增量行 → trade 事件 (仅作 JSONL 丢包兜底)。"""
        start = self._files.get(self.trades_csv, 0)
        evs, end = self._read_csv(self.trades_csv, start)
        self._files[self.trades_csv] = end
        return evs

    def _read_csv(self, path: str, start: int):
        out = []
        if not path:
            return out, start
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            return out, start
        i = start
        while i < len(lines):
            line = lines[i]
            i += 1
            if i == len(lines) and not line.endswith(("\n", "\r")):
                i -= 1
                break
            cells = line.strip().split(",")
            if len(cells) < 7:
                continue
            action = cells[4].strip().upper()
            sig = _CSV_ACTION.get(action)
            if sig is None:
                continue
            try:
                vol = float(cells[5])
                price = float(cells[6])
            except (TypeError, ValueError):
                continue
            if vol <= 0 or price <= 0:
                continue
            key = (cells[0].strip(), vol, price)
            if key in self._seen_trades:
                continue
            self._seen_trades.add(key)
            out.append({"kind": "trade", "code": cells[3].strip(),
                        "localid": cells[0].strip(), "volume": vol,
                        "price": price, "islong": sig[0],
                        "isopen": sig[1], "ts": 0.0})
        return out, i

    # ------------------------------------------------------------------
    def poll(self) -> list:
        """本轮新事件 (归一化): {"kind": order|trade, ...}。"""
        out = []
        for ev in self._poll_jsonl():
            typ = ev.get("type")
            lid = ev.get("localid")
            lid = None if lid is None else str(lid)
            if typ == "order":
                out.append({"kind": "order",
                            "code": str(ev.get("code") or ""),
                            "localid": lid,
                            "islong": ev.get("islong"),
                            "isopen": ev.get("isopen"),
                            "total": ev.get("total"),
                            "canceled": bool(ev.get("canceled")),
                            "ts": float(ev.get("ts") or 0.0)})
            elif typ == "trade":
                key = (lid, float(ev.get("volume") or 0),
                       float(ev.get("price") or 0))
                if key in self._seen_trades:
                    continue
                self._seen_trades.add(key)
                out.append({"kind": "trade",
                            "code": str(ev.get("code") or ""),
                            "localid": lid,
                            "volume": ev.get("volume"),
                            "price": ev.get("price"),
                            "islong": ev.get("islong"),
                            "isopen": ev.get("isopen"),
                            "ts": float(ev.get("ts") or 0.0)})
        # CSV 兜底排在 JSONL 之后: 已去重的跳过, 缺的补上
        out.extend(self._poll_csv())
        return out


class ExecAttribution:
    """OrderFeed → OrderAttributor 的组装器 (策略侧单点入口)。

    簿镜像初始化优先级: 券商两簿 (rtdata positions 精确到多空) >
    引擎净仓 (alert 模式兜底)。镜像漂移由 on_intent 的 before 校准
    兜住, 预测错误只导致签名不匹配 → 该订单跳过 → 意图保持 UNKNOWN
    回落旧证据链, 不会假确认。
    """

    def __init__(self, code: str, journal_dir: str = "",
                 trades_csv: str = ""):
        self.code = code
        self.attr = OrderAttributor()
        self.feed = OrderFeed(journal_dir, trades_csv)
        self._inited = False

    def init_book(self, broker_positions: list = None,
                  fallback_net: float = 0.0, default_dir: str = ""):
        """券商多空两簿 (rtdata positions, 按品种前缀) → 镜像初值。"""
        prefix = ".".join(str(self.code).split(".")[:2]) + "."
        long = short = 0.0
        if broker_positions:
            for p in broker_positions:
                if not isinstance(p, dict):
                    continue
                if not str(p.get("code", "")).startswith(prefix):
                    continue
                for side, acc in (("long", 1), ("short", -1)):
                    item = p.get(side)
                    if not isinstance(item, dict):
                        continue
                    for k in ("newvol", "prevol"):
                        v = item.get(k)
                        if isinstance(v, (int, float)) and v == v:
                            if acc > 0:
                                long += float(v)
                            else:
                                short += float(v)
        if long == 0.0 and short == 0.0:
            net = float(fallback_net or 0.0)
            long, short = max(0.0, net), max(0.0, -net)
        self.attr.init_book(self.code, long, short)
        self._inited = True

    def on_intent(self, iid: str, before: int, after: int, ts: float):
        return self.attr.on_intent(iid, self.code, before, after, ts)

    def discard(self, iid: str):
        self.attr.discard(iid)

    def poll(self):
        """增量事件喂入归因器; 返回本轮未匹配成交数 (守恒校验用)。

        事件按品种前缀过滤: 侧车/成交落盘是**账户级** (共享账户下含
        其他策略的全部订单, 2026-10-30 生产实测 INE.ec/ao/ag 外部
        成交进入全部 12 策略的 feed), 只消费本 code 前缀的事件。
        """
        from core.LiveTruth import code_prefix
        prefix = code_prefix(self.code) or str(self.code)
        n0 = len(self.attr.unmatched)
        for ev in self.feed.poll():
            code = str(ev.get("code") or "")
            if code != str(self.code) and not (
                    prefix and code.startswith(prefix)):
                continue
            # 事件归一化到 HOT 键: 队列/绑定全部以 self.code 为键,
            # 事件携带的是真实合约码 (SHFE.rb.2610)
            ev = dict(ev, code=str(self.code))
            if ev["kind"] == "order":
                self.attr.on_order(ev)
            else:
                self.attr.on_trade(ev)
        return len(self.attr.unmatched) - n0

    def progress(self, iid: str):
        return self.attr.progress(iid)
