"""StraTurtle — 海龟趋势跟踪策略 (wtpy CTA 框架)

架构: 薄壳模式, 信号逻辑全部在 helpers/TurtleEngine (纯逻辑可单测)。
本层职责:
- 生命周期: on_init 订阅K线/注册图表/恢复状态, on_calculate 主流程
- 指标计算: ATR(N) + Donchian 通道 (helpers/indicators)
- 执行: 方向闸 → VaR 手数 → stra_set_position 目标仓位
- 换月适配: avgpx 漂移 re-anchor 引擎层价格 (.HOT 自动移仓)
- 持久化: user_save_data 引擎快照, 重启恢复
- 图表: 通道线/止损线叠加主图, ATR/持仓副图, 进出场 mark

框架对齐:
- 交易 .HOT 主力连续合约, C++ 核心自动换月
- 合约乘数取 stra_get_comminfo().volscale (非配置)
- set_position 目标语义, live 由 executers.yaml 执行单元落单
"""
import json
import os
import time

import numpy as np
from wtpy import BaseCtaStrategy, CtaContext

from schemas import InstrumentConfig, TurtleBaseConfig, RiskConfig
from filters import build_chain, FilterContext
from helpers.indicators import wilder_atr, donchian
from helpers.TurtleEngine import (TurtleEngine, DEC_ENTER, DEC_ADD,
                                  DEC_EXIT, DEC_SKIP, DEC_ARM)
from helpers.PositionSizer import PositionSizer
from helpers.RiskManager import RiskManager

# 组合目录 (部署侧 = deploy/cta_turtle): 真值源/状态文件按此定位
_GROUP_DIR = os.path.dirname(os.path.abspath(__file__))


class StraTurtle(BaseCtaStrategy):

    # 同类否决日志的冷却秒数 (P2: 逐 key 冷却, 见 _veto_log)
    _VETO_LOG_COOLDOWN = 300.0
    # 执行单元卡死自愈的每日上限标记目录可用 WT_RECOVER_DIR 覆盖

    def __init__(self, name: str, inst_cfg: InstrumentConfig,
                 base_cfg: TurtleBaseConfig,
                 risk_cfg: RiskConfig = None,
                 risk_mgr: RiskManager = None,
                 notify=None,
                 bt_no_simtick: bool = False):
        BaseCtaStrategy.__init__(self, name)
        self._inst = inst_cfg
        self._base = base_cfg
        self._notify = notify    # helpers/notifier.WebhookNotifier 或 None
        # bt 日线 trigger 退化: 无 m5 数据时不订 tick/m5 副K (模拟tick
        # 只能由非日K bar 生成, simTicks 跳过 KP_DAY), 触发判定仍走
        # on_bar 的 check_bar, 撮合退化为 bar 收盘口径 (成交价偏保守,
        # 与 rb 有 m5 时的 tick 撮合基线不可直接对拍)
        self._bt_no_simtick = bt_no_simtick

        # === 子系统装配 ===
        # S1+S2: 经典海龟双系统并行, 各分得 floor/ceil 半量 units,
        # 共享过滤链与组合风控账本 (同 code 合并记账), 独立引擎状态;
        # 框架持仓为两子系统净和, 批量执行后统一 set_position。
        # units=1 时 S2 分 0 手 → 其入场按 lots=0 否决 (空转)。
        self._subs = []

        def _mk_sub(sysname: str, units, entry=None, exit_=None, fs=None,
                    share=1.0):
            """装配一个子系统/速度; entry/exit/fs=None 时沿用全局 S1/S2 口径"""
            return {"name": sysname, "units": max(0.0, float(units)),
                    "entry": entry, "exit": exit_, "fs": fs,
                    "share": float(share),
                    "engine": TurtleEngine(
                        system=sysname,
                        stop_atr_mult=base_cfg.stop_atr_mult,
                        pyramid_layers=base_cfg.pyramid_layers,
                        pyramid_step_atr=base_cfg.pyramid_step_atr,
                        enable_s1_filter=base_cfg.enable_s1_filter,
                        entry_mode=base_cfg.entry_mode,
                        stop_n_mode=base_cfg.stop_n_mode)}

        if getattr(inst_cfg, "speeds", None):
            # 多速度 (2026-09-16): 每档速度 = 独立通道窗口的子系统, 按 share
            # 切分品种风险预算, 框架层净额化后一次下单 (净额化同时降低换手)
            u = float(inst_cfg.units)
            for sp in inst_cfg.speeds:
                self._subs.append(_mk_sub(sp.name, u * sp.share, sp.entry,
                                          sp.exit, sp.fs, sp.share))
        elif inst_cfg.system == "S1+S2":
            u = int(inst_cfg.units)
            self._subs.append(_mk_sub("S1", u - u // 2,
                                      share=(u - u // 2) / max(u, 1)))
            self._subs.append(_mk_sub("S2", u // 2,
                                      share=(u // 2) / max(u, 1)))
        else:
            self._subs.append(_mk_sub(inst_cfg.system, inst_cfg.units))
        # 兼容别名: 单系统即唯一引擎; 双系统指向 S1 子系统
        self._engine = self._subs[0]["engine"]

        self._sizer = PositionSizer(base_cfg.unit_var)
        # 组合风控: runLive 多品种共享同一实例; 单品种回测私有实例等价
        self._risk = risk_mgr or RiskManager(risk_cfg or RiskConfig())
        # 信号过滤链 (隐式方向闸 + instruments[].filters)
        self._chain = build_chain(inst_cfg)
        self._volscale = None       # on_init 时从框架取
        self._pricetick = None
        # 否决日志去重: {key: 上次记录时间} + 命中次数 (P2: 原实现是全局
        # 单槽 — risk/rscale/addrisk 交替触发时每根 bar 重复刷屏, 且成功
        # 入场会重置槽位, 使持续存在的否决在每次成功后被再次记录)
        self._veto_last = {}
        self._veto_hits = {}
        self._recon_miss = 0        # 运行时对账连续失配计数
        self._last_n = float("nan")  # 对账重建记账用最近 N
        # 盘中去重状态 (P1: None=待首个 calculate 建基线, 不回放历史)
        self._d1_date = None        # 已消费的信号周期末根 bar 日期
        self._m5_key = None         # 已扫描的盘中止损主K末根键值
        self._catchup_date = None   # 自愈评估已检查的交易日 (每日至多一次)
        self._in_schedule = False   # on_calculate 期间置位 (图表API仅此时可用)
        # 成交均价待回写队列: {子系统名: {"engine","ts","fees0",...}}
        # (P2: 原实现是单槽, 同一批次里后确认的子系统会覆盖前一个,
        #  导致先确认那层的记账价永不被成交均价修正)
        self._fill_pending = {}
        # 涨跌停锁死疑似状态: {code: True}; on_tick 价格钉住检测置位,
        # 价格恢复移动即清除; 置位期间 veto 加仓 (出场/止损不受影响)
        self._limit_lock = {}
        self._pin_price = None      # [price, first_ts] 价格钉住检测
        self._fund_live_ts = 0.0    # fund_live.csv 写入节流
        # K线旁路日志 (数据完整性: 崩溃窗口兜底, helpers/BarJournal)
        self._journal = None        # 惰性创建 BarJournal
        self._j_d1_date = None      # 已落日志的信号周期日期
        self._j_m5_key = None       # 已落日志的盘中键值
        # 交易通道真值源 (P0-3): generated/traders/<id>/rtdata.json
        self._truth = None          # helpers.LiveTruth.ChannelTruth 或 None
        self._truth_probed = False  # 已探测过 (不可用时不反复重试)
        self._truth_retry_ts = 0.0  # 下次允许重探时间 (文件可能后到)
        self._truth_stale_warned = False   # 陈旧真值告警一次 (P2)
        self._truth_alert = {}      # 真值/框架差异告警去重 (code,差额)->ts
        self._truth_check_ts = 0.0  # 周期对账上次执行 (on_tick 驱动)
        self._truth_mismatch_since = None   # 差额起始时间 (升级告警用)
        self._trade_journal = None  # 逐笔成交 (trades.csv) 增量读取器

    @property
    def _use_journal(self) -> bool:
        return bool(self._base.journal_dir)

    def _journal_writer(self):
        if self._journal is None:
            from helpers.BarJournal import BarJournal
            self._journal = BarJournal(self._base.journal_dir)
        return self._journal

    # ------------------------------------------------------------------
    # 交易通道真值源 (P0-3)
    # ------------------------------------------------------------------
    def _truth_source(self):
        """交易通道真值源 (helpers/LiveTruth); 不可用返回 None。

        框架侧持仓/资金都是信号口径 (见 LiveTruth 模块头), 无法识别
        "执行单元卡死但信号仓已达标"的幽灵仓位; 本真值源来自交易通道
        落盘的 rtdata.json (真实券商持仓+账户资金)。
        文件可能在进程启动后才生成 → 每 600s 允许重探一次。
        """
        if self._truth is not None:
            return self._truth
        now = time.time()
        if self._truth_probed and now < self._truth_retry_ts:
            return None
        self._truth_probed = True
        self._truth_retry_ts = now + 600.0
        try:
            from helpers.LiveTruth import ChannelTruth, resolve_truth_path
            path = resolve_truth_path(
                _GROUP_DIR, enabled=getattr(self._base, "truth_json", ""))
            if path:
                self._truth = ChannelTruth(
                    path, max_age=float(
                        getattr(self._base, "truth_max_age", 1800.0)))
        except Exception:
            self._truth = None
        return self._truth

    def _truth_position(self, context, code: str):
        """实际持仓真值: 优先通道真值, 回退框架信号口径。

        Returns: (pos: float, src: str) — src ∈ {"truth", "framework"}

        陈旧真值不参与决策 (P2 实测教训): 部署/回测环境里残留的过期
        rtdata.json 会被当成真实持仓比对, 触发错误的重建/平仓
        (2026-09-15: 回测目录里 22 小时前的样本文件把 cu 基线从
        335% 拉到 76%)。陈旧时回退框架口径并告警一次。

        归属限制 (2026-09-15 生产实测): 券商持仓是**账户级**的, 共享
        账户下同一合约可能含其他策略仓位 (实测 CZCE.TA: 账户 112 vs
        本策略 64), 无法归属 → 默认 `truth_rebuild_mode=alert` 只观测
        告警, 不据此重建/收养; 只有独占账户的部署才设 rebuild。
        """
        fw = float(context.stra_get_position(code))
        t = self._truth_source()
        if t is None:
            return fw, "framework"
        v = t.net_position(code)
        if v is None:
            return fw, "framework"
        if t.stale():
            if not self._truth_stale_warned:
                self._truth_stale_warned = True
                age = t.age()
                context.stra_log_text(
                    f"{self.name()} truth source stale "
                    f"(age={age if age is None else round(age)}s > "
                    f"{t.max_age:.0f}s) → 回退框架信号口径, 不做"
                    f"真值对账/重建", level=3)
                self._alert("truth_stale", f"{t.path} 陈旧, 真值对账已停用")
            return fw, "framework"
        if self._truth_mode == "rebuild":
            return float(v), "truth"
        self._truth_mismatch_alert(context, code, fw, float(v))
        return fw, "framework"

    _TRUTH_CHECK_INTERVAL = 300.0   # 周期对账间隔 (秒)
    _TRUTH_ESCALATE_SEC = 600.0     # 差额持续多久升级为高危告警

    def _periodic_truth_check(self, context):
        """周期性真值/框架仓对账 (不依赖日评估路径, 每 300s 一次)。

        2026-09-17 事故: 交易通道登录失败 → 引擎按止损"平仓"但券商未成交
        (sc 3 手 / m 12 手裸奔), 而差额告警只挂在日评估上, 那两天日评估
        又因会话事件缺失未触发 → 全程静默。现改为 tick 驱动定时检查,
        差额持续 >10 分钟升级为 truth_mismatch_persist 高危告警。
        """
        now = time.time()
        if now - self._truth_check_ts < self._TRUTH_CHECK_INTERVAL:
            return
        self._truth_check_ts = now
        code = self._inst.code
        tv = self._truth_net(code)
        if tv is None:
            return
        fw = float(context.stra_get_position(code))
        if int(round(tv)) == int(round(fw)):
            self._truth_mismatch_since = None
            return
        if self._truth_mismatch_since is None:
            self._truth_mismatch_since = now
        self._truth_mismatch_alert(context, code, fw, float(tv))
        held = now - self._truth_mismatch_since
        if held > self._TRUTH_ESCALATE_SEC:
            self._alert(
                "truth_mismatch_persist",
                f"{code} 账户真值 {tv:g} vs 框架仓 {fw:g} 持续 "
                f"{int(held / 60)} 分钟 — 疑似下单未成交/交易通道故障, "
                f"需人工核对券商持仓")

    @property
    def _truth_mode(self) -> str:
        return str(getattr(self._base, "truth_rebuild_mode", "alert"))

    def _truth_mismatch_alert(self, context, code: str, fw: float,
                              tv: float):
        """alert 模式: 账户真值与框架仓差额只告警 (不重建), 按差额去重"""
        d = int(round(tv)) - int(round(fw))
        if d == 0:
            return
        key = (code, d)
        now = time.time()
        last = self._truth_alert.get(key)
        if last is not None and now - last < self._VETO_LOG_COOLDOWN:
            return
        self._truth_alert[key] = now
        txt = (f"{code} 账户真值 {tv:g} vs 框架仓 {fw:g} (差 {d:+d}) "
               f"— alert 模式不重建, 需人工确认归属")
        context.stra_log_text(f"{self.name()} truth mismatch: {txt}", level=3)
        self._alert("truth_mismatch", txt)

    def _account_available(self):
        """券商口径账户可用资金 (真值源提供); 不可用/文件陈旧 → None。

        共享账户下该值是**扣除其他策略占用后**的真实可用资金, 是
        "资金耗尽被拒单" (2026-09-09) 的防线; 陈旧文件不参与决策。
        """
        t = self._truth_source()
        if t is None or t.stale():
            return None
        return t.available()

    def _log_account_gate(self, context, tag: str, reasons: list,
                          want: int, final: int):
        """账户资金约束的 warn 模式留痕 (veto 模式的缩量/否决走已有日志)"""
        hit = next((r for r in reasons if r.startswith("account_margin(")),
                   None)
        if hit is None:
            return
        mode = getattr(self._risk.cfg, "account_gate_mode", "warn")
        if mode == "warn":
            self._veto_log(
                context, f"accwarn{tag}",
                f"{tag} 账户可用资金偏紧 (warn 模式, 未减手数): {hit}, "
                f"want={want}, final={final}")

    def _bar_row(self, bars, i: int = -1) -> dict:
        """提取第 i 根 K 线为 journal 行; OHLC 不可靠时返回 None"""
        d = self._nd_field(bars, "date")
        if d is None:
            return None
        vals = {}
        for name in ("opens", "highs", "lows", "closes"):
            arr = getattr(bars, name, None)
            try:
                vals[name] = float(arr[i]) if arr is not None else None
            except (TypeError, ValueError, IndexError):
                vals[name] = None
        if any(vals[n] is None for n in ("opens", "highs", "lows",
                                         "closes")):
            return None
        vol_arr = getattr(bars, "volumes", None)
        try:
            vol = float(vol_arr[i]) if vol_arr is not None else 0.0
        except (TypeError, ValueError, IndexError):
            vol = 0.0
        turnover = 0.0
        nd = getattr(bars, "ndarray", None)
        names = getattr(getattr(nd, "dtype", None), "names", None) or ()
        if "turnover" in names:
            try:
                turnover = float(nd["turnover"][i])
            except (TypeError, ValueError, IndexError):
                turnover = 0.0
        t = self._nd_field(bars, "time")
        oi_arr = self._extract_oi(bars)
        try:
            oi = float(oi_arr[i]) if oi_arr is not None else 0.0
            dt = int(d[i])
            tm = int(t[i]) if t is not None else 0
        except (TypeError, ValueError, IndexError):
            return None
        return {"date": dt, "time": tm,
                "open": vals["opens"], "high": vals["highs"],
                "low": vals["lows"], "close": vals["closes"],
                "volume": vol, "turnover": turnover,
                "open_interest": oi}

    def _journal_signal_bar(self, bars):
        """信号周期新完成 bar → journal (首call建基线不写历史)"""
        if not self._use_journal or bars is None:
            return
        date = self._bar_last_date(bars)
        if date is None:
            return
        if self._j_d1_date is None:
            self._j_d1_date = date
            return
        if date == self._j_d1_date:
            return
        row = self._bar_row(bars)
        if row:
            self._journal_writer().append(self._inst.code,
                                          self._inst.period, row)
        self._j_d1_date = date

    def _journal_intrabar_bars(self, mbars, mkey):
        """盘中主K新增段 → journal"""
        if not self._use_journal or mbars is None or mkey is None:
            return
        if self._j_m5_key is None:
            self._j_m5_key = float(mkey[-1])
            return
        writer = None
        for i in range(len(mkey)):
            if mkey[i] <= self._j_m5_key:
                continue
            row = self._bar_row(mbars, i)
            if row:
                if writer is None:
                    writer = self._journal_writer()
                sub_period = getattr(self._base, "intrabar_period", "m5")
                writer.append(self._inst.code, sub_period, row)
        self._j_m5_key = float(mkey[-1])

    # ------------------------------------------------------------------
    # 工厂
    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls, name: str, code: str, config_path: str,
                    risk_mgr: RiskManager = None, notify=None,
                    bt_no_simtick: bool = False):
        from helpers.ConfigLoader import ConfigLoader
        cfg = ConfigLoader.load(config_path)
        for inst in cfg.instruments:
            if inst.code == code:
                return cls(name, inst, cfg.base, risk_cfg=cfg.risk,
                           risk_mgr=risk_mgr, notify=notify,
                           bt_no_simtick=bt_no_simtick)
        raise ValueError(f"{code} 不在 {config_path} 的 instruments 中")

    # ------------------------------------------------------------------
    # 事件推送
    # ------------------------------------------------------------------
    def _safe_mark(self, context, price: float, icon: str, tag: str):
        """图表标记安全封装: 仅 schedule 时段可用 (CtaStraBaseCtx.
        cpp:1978 非 schedule 打 error 并丢弃), on_tick 路径静默跳过"""
        if not self._in_schedule:
            return
        context.add_chart_mark(price, icon, tag)

    def _alert(self, key: str, text: str):
        """关键事件推送 + **本地落盘** (未配置 webhook 也不能静默)。

        2026-09-17 事故教训: 交易通道登录失败后所有告警只走 notifier,
        而生产 notify.enabled=false → 裸奔持仓两天无人知晓; 故此处无条件
        追加一行到 <group>/Logs/alerts.log, 作为最后一道留痕。
        """
        try:
            if self._notify is not None:
                self._notify.send(f"{self.name()}:{key}", text)
        except Exception:
            pass
        try:
            d = os.path.join(_GROUP_DIR, "Logs")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "alerts.log"), "a",
                      encoding="utf-8") as f:
                f.write("%s %s %s | %s\n" % (time.strftime("%F %T"),
                                             self.name(), key, text))
        except OSError:
            pass

    # ------------------------------------------------------------------
    # 子系统访问
    # ------------------------------------------------------------------
    @property
    def _is_dual(self) -> bool:
        return len(self._subs) > 1

    def _sub(self, name: str):
        for s in self._subs:
            if s["name"] == name:
                return s
        return None

    def _combined_target(self) -> int:
        """两子系统的净目标仓位 (框架 set_position 的最终标的)"""
        t = 0
        for s in self._subs:
            e = s["engine"]
            if e.direction != 0 and e.layers:
                sign = 1 if e.direction > 0 else -1
                t += sign * sum(l for l, _ in e.layers)
        return t

    def _first_active_sub(self):
        for s in self._subs:
            if s["engine"].direction != 0:
                return s
        return self._subs[0]

    def _sub_lots(self, sub) -> int:
        e = sub["engine"]
        if e.direction == 0:
            return 0
        return sum(l for l, _ in e.layers)

    def _mark_fill_pending(self, context, key: str, engine):
        """登记"成交均价待回写" (按子系统分槽, 幂等覆盖同槽)。

        同时记录逐笔成交游标 + 本层手数: 回写时优先从 trades.csv 取
        **该层真实成交均价** (P2), 避免"多笔待回写时只有净均价方程 →
        欠定"的问题。
        """
        j = self._trade_journal_source()
        cursor = j.cursor() if j is not None else None
        # 成交确认的真值基线 (增量口径): 登记时刻的账户持仓, 之后只看
        # **增量**是否等于本层手数 — 绝对值在共享账户下含其他策略仓位,
        # 不可用 (2026-09-15 实测: CZCE.TA 账户 112 vs 本策略 64)
        truth0 = self._truth_net(self._inst.code)
        # 同槽覆盖时结转前一笔手数: 同一成交窗口内先确认的手数必须在
        # 逐笔成交里先被消费, 否则末层会取到首笔的成交价 (P2)
        prev = self._fill_pending.get(key)
        carry = 0.0
        if prev is not None and prev.get("cursor") == cursor:
            carry = float(prev.get("skip") or 0.0) + \
                float(prev.get("lots") or 0.0)
            if truth0 is None:
                truth0 = prev.get("truth0")
        self._fill_pending[key] = {
            "engine": engine, "ts": time.time(),
            "fees0": self._get_fees(context),
            "lots": engine.layers[-1][0] if engine.layers else 0,
            "cursor": cursor, "skip": carry, "truth0": truth0}

    def _truth_net(self, code: str):
        """账户真值净仓 (新鲜时); 不可用/陈旧 → None"""
        t = self._truth_source()
        if t is None or t.stale():
            return None
        v = t.net_position(code)
        return None if v is None else float(v)

    def _trade_journal_source(self):
        """逐笔成交读取器 (与真值源同目录 trades.csv); 不可用 → None"""
        if self._trade_journal is not None:
            return self._trade_journal
        t = self._truth_source()
        if t is None or not t.path:
            return None
        try:
            from helpers.LiveTruth import TradeJournal
            p = os.path.join(os.path.dirname(t.path), "trades.csv")
            self._trade_journal = TradeJournal(p)
        except Exception:
            self._trade_journal = None
        return self._trade_journal

    def _journal_layer_price(self, code: str, fp: dict, engine,
                             skip: float = 0.0):
        """本层真实成交均价 (逐笔成交口径); 不可用/未成交齐 → None。

        skip = 同组 (同合约同游标) 前序记录已分配的手数。
        """
        j = self._trade_journal_source()
        if j is None or fp.get("cursor") is None or not engine.layers:
            return None
        lots = fp.get("lots") or engine.layers[-1][0]
        try:
            return j.layer_price(code, fp["cursor"], float(lots),
                                 skip=float(fp.get("skip") or 0.0) + skip)
        except Exception:
            return None

    def _set_n(self, n: float):
        """刷新策略层与各子系统引擎的"最近评估 N" (P0-2 冻结 N 的来源)。

        引擎侧缓存用于: ①confirm_* 缺省 N 回退; ②对账/收养重建单层时
        的止损锚 (close 模式与 intrabar 路径下引擎不经过 on_bar, 不刷新
        则止损判定静默失效)。
        """
        self._last_n = n
        for s in self._subs:
            s["engine"].update_n(n)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def on_init(self, context: CtaContext):
        code = self._inst.code
        p = self._base

        # 盘中止损模式: 主K切换为 intrabar_period, 信号周期(d1/h1)作副K
        if self._inst.intrabar_stop:
            self._main_period = p.intrabar_period
            context.stra_prepare_bars(code, self._main_period,
                                      p.intrabar_cnt, isMain=True)
        else:
            self._main_period = self._inst.period
            context.stra_prepare_bars(code, self._main_period,
                                      p.bar_cnt, isMain=True)

        # trigger 模式: 订阅 tick 驱动盘中触发; 日线主K时另备 m5 副K
        # (实盘日线不闭合 F3 → session_end 用 m5 聚合合成当日日K评估)
        # bt_no_simtick: bt 且无 m5 数据 → 不订 tick/副K, 触发判定走
        # on_bar check_bar, 撮合退化为 bar 收盘口径
        if p.entry_mode == "trigger" and not self._bt_no_simtick:
            context.stra_sub_ticks(code)
            if not self._inst.intrabar_stop:
                context.stra_prepare_bars(code, p.intrabar_period,
                                          p.intrabar_cnt, isMain=False)
        elif p.entry_mode == "trigger":
            context.stra_log_text(
                f"{self.name()} bt_no_simtick: trigger 判定走 "
                f"check_bar, 撮合=bar收盘口径 (无 m5 模拟tick)")

        comm = context.stra_get_comminfo(code)
        self._volscale = float(comm.volscale)
        self._pricetick = float(comm.pricetick)

        # === 图表注册 ===
        context.set_chart_kline(code, self._inst.period)
        idx = f"Turtle_{self.name()}"
        self._idx = idx
        context.register_index(idx, idxType=1)      # 副图: ATR + 持仓
        context.register_index_line(idx, "atr_n", lineType=0)
        context.register_index_line(idx, "position", lineType=1)
        context.add_index_baseline(idx, "position", 0.0)
        ch_idx = f"TurtleCh_{self.name()}"
        self._ch_idx = ch_idx
        context.register_index(ch_idx, idxType=0)   # 主图叠加: 通道 + 止损
        context.register_index_line(ch_idx, "entry_upper", lineType=0)
        context.register_index_line(ch_idx, "entry_lower", lineType=0)
        context.register_index_line(ch_idx, "stop", lineType=0)

        # === 状态恢复 (v4: +trigger武装; v3: 双子系统+去重键; v2/v1 兼容) ===
        raw = context.user_load_data(f"{self.name()}_engine", "", str)
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("v") in (3, 4):
                    for nm, snap in (data.get("subs") or {}).items():
                        sub = self._sub(nm)
                        if sub:
                            sub["engine"].restore(snap)
                    self._d1_date = data.get("d1_date")
                    self._m5_key = data.get("m5_key")
                elif isinstance(data, dict) and data.get("v") == 2:
                    self._subs[0]["engine"].restore(data["engine"])
                    self._d1_date = data.get("d1_date")
                    self._m5_key = data.get("m5_key")
                else:
                    self._subs[0]["engine"].restore(data)
                if p.entry_mode == "trigger":
                    # 旧版快照无武装状态 → 按已有 N/通道重建 (幂等)
                    for s in self._subs:
                        if s["engine"].armed.get("long") is None and \
                                s["engine"].armed.get("stop") is None:
                            s["engine"].rearm()
                    # 孤儿 pending 清除: pending=触发决策已发出待成交
                    # 确认; 进程重启后其下落不可知 (若决策在策略层丢失
                    # 则从未下单 —— 2026-09-07 实测: 9/4 旧代码
                    # _last_n=NaN 静默丢弃 add 决策, pending 挂起后经
                    # 快照持久化/恢复, 永久吞掉后续全部触发, sc 盘中
                    # 709 穿越 add 位 688.32 无响应)。清除后武装位保留,
                    # 价格仍在位上方时下一 tick 立即补票触发; 若决策
                    # 实际已成交而 confirm 前崩溃, 下方持仓对账与运行时
                    # reconcile 按券商实际持仓兜底。
                    for s in self._subs:
                        dropped = s["engine"].clear_pending()
                        if dropped is not None:
                            context.stra_log_text(
                                f"{self.name()}[{s['name']}] drop orphan "
                                f"pending {dropped} on restart "
                                f"(re-trigger if price still beyond level)",
                                level=2)
                            self._alert("orphan_pending",
                                        f"drop orphan pending {dropped} "
                                        f"on restart")
                    # _last_n 同步: 引擎 _n 已随快照恢复, 策略层缓存
                    # 不同步则重启后~下次日评估间所有触发决策被
                    # isfinite(_last_n) 闸静默丢弃 (2026-09-04 夜盘实测:
                    # sc 穿越 add 位无响应)
                    for s in self._subs:
                        en = s["engine"]._n
                        if en is not None and np.isfinite(en):
                            self._last_n = en
                            break
                context.stra_log_text(
                    f"{self.name()} restored: dir={self._engine.direction}, "
                    f"layers={len(self._engine.layers)}")
            except (json.JSONDecodeError, TypeError) as e:
                context.stra_log_text(
                    f"{self.name()} state restore failed: {e}", level=2)

        # 持仓状态对账: 框架有持仓但引擎空仓 → 按单层重建到首子系统
        # 口径: avgpx 取绝对值 (框架空头 avgpx 为负, avgpx×pos=成本>0;
        # 未取绝对值会把层价写成负数 → 止损位落到价格另一侧, 下一根
        # bar 立刻以噪声价"止损"平掉真实持仓)
        # 持仓量优先取交易通道真值 (P0-3): 框架口径是信号账, 手工干预/
        # 执行单元卡死时与券商实际持仓不一致
        t = self._truth_source()
        if t is not None:
            context.stra_log_text(
                f"{self.name()} truth source: {t.path} "
                f"(mode={self._truth_mode}, age="
                f"{t.age() if t.age() is None else round(t.age(), 1)}s, "
                f"available={t.available()})")
        pos, pos_src = self._truth_position(context, code)
        if pos != 0 and self._combined_target() == 0:
            avgpx = abs(float(context.stra_get_position_avgpx(code) or 0))
            if not np.isfinite(avgpx) or avgpx <= 0:
                px = context.stra_get_price(code)
                avgpx = abs(float(px)) if px and np.isfinite(px) else 0.0
            if avgpx > 0:
                self._subs[0]["engine"].adopt_position(
                    1 if pos > 0 else -1, abs(int(pos)), avgpx)
                context.stra_log_text(
                    f"{self.name()} adopt orphan position {pos}@{avgpx}, "
                    f"rebuilt as single layer (src={pos_src})", level=2)
            else:
                context.stra_log_text(
                    f"{self.name()} orphan position {pos} adopt skipped: "
                    f"avgpx/price unavailable (待运行时对账重建)", level=3)

        # 组合风控账本恢复: RiskManager._book 进程内存态不持久化,
        # 重启后为空 → 持仓期间组合总敞口/单位数约束被低估
        # (2026-09-04 审计发现)。按恢复/收养的引擎持仓重建记账。
        tot_lots = 0
        wsum = 0.0
        dirn = 0
        n0 = None
        risk0 = 0.0
        for s in self._subs:
            e = s["engine"]
            if e.direction == 0 or not e.layers:
                continue
            dirn = e.direction
            for l, pr in e.layers:
                tot_lots += l
                wsum += l * pr
            risk0 += e.booked_risk(self._volscale or 1.0,
                                   p.stop_atr_mult,
                                   stop=e.stop_anchor())
            if n0 is None and e._n is not None and np.isfinite(e._n):
                n0 = e._n
        if tot_lots > 0 and not self._risk.has_position(code):
            self._risk.on_rebuilt(
                code, dirn, tot_lots, wsum / tot_lots,
                n0 or 1.0, self._volscale or 1.0,
                p.stop_atr_mult, self._inst.group,
                risk=risk0 if risk0 > 0 else None)
            context.stra_log_text(
                f"{self.name()} risk book restored: dir={dirn} "
                f"lots={tot_lots} avg={wsum / tot_lots:.2f} "
                f"risk={risk0:.0f} (stop caliber)")

        context.stra_log_text(
            f"StraTurtle inited: {code} {self._inst.period} "
            f"{self._inst.system} units={self._inst.units} "
            f"dir_gate={self._inst.direction}")

        # F1 执行单元预热 (2026-09-10): wtpy 0.9.9.3 实测盘中新建的
        # MiniImpactExecUnit 大概率 tick 断供卡死 ("No lastest tick data,
        # execute later" 后永久静默, 订单不到柜台 → cu/al 卡死一天、
        # MA 幽灵加仓 4 手); 启动早期 (channel ready 事件前后) 创建的
        # 单元工作正常。on_init 同值 set_position 强制预建执行单元
        # (diff=0 不下单), 保证止损/加仓触发时单元已存活。
        # bt 跳过 (无执行单元概念, 且保持回测基线口径)。
        if not getattr(context, "is_backtest", False):
            try:
                cur = int(round(context.stra_get_position(code)))
                context.stra_set_position(code, cur, "PreWarm")
                context.stra_log_text(
                    f"{self.name()} exec unit pre-warmed (target={cur})")
            except Exception as e:
                context.stra_log_text(
                    f"{self.name()} exec prewarm failed: {e}", level=2)

    def on_calculate(self, context: CtaContext):
        self._in_schedule = True
        try:
            if self._inst.intrabar_stop:
                self._on_calc_intrabar(context)
            else:
                self._on_calc_daily(context)
        finally:
            self._in_schedule = False

    def _on_calc_daily(self, context: CtaContext):
        code = self._inst.code
        p = self._base
        bars = context.stra_get_bars(code, self._inst.period, p.bar_cnt,
                                     isMain=True)
        need = self._need_bars()
        if bars is None or len(bars) < need:
            return

        closes = bars.closes
        highs = bars.highs
        lows = bars.lows

        # trigger 模式 (回测日线粒度): 先用当根 OHLC 做触碰仿真
        # (按昨日武装位), 再进入日线评估 (武装次日)。
        # 触碰仿真按 bar 日期去重: 重启重放同一根信号 bar 时不得二次
        # 消费 (否则恢复的 add 武装位会对同根 bar 重复触发, 原测试靠
        # _last_n=NaN 意外挡住的真问题, 2026-09-04 修复 _last_n 同步后暴露)
        if p.entry_mode == "trigger":
            d_cur = self._bar_last_date(bars)
            if d_cur is not None and self._d1_date is not None \
                    and int(d_cur) > int(self._d1_date):
                # 新交易日: 上一交易日挂起的 pending 必然已死 (正常流程
                # pending 在同一 tick/bar 内被 confirm/reject 消费,
                # 跨日存活 = 决策丢失), 清除后当根触碰仿真立即可用
                self._drop_stale_pending(context, f"bar_eval {d_cur}")
            if d_cur is None or self._d1_date is None \
                    or int(d_cur) > int(self._d1_date):
                opens = getattr(bars, "opens", None)
                o = float(opens[-1]) if opens is not None \
                    else float(closes[-1])
                self._run_trigger_bar(context, o, float(highs[-1]),
                                      float(lows[-1]), float(closes[-1]))

        atr, n, chan = self._eval_core(
            context, bars, highs, lows, closes,
            float(highs[-1]), float(lows[-1]), float(closes[-1]),
            float(closes[-2]), with_oi=True)
        if n is None:
            return

        # K线旁路日志: 新完成信号bar幂等落盘 (崩溃窗口兜底)
        self._journal_signal_bar(bars)

        # trigger 模式: 记录已评估日期, 防 on_session_end 合成路径重评估
        if p.entry_mode == "trigger":
            d = self._bar_last_date(bars)
            if d is not None:
                self._d1_date = d

        self._post_calculate(context, closes, n, chan)

    def _eval_core(self, context: CtaContext, bars, highs, lows, closes,
                   h: float, l: float, c: float, prev_close: float,
                   with_oi: bool, synth=None):
        """三评估路径共用的评估骨架 (P1-8: on_calc_daily /
        _session_eval_daily / _catchup_eval 此前各持一份近重复副本 —
        历史上这类副本曾真实漂移: session 路径的 _reanchor_on_roll
        曾漏挂导致实盘换月漂移永不修正, 故收敛为单一骨架)。

        顺序: 指标 → N 刷新 → 窗口日志 → 换月对账 → 过滤器上下文 →
        引擎 on_bar → 执行 (单系统逐决策 / 双系统净额批量)。
        with_oi: 是否从 bars 取成交量/持仓量构造过滤上下文
        (session_end 合成路径无当日 m5 明细, 传 False)。
        返回 (atr, n, chan); n 无效 → (None, None, None), 调用方须
        直接返回, 不得继续任何后续步骤。
        """
        atr, n, chan = self._calc_indicators(highs, lows, closes)
        if n is None:
            return None, None, None
        self._set_n(n)
        self._log_eval_window(context, bars, closes, n, synth)
        self._reanchor_on_roll(context)
        fctx = FilterContext(
            code=self._inst.code, price=float(c), n=n, n_series=atr,
            highs=highs, lows=lows, closes=closes,
            volumes=getattr(bars, "volumes", None) if with_oi else None,
            open_interests=self._extract_oi(bars) if with_oi else None)
        if self._is_dual:
            dec_map = {s["name"]: s["engine"].on_bar(
                h, l, c, prev_close, n, chan[s["name"]])
                for s in self._subs}
            self._execute_dual(context, dec_map, n, fctx)
        else:
            for dec in self._engine.on_bar(h, l, c, prev_close, n,
                                           chan[self._inst.system]):
                self._execute(context, dec, n, fctx)
        return atr, n, chan

    def _calc_indicators(self, highs, lows, closes):
        """ATR + 各子系统 Donchian 通道。

        P2: 每个周期只算一次 (原实现重复计算 — `_mk` 内每次都重算
        fail-safe 通道, 双系统分支里 s2_exit 也算了两次)。
        未加跨 bar 记忆化: 实测单次 512µs (瓶颈 scipy lfilter),
        12 品种 × 288 根 m5 ≈ 1.8s CPU/日, 收益低于"缓存键错配导致
        指标陈旧"的风险 (取数窗口是定长滚动数组, 长度+末值不足以唯一
        标识一根 bar)。

        Returns:
            (atr序列, N, {子系统名: channels dict}); N 无效返回 (None,..)
        """
        p = self._base
        atr = wilder_atr(highs, lows, closes, p.atr_period)
        n = float(atr[-1])
        if not np.isfinite(n):
            return None, None, None

        cache = {}

        def ch(period: int):
            """Donchian (含当根包络前的前 N 根), 同周期只算一次"""
            if period not in cache:
                cache[period] = donchian(highs, lows, period)
            return cache[period]

        def _mk(eu, el, xu, xl, fu, fl):
            return {
                'entry_upper': float(eu[-1]), 'entry_lower': float(el[-1]),
                'exit_upper': float(xu[-1]), 'exit_lower': float(xl[-1]),
                'fs_upper': float(fu[-1]), 'fs_lower': float(fl[-1]),
                'prev_entry_upper': float(eu[-2]),
                'prev_entry_lower': float(el[-2]),
                'prev_fs_upper': float(fu[-2]),
                'prev_fs_lower': float(fl[-2]),
            }

        chan = {}
        for sub in self._subs:
            # 每子系统自己的窗口 (多速度); 缺省沿用 S1/S2 全局口径
            dflt = (p.s2_entry, p.s2_exit) if sub["name"] == "S2" \
                else (p.s1_entry, p.s1_exit)
            en = int(sub.get("entry") or dflt[0])
            ex = int(sub.get("exit") or dflt[1])
            fsp = int(sub.get("fs") or p.s2_entry)
            a, b = ch(en)
            c2, d2 = ch(ex)
            f1, f2 = ch(fsp)
            chan[sub["name"]] = _mk(a, b, c2, d2, f1, f2)
        return atr, n, chan

    def _need_bars(self) -> int:
        """暖机所需信号周期 bar 数 (覆盖各子系统窗口 + ATR + 2 根余量)"""
        p = self._base
        need = max(p.s2_entry, p.s1_entry, p.s2_exit, p.atr_period)
        for s in self._subs:
            for k in ("entry", "exit", "fs"):
                v = s.get(k)
                if v:
                    need = max(need, int(v))
        return need + 2

    def _post_calculate(self, context: CtaContext, closes, n: float,
                        chan: dict, mark: bool = True):
        """对账 → 图表 → 持久化 (daily/intrabar 共用收尾)

        图表段仅 schedule 时段可用 (C++ set_index_value 非 schedule 打
        error 丢弃, CtaStraBaseCtx:2036) — mark 再叠 _in_schedule 闸,
        on_tick/session_end 路径自动跳过。"""
        code = self._inst.code
        self._reconcile(context, closes)
        self._reanchor_actual_fill(context)
        self._dump_fund_live(context)
        pos = context.stra_get_position(code)
        first = self._first_active_sub()
        channels = chan[first["name"]]
        if mark and self._in_schedule:
            context.set_index_value(self._idx, "atr_n", n)
            context.set_index_value(self._idx, "position", float(pos))
            context.set_index_value(self._ch_idx, "entry_upper",
                                    channels['entry_upper'])
            context.set_index_value(self._ch_idx, "entry_lower",
                                    channels['entry_lower'])
            fe = first["engine"]
            if fe.direction != 0 and fe.layers:
                # 双系统图表取首活跃子系统止损 (净仓位图元为合并口径)
                context.set_index_value(self._ch_idx, "stop",
                                        fe.stop_price())
        snap = {"v": 4,
                "subs": {s["name"]: s["engine"].snapshot()
                         for s in self._subs},
                "d1_date": self._d1_date, "m5_key": self._m5_key}
        context.user_save_data(f"{self.name()}_engine", json.dumps(snap))

    def _dump_fund_live(self, context: CtaContext):
        """日内资金旁路落盘 (P2-11): 框架 fund.csv 只在 session_end 日切
        时写入, 盘中权益不可见 (2026-09-18 sc 止损当日费用次日才见)。
        这里每 ≥60s 追加一行到 generated/outputs/<name>/fund_live.csv,
        纯旁路: 写失败只记日志, 绝不影响交易。"""
        now = time.time()
        if now - self._fund_live_ts < 60.0:
            return
        try:
            import datetime as _dt
            outdir = os.path.join(_GROUP_DIR, "generated", "outputs",
                                  self.name())
            os.makedirs(outdir, exist_ok=True)
            path = os.path.join(outdir, "fund_live.csv")
            new = not os.path.exists(path)
            pnl = context.stra_get_fund_data(0)
            fee = self._get_fees(context)
            pos = context.stra_get_position(self._inst.code)
            px = context.stra_get_price(self._inst.code)
            with open(path, "a", encoding="utf-8") as f:
                if new:
                    f.write("date,time,net_pnl,fee,position,price\n")
                f.write(_dt.datetime.now().strftime("%Y%m%d,%H%M%S")
                        + f",{pnl if isinstance(pnl, (int, float)) else ''}"
                        f",{fee if fee is not None else ''}"
                        f",{pos},"
                        f"{px if px is not None and px == px else ''}\n")
            self._fund_live_ts = now
        except Exception as e:
            context.stra_log_text(
                f"{self.name()} fund_live dump failed: {e}", level=2)

    # ------------------------------------------------------------------
    # 盘中止损路径 (P1): 主K=intrabar_period 触发; 信号周期仅取完成日
    # ------------------------------------------------------------------
    def _on_calc_intrabar(self, context: CtaContext):
        code = self._inst.code
        p = self._base
        mbars = context.stra_get_bars(code, p.intrabar_period,
                                      p.intrabar_cnt, isMain=True)
        bars = context.stra_get_bars(code, self._inst.period, p.bar_cnt)
        need = self._need_bars()
        if mbars is None or len(mbars) == 0 or bars is None \
                or len(bars) < need:
            return

        closes = bars.closes
        highs = bars.highs
        lows = bars.lows
        atr, n, chan = self._calc_indicators(highs, lows, closes)
        if n is None:
            return
        self._set_n(n)

        mkey = self._bar_keys(mbars)
        ddate = self._bar_last_date(bars)

        # 基线/去重判定相互独立: 任一键值体系缺失只跳过对应能力
        can_scan = False
        if mkey is not None:
            if self._m5_key is None:
                self._m5_key = float(mkey[-1])    # 首次建基线不回放
            else:
                can_scan = True
        can_signal = False
        if ddate is not None:
            if self._d1_date is None:
                self._d1_date = ddate             # 首次建基线
            else:
                can_signal = True

        self._reanchor_on_roll(context)

        # 1) 盘中段: trigger 模式逐根做触碰仿真(含止损/出场/加仓/入场);
        #    close 模式扫描 2N 止损
        if can_scan:
            if p.entry_mode == "trigger":
                self._run_trigger_bars(context, mbars, mkey)
            else:
                self._scan_intrabar_stop(context, mbars, mkey)
            self._journal_intrabar_bars(mbars, mkey)
            self._m5_key = float(mkey[-1])

        # 2) 日线信号: 信号周期出现新完成 bar 才评估一次 (日期去重)
        if can_signal and ddate != self._d1_date:
            self._drop_stale_pending(context, f"bar_eval {ddate}")
            self._d1_date = ddate
            fctx = FilterContext(
                code=code, price=float(closes[-1]), n=n, n_series=atr,
                highs=highs, lows=lows, closes=closes,
                volumes=getattr(bars, "volumes", None),
                open_interests=self._extract_oi(bars))
            last_h, last_l, last_c = float(highs[-1]), float(lows[-1]), \
                float(closes[-1])
            prev_c = float(closes[-2])
            if self._is_dual:
                dec_map = {s["name"]: s["engine"].on_bar(
                    last_h, last_l, last_c, prev_c, n, chan[s["name"]])
                    for s in self._subs}
                self._execute_dual(context, dec_map, n, fctx)
            else:
                decisions = self._engine.on_bar(
                    last_h, last_l, last_c, prev_c, n,
                    chan[self._inst.system])
                for dec in decisions:
                    self._execute(context, dec, n, fctx)

        # K线旁路日志 (与决策去重独立, 幂等)
        self._journal_signal_bar(bars)

        self._post_calculate(context, closes, n, chan)

    def _scan_intrabar_stop(self, context: CtaContext, mbars, mkey):
        """扫描新增主K段是否触及各子系统 2N 止损 → 即时离场 (净仓调整)"""
        actives = [s for s in self._subs
                   if s["engine"].direction != 0 and s["engine"].layers]
        if not actives or mkey is None:
            return
        n = self._last_n
        if not np.isfinite(n):
            return
        prev_key = self._m5_key
        highs = np.asarray(mbars.highs, dtype=float)
        lows = np.asarray(mbars.lows, dtype=float)
        times = self._nd_field(mbars, "time")
        code = self._inst.code
        hits = []   # (sub, stop_px, bar_idx)
        for i in range(len(mkey)):
            if mkey[i] <= prev_key:
                continue
            for sub in actives:
                if any(h[0] is sub for h in hits):
                    continue
                e = sub["engine"]
                # 止损价取该层冻结 N (P0-2); actives 已保证 layers 非空
                sp = e.stop_price()
                if not np.isfinite(sp):
                    continue
                hit = (e.direction > 0 and lows[i] <= sp) or \
                      (e.direction < 0 and highs[i] >= sp)
                if hit:
                    hits.append((sub, sp, i))
            if len(hits) == len(actives):
                break
        if not hits:
            return
        for sub, sp, i in hits:
            tstr = f"{int(times[i])}" if times is not None else "?"
            old = self._sub_lots(sub) * (1 if sub["engine"].direction > 0
                                         else -1)
            sub["engine"].confirm_exit(sp)
            self._safe_mark(context, sp, "exit", self.name())
            context.stra_log_text(
                f"{self.name()}[{sub['name']}] intrabar exit "
                f"{old}@{sp:.2f} (stop touched @m5 {tstr})", level=1)
        new_t = self._combined_target()
        cur = int(round(context.stra_get_position(code)))
        if new_t != cur:
            context.stra_set_position(code, new_t, "ExitI")
        self._sync_risk_book(n)

    # ------------------------------------------------------------------
    # trigger 模式 (方案C): 盘中触发执行路由
    # 信号=价格穿越武装位 (tick/bar); 过滤器于武装时(日评估)前置裁决;
    # 风控于触发时复核当前权益; 否决 → engine.reject_pending 当日撤武装
    # ------------------------------------------------------------------
    def _run_trigger_bar(self, context: CtaContext, o: float, h: float,
                         l: float, c: float):
        """单根 bar 触碰仿真 (bt 日线/m5 路径)"""
        if not np.isfinite(self._last_n):
            return
        for sub in self._subs:
            decs = sub["engine"].check_bar(o, h, l, c)
            if os.environ.get("WT_TRIG_DEBUG") == "1":
                context.stra_log_text(
                    f"[trigdbg] {sub['name']} d={context.stra_get_date()} "
                    f"OHLC={o:.0f}/{h:.0f}/{l:.0f}/{c:.0f} "
                    f"armed={sub['engine'].armed} "
                    f"decs={[d.get('decision') for d in decs]}")
            self._exec_trigger_decs(context, sub, decs)

    def _run_trigger_bars(self, context: CtaContext, mbars, mkey):
        """新增主K段逐根触碰仿真 (trigger 模式的 m5 主K路径)"""
        prev_key = self._m5_key
        opens = getattr(mbars, "opens", None)
        highs = mbars.highs
        lows = mbars.lows
        closes = mbars.closes
        for i in range(len(mkey)):
            if mkey[i] <= prev_key:
                continue
            o = float(opens[i]) if opens is not None else float(closes[i])
            self._run_trigger_bar(context, o, float(highs[i]),
                                  float(lows[i]), float(closes[i]))

    def on_tick(self, context: CtaContext, stdCode: str, newTick):
        """实盘 tick 驱动触发 (trigger 模式)"""
        if self._base.entry_mode != "trigger" or stdCode != self._inst.code:
            return
        # 新交易日自愈评估: session_end 未触发(实盘收盘后无 tick,
        # ticker 兜底要求行情时间推进过收盘点, 白盘 15:00 后无行情
        # → 1515 永不触发) 或盘后启动时, 首个 tick 检测 his 末根
        # 未评估则立即评估武装 (每日至多检查一次)
        tdate = context.stra_get_tdate()
        if tdate and self._catchup_date != int(tdate):
            self._catchup_date = int(tdate)
            self._catchup_eval(context)
        px = context.stra_get_price(stdCode)
        if px is None or px != px or px <= 0:
            return
        self._detect_limit_lock(context, stdCode, float(px))
        self._periodic_truth_check(context)   # 定时真值对账 (独立于日评估)
        self._reanchor_actual_fill(context)   # 分批窗口闭合即回写均价
        fired = False
        for sub in self._subs:
            decs = sub["engine"].on_price(float(px))
            if decs:
                fired = True
            self._exec_trigger_decs(context, sub, decs)
        if fired:
            self._persist(context)   # 触发事件即时落快照

    def _detect_limit_lock(self, context: CtaContext, code: str,
                           px: float):
        """涨跌停锁死疑似检测 (P2-10): 价格钉住 + 存在未闭合成交窗口。

        有界实现 (单一信号): tick 价 pinned 超过 base.limit_lock_secs
        且仍有 fill_pending 时判定。经典海龟规程: 锁死板不追单 → 置位
        期间 veto **加仓**; 出场/止损不受任何影响; 入场照常 (未到柜台
        的部分由 ghost_fill 自愈裁剪)。价格恢复移动即清除。
        注意: 无 tick 流时不触发 (也不误判), 故仅是"锁死"的可观测
        代理之一, 与 fill_timeout/ghost_fill 告警互补。
        """
        secs = int(self._base.limit_lock_secs)
        if secs <= 0:
            return
        now = time.time()
        pin = self._pin_price
        if pin is None or pin[0] != px:
            self._pin_price = [px, now]
            if self._limit_lock.pop(code, None):
                context.stra_log_text(
                    f"{self.name()} limit-lock cleared: price moving again",
                    level=2)
            return
        if code in self._limit_lock or not self._fill_pending:
            return
        pinned_for = now - pin[1]
        if pinned_for < secs:
            return
        self._limit_lock[code] = True
        context.stra_log_text(
            f"{self.name()} limit-lock suspected: {px} pinned "
            f"{pinned_for:.0f}s, {len(self._fill_pending)} fill pending "
            f"— adds suspended, exits unaffected", level=2)
        self._alert("limit_lock",
                    f"{code} {px} pinned {pinned_for:.0f}s, "
                    f"{len(self._fill_pending)} fill pending")

    def _exec_trigger_decs(self, context: CtaContext, sub: dict,
                           decs: list):
        """触发决策执行: 入场/加仓已过武装时过滤, 触发时只过风控闸"""
        if not decs:
            return
        if not np.isfinite(self._last_n):
            # 重启后~下次日评估的窗口外不应发生 (快照已恢复 _n → _last_n
            # 在 on_init 同步); 仍兜底告警而非静默丢弃
            context.stra_log_text(
                f"{self.name()} trigger decisions dropped: "
                f"N unavailable (restart pre-eval window)", level=2)
            self._alert("trigger_drop", "trigger decisions dropped: "
                        "N unavailable (restart pre-eval window)")
            return
        for dec in decs:
            dec["prefiltered"] = True      # 武装时已裁决, 免重复过滤
        if self._is_dual:
            self._execute_dual(context, {sub["name"]: decs}, self._last_n)
        else:
            for dec in decs:
                self._execute(context, dec, self._last_n)

    def _on_arm(self, context: CtaContext, engine, fctx):
        """武装落地: 过滤链对入场方向逐一裁决, 否决方向当日解除武装"""
        for d, lv in engine.entry_levels().items():
            if lv is None:
                continue
            dec = {"price": lv, "direction": d}
            if not self._run_filter_chain(context, d, dec, fctx):
                engine.disarm_entry(d)

    def _persist(self, context: CtaContext):
        """轻量持久化 (触发事件后): 仅引擎快照+去重键"""
        snap = {"v": 4,
                "subs": {s["name"]: s["engine"].snapshot()
                         for s in self._subs},
                "d1_date": self._d1_date, "m5_key": self._m5_key}
        context.user_save_data(f"{self.name()}_engine", json.dumps(snap))

    @staticmethod
    def _nd_field(bars, field):
        """防御式读取 K 线结构化字段 (WtNpKline.ndarray / 测试桩属性)"""
        nd = getattr(bars, "ndarray", None)
        if nd is not None and field in (
                getattr(nd.dtype, "names", None) or ()):
            return nd[field]
        return getattr(bars, field, None)

    def _bar_keys(self, bars):
        """单调递增 bar 键值数组 (date*1e6 + time%1e6); 无法构造返回 None"""
        d = self._nd_field(bars, "date")
        t = self._nd_field(bars, "time")
        if d is None or t is None or len(d) != len(t):
            return None
        try:
            d64 = np.asarray(d, dtype=np.int64)
            t64 = np.asarray(t, dtype=np.int64)
        except (TypeError, ValueError):
            return None
        return d64 * 1000000 + (t64 % 1000000)

    def _log_eval_window(self, context, bars, closes, n, synth=None):
        """打印参与本次日评估的数据窗口 (排障用)。

        2026-09-16 实测教训: HOT 日线文件冻结在 09-01, 策略的 N/20/55 日
        通道一直用陈旧窗口, 但日志里完全看不出来 — 只能靠真实合约文件的
        日期反推才定位到。故每次日评估固定打印窗口根数与首末日期 (含当日
        合成 bar), 后续任何"数据不新"问题都能一眼看出。
        """
        try:
            context.stra_log_text(
                f"{self.name()} eval window: {len(closes)} 根 "
                f"[{self._bar_first_date(bars)}~{self._bar_last_date(bars)}]"
                f" synth={'yes' if synth is not None else 'no'} "
                f"n={n:.2f}")
        except Exception:      # 日志失败不影响评估
            pass

    @staticmethod
    def _bar_first_date(bars):
        d = StraTurtle._nd_field(bars, "date")
        if d is None or len(d) == 0:
            return None
        try:
            return int(d[0])
        except (TypeError, ValueError):
            return None

    def _bar_last_date(self, bars):
        d = self._nd_field(bars, "date")
        if d is None or len(d) == 0:
            return None
        try:
            return int(d[-1])
        except (TypeError, ValueError):
            return None

    def on_session_end(self, context: CtaContext, curTDate: int):
        if self._combined_target() != 0:
            subs = ",".join(
                f"{s['name']}:dir{s['engine'].direction}"
                f"x{self._sub_lots(s)}"
                for s in self._subs
                if s["engine"].direction != 0)
            msg = (f"session end: pos="
                   f"{context.stra_get_position(self._inst.code)} ({subs})")
            context.stra_log_text(f"{self.name()} {msg}")
            self._alert("session", msg)
        # trigger 模式: 实盘日线永不闭合(F3) → session end 用当日 m5
        # 聚合合成日K, 做收盘日线评估并武装次日触发位
        if self._base.entry_mode == "trigger":
            self._session_eval_daily(context, curTDate)

    def _drop_stale_pending(self, context: CtaContext, tag: str):
        """日评估前清除跨日 stale pending: pending=触发决策已发出待
        成交确认, 挂起超过一个交易日意味着决策已在策略层丢失 (如
        N 缺失窗口静默丢弃) 或订单状态不可知; 保留武装位, 清除后
        新一轮评估/触发照常进行 (与 on_init 孤儿 pending 清除同源)"""
        for s in self._subs:
            dropped = s["engine"].clear_pending()
            if dropped is not None:
                context.stra_log_text(
                    f"{self.name()}[{s['name']}] drop stale pending "
                    f"{dropped} @ {tag} (decision lost in-flight)",
                    level=2)
                self._alert("stale_pending",
                            f"drop stale pending {dropped} @ {tag}")

    def _session_eval_daily(self, context: CtaContext, curTDate: int):
        """trigger 模式收盘评估: m5 聚合当日日K → 指标 → on_bar 武装"""
        code = self._inst.code
        p = self._base
        if self._d1_date is not None and int(self._d1_date) == int(curTDate):
            return                               # bt 日线到达路径已评估
        self._drop_stale_pending(context, f"session_eval {curTDate}")
        bars = context.stra_get_bars(code, self._inst.period, p.bar_cnt)
        need = self._need_bars()
        # need-1: 本路径把当日 m5 聚合出的合成日K追加到 his 之后 (下方
        # synth), 有效长度 = len(bars)+1; 与 _catchup_eval 的 need 差异
        # 是刻意的 (那条路径不追加合成bar)
        if bars is None or len(bars) < need - 1:
            return
        # 当日 m5 聚合 (his 无当日, 见 DATA_OPS F3)
        synth = None
        mbars = context.stra_get_bars(code, p.intrabar_period,
                                      p.intrabar_cnt)
        if mbars is not None and len(mbars) > 0:
            dates = self._nd_field(mbars, "date")
            if dates is not None:
                idx = [i for i in range(len(dates))
                       if int(dates[i]) == int(curTDate)]
                if idx:
                    opens = getattr(mbars, "opens", None)
                    synth = (
                        float(opens[idx[0]]) if opens is not None
                        else float(mbars.closes[idx[0]]),
                        float(np.max(np.asarray(mbars.highs)[idx])),
                        float(np.min(np.asarray(mbars.lows)[idx])),
                        float(mbars.closes[idx[-1]]))
        closes = np.asarray(bars.closes, dtype=float)
        highs = np.asarray(bars.highs, dtype=float)
        lows = np.asarray(bars.lows, dtype=float)
        prev_close = float(closes[-1])
        if synth is not None:
            o, h, l, c = synth
            highs = np.append(highs, h)
            lows = np.append(lows, l)
            closes = np.append(closes, c)
        else:
            # 无当日 m5 (异常兜底): 以 his 末根充当, 评估幂等
            o, h, l, c = float(highs[-1]), float(highs[-1]), \
                float(lows[-1]), prev_close
        atr, n, chan = self._eval_core(context, bars, highs, lows, closes,
                                       h, l, c, prev_close, with_oi=False,
                                       synth=synth)
        if n is None:
            return
        self._d1_date = int(curTDate)
        # 当日合成日K落 journal (崩溃窗口兜底)
        if synth is not None and self._use_journal:
            self._journal_writer().append(
                code, "d", {"date": int(curTDate), "time": 1500,
                            "open": o, "high": h, "low": l, "close": c})
        self._post_calculate(context, closes, n, chan)

    def _catchup_eval(self, context: CtaContext):
        """新交易日自愈评估 (仅实盘 trigger 日线): 用 his 数据
        (不含当日) 补做日线评估并武装。与 session_end 的 synth=None
        兜底分支同口径; bt 不启用 (bt 日K闭合, on_bar 路径完整)。"""
        if self._base.entry_mode != "trigger" or self._inst.intrabar_stop:
            return
        if getattr(context, "is_backtest", False):
            return
        code = self._inst.code
        p = self._base
        bars = context.stra_get_bars(code, self._inst.period, p.bar_cnt)
        need = self._need_bars()
        # 本路径只用 his (不追加合成bar), 故要求完整 need 根
        if bars is None or len(bars) < need:
            return
        last_date = self._bar_last_date(bars)
        if last_date is None:
            return
        if self._d1_date is not None and int(self._d1_date) >= int(last_date):
            return                        # his 末根已评估过
        self._drop_stale_pending(context, f"catchup {last_date}")
        highs = np.asarray(bars.highs, dtype=float)
        lows = np.asarray(bars.lows, dtype=float)
        closes = np.asarray(bars.closes, dtype=float)
        atr, n, chan = self._eval_core(
            context, bars, highs, lows, closes,
            float(highs[-1]), float(lows[-1]), float(closes[-1]),
            float(closes[-2]), with_oi=True)
        if n is None:
            return
        self._d1_date = int(last_date)
        context.stra_log_text(
            f"{self.name()} catchup eval: his last={last_date} armed "
            f"(session_end 缺失/盘后启动自愈)")
        self._post_calculate(context, closes, n, chan, mark=False)

    def on_backtest_end(self, context: CtaContext):
        pos = context.stra_get_position(self._inst.code)
        if pos != 0:
            context.stra_log_text(
                f"{self.name()} backtest end open pos {pos}, force close",
                level=3)
            context.stra_set_position(self._inst.code, 0, "BEC")
        context.stra_log_text(f"{self.name()} backtest ended")

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    def _size_and_cap(self, context: CtaContext, kind: str, n: float,
                      price: float, tag: str):
        """入场/加仓共用的 定手数+风控闸门 段 (P1-8: 合并原 ENTER/ADD
        两处近重复门控链, 否决日志 key/文案口径保持不变)。

        kind = "enter"|"add"; tag = enter 时方向 d, add 时当前层数
        (用于否决 key 与日志, 与原实现逐字一致)。
        返回 (final_lots, reasons); final_lots<=0 时已打印否决日志并
        reject_pending, 调用方直接返回。
        """
        lots = self._sizer.layer_lots(n, self._volscale, self._inst.units)
        if lots <= 0:
            if kind == "enter":
                msg = (f"veto enter {tag}@{price:.2f}: "
                       f"lots=0 (N={n:.2f}×volscale={self._volscale} "
                       f"> budget)")
                key = f"lot{tag}"
            else:
                msg = f"veto add @{price:.2f}: lots=0"
                key = f"add{tag}"
            self._veto_log(context, key, msg)
            self._reject_trigger(self._engine)
            return 0, []
        code = self._inst.code
        equity = self._risk.equity(context)
        final_lots, reasons = self._risk.cap_entry(
            code, lots=lots, n=n, volscale=self._volscale,
            price=price, stop_mult=self._base.stop_atr_mult,
            equity=equity, group=self._inst.group,
            avail=self._account_available())
        rkey = f"risk{tag}" if kind == "enter" else f"addrisk{tag}"
        self._log_account_gate(context, rkey, reasons, lots, final_lots)
        if final_lots <= 0:
            rpl = n * self._volscale * self._base.stop_atr_mult
            self._veto_log(
                context, rkey,
                f"veto {kind} @{price:.2f}: risk "
                f"({'/'.join(reasons)}), equity={equity:.0f}, "
                f"want={lots}, rpl={rpl:.0f}, vs={self._volscale}")
            self._reject_trigger(self._engine)
            return 0, reasons
        if final_lots < lots:
            skey = f"rscale{tag}" if kind == "enter" else f"addscale{tag}"
            if kind == "enter":
                msg = (f"risk scaled {lots}->{final_lots} lots "
                       f"({'/'.join(reasons)}), equity={equity:.0f}")
            else:
                msg = (f"risk scaled add {lots}->{final_lots} "
                       f"({'/'.join(reasons)})")
            self._veto_log(context, skey, msg)
        return final_lots, reasons

    def _execute(self, context: CtaContext, dec: dict, n: float,
                 fctx: FilterContext = None):
        code = self._inst.code
        kind = dec["decision"]

        if kind == DEC_ARM:
            # trigger 模式: 武装时过滤链前置裁决入场方向
            self._on_arm(context, self._engine, fctx)
            a = self._engine.armed
            context.stra_log_text(
                f"{self.name()} armed L={a['long']} S={a['short']} "
                f"stop={a['stop']} add={a['add']} exit={a['exit']}")
            return

        if kind == DEC_SKIP:
            context.stra_log_text(
                f"{self.name()} S1 filter skip {dec['direction']}@"
                f"{dec['price']:.2f} (last winner), fail-safe armed")
            return

        if kind == DEC_EXIT:
            old_pos = context.stra_get_position(code)
            context.stra_set_position(code, 0, "Exit")
            self._engine.confirm_exit(dec["price"])
            self._risk.on_closed(code)
            self._safe_mark(context, dec["price"], "exit", self.name())
            context.stra_log_text(
                f"{self.name()} exit {old_pos}@{dec['price']:.2f} "
                f"reason={dec['reason']}")
            return

        if kind == DEC_ENTER:
            d = dec["direction"]
            # === 过滤链 (隐式方向闸 + 配置过滤器): 否决不提交引擎 ===
            # trigger 模式触发决策已在武装时裁决 (prefiltered), 跳过
            if not dec.get("prefiltered") and \
                    not self._run_filter_chain(context, d, dec, fctx):
                self._reject_trigger(self._engine)
                return
            final_lots, _reasons = self._size_and_cap(
                context, "enter", n, dec["price"], str(d))
            if final_lots <= 0:
                return
            context.stra_set_position(code, d * final_lots, "Enter")
            # 记账价 = 决策价 (触发位/收盘价/tick 价)。
            # 不再从框架 avgpx 反推: stra_get_position/avgpx 是**信号账面**
            # (set_position 即变, 真实成交在下一次 tick 落地, 见
            # helpers/LiveTruth 模块头), confirm 时反推得到的是"新信号仓
            # × 旧成交均价"的混合值 (加仓时甚至等于加仓前的旧均价)。
            # 真实成交价由 _reanchor_actual_fill 回写: 逐笔成交优先,
            # 净均价兜底。
            fill = float(dec["price"])
            # n: 本层冻结 N (P0-2) — 止损/加仓不再随当根 N 漂移
            self._engine.confirm_enter(d, final_lots, fill, n)
            self._mark_fill_pending(context, self._subs[0]["name"],
                                    self._engine)
            # 风控账本按**当前实际止损位**记账 (P1 口径修复): 2N 口径
            # 会虚高数倍并占满组合预算 → 加仓全被否决 (2026-09-16 实测)
            self._sync_risk_book(n)
            self._safe_mark(context, dec["price"],
                                   "buy" if d > 0 else "sell", self.name())
            fs = " [FAIL-SAFE]" if dec.get("failsafe") else ""
            context.stra_log_text(
                f"{self.name()} enter {'LONG' if d > 0 else 'SHORT'} "
                f"{final_lots} lots @~{fill:.2f}, N={n:.2f}, VaR/layer="
                f"{n * self._volscale * final_lots:.0f}{fs}")
            return

        if kind == DEC_ADD:
            if self._limit_lock.get(code):
                # 涨跌停锁死疑似: 暂停加仓 (出场/止损不受影响), 告警
                self._veto_log(context, "limitlock",
                               f"veto add @{dec['price']:.2f}: "
                               f"limit-locked (price pinned "
                               f">{self._base.limit_lock_secs}s)")
                self._reject_trigger(self._engine)
                return
            d = self._engine.direction
            final_lots, _reasons = self._size_and_cap(
                context, "add", n, dec["price"],
                str(len(self._engine.layers)))
            if final_lots <= 0:
                return
            old_pos = context.stra_get_position(code)
            context.stra_set_position(code, old_pos + d * final_lots, "Add")
            fill = float(dec["price"])   # 同入场: 记账价=决策价, 待回写修正
            self._engine.confirm_add(final_lots, fill, n)
            self._mark_fill_pending(context, self._subs[0]["name"],
                                    self._engine)
            self._sync_risk_book(n)
            self._safe_mark(context, dec["price"],
                                   "buy" if d > 0 else "sell", self.name())
            context.stra_log_text(
                f"{self.name()} add layer{len(self._engine.layers)} "
                f"{final_lots} lots @~{fill:.2f}, N={n:.2f}")
            return

    def _run_filter_chain(self, context: CtaContext, sig_dir: int,
                          dec: dict, fctx: FilterContext = None) -> bool:
        """信号过滤链: 任一过滤器否决即拒绝 (不触碰引擎状态)"""
        for flt in self._chain:
            try:
                if fctx is not None:
                    ok, why = flt.allow_entry(fctx.with_direction(sig_dir))
                else:   # 直测路径 (_execute 无 bar 上下文)
                    ok, why = flt.allow_entry(FilterContext(sig_dir=sig_dir))
            except Exception as e:   # 过滤器异常不阻断交易, 记录后放行
                context.stra_log_text(
                    f"{self.name()} filter {flt.name} error: {e}", level=2)
                continue
            if not ok:
                self._veto_log(
                    context, f"f_{flt.name}_{sig_dir}",
                    f"veto enter {'LONG' if sig_dir > 0 else 'SHORT'}@"
                    f"{dec['price']:.2f}: filter={flt.name} ({why})")
                return False
        return True

    @staticmethod
    def _extract_oi(bars):
        """防御式提取持仓量序列 (WtNpKline 经 .ndarray 暴露原始字段;
        无该字段/非框架 K 线对象时返回 None, 过滤器自行降级)"""
        nd = getattr(bars, "ndarray", None)
        if nd is None:
            return None
        names = getattr(nd.dtype, "names", None) or ()
        if "open_interest" in names:
            return nd["open_interest"]
        return None

    # ------------------------------------------------------------------
    # 运行时对账 (P0-3)
    # ------------------------------------------------------------------
    def _reconcile(self, context: CtaContext, closes):
        """引擎层与实际持仓持续失配检测。

        实盘部分成交/拒单/手工干预会造成实际持仓与引擎 layers 脱节
        (on_init 只收养一次)。连续 recon_patience 根 bar 失配则告警并
        按实际持仓重建为单层, 风控账本同步覆盖。单根失配(订单在途)不动作。

        持仓量优先取交易通道真值 (P0-3): 框架 stra_get_position 是信号
        口径 (set_position 即变), 与之对比等于同源自比, 抓不到执行层
        问题; 真值不可用时回退框架口径 (行为与升级前一致)。
        """
        code = self._inst.code
        pos, pos_src = self._truth_position(context, code)
        eng_pos = self._combined_target()
        try:
            pos_i = int(round(pos))
        except (TypeError, ValueError):
            return
        if pos_i == eng_pos:
            self._recon_miss = 0
            return

        self._recon_miss += 1
        if self._recon_miss < int(self._base.recon_patience):
            return

        n = self._last_n if np.isfinite(self._last_n) else 0.0
        if pos_i == 0:
            # 外部全平: 全部活跃子系统确认出场, 风控清账
            px = float(closes[-1])
            held = [s for s in self._subs
                    if s["engine"].direction != 0 and s["engine"].layers]
            avg_entry = 0.0
            tot = 0
            for s in held:
                L = sum(l for l, _ in s["engine"].layers)
                avg_entry += sum(l * pr for l, pr
                                 in s["engine"].layers)
                tot += L
            avg_entry = avg_entry / max(1, tot)
            context.stra_log_text(
                f"{self.name()} RECONCILE: engine held "
                f"({','.join(s['name'] for s in held) or '-'}) but broker "
                f"flat, flatten @~{px:.2f} (entry~{avg_entry:.2f})", level=3)
            self._alert("reconcile", f"broker flat, engine flattened "
                                     f"@~{px:.2f}")
            for s in held:
                s["engine"].confirm_exit(px)
            self._risk.on_closed(code)
        else:
            d = 1 if pos_i > 0 else -1
            avgpx = abs(float(context.stra_get_position_avgpx(code) or 0))
            if avgpx <= 0:
                avgpx = float(closes[-1])
            context.stra_log_text(
                f"{self.name()} RECONCILE: mismatch persisted "
                f"(engine net={eng_pos}; broker {pos_i} [{pos_src}]), "
                f"rebuilt into "
                f"{self._subs[0]['name']} as single layer "
                f"[{abs(pos_i)}@{avgpx:.2f}]", level=3)
            self._alert("reconcile", f"mismatch persisted, rebuilt "
                                     f"{abs(pos_i)}@{avgpx:.2f}")
            first = self._subs[0]["engine"]
            first.adopt_position(d, abs(pos_i), avgpx, n if n > 0 else None)
            for s in self._subs[1:]:
                s["engine"].flatten_book()
            # 风险按重建后各层冻结 N 记账 (与单系统路径同口径)
            risk = sum(s["engine"].booked_risk(
                           self._volscale or 1.0,
                           self._base.stop_atr_mult,
                           stop=s["engine"].stop_anchor())
                       for s in self._subs)
            self._risk.on_rebuilt(code, d, abs(pos_i), avgpx,
                                  n if n > 0 else 1.0, self._volscale or 1.0,
                                  self._base.stop_atr_mult, self._inst.group,
                                  risk=risk)
        self._recon_miss = 0

    # ------------------------------------------------------------------
    # S1+S2 双系统批量执行 (净仓语义: 框架持仓 = Σ子系统目标)
    # ------------------------------------------------------------------
    def _execute_dual(self, context: CtaContext, dec_map: dict, n: float,
                      fctx: FilterContext = None):
        """逐子系统消费决策 (只改引擎态), 批次结束统一落一次净仓单。

        成交价口径: 引擎按决策价(bar close)确认 — 回测撮合价即当根
        收盘, 实盘经执行单元的偏差由运行时对账兜底。
        """
        code = self._inst.code
        for sub in self._subs:
            for dec in dec_map.get(sub["name"], []):
                kind = dec["decision"]
                e = sub["engine"]
                tag = f"[{sub['name']}]"
                if kind == DEC_ARM:
                    self._on_arm(context, e, fctx)
                    continue
                if kind == DEC_SKIP:
                    context.stra_log_text(
                        f"{self.name()}{tag} S1 filter skip "
                        f"{dec['direction']}@{dec['price']:.2f} "
                        f"(last winner), fail-safe armed")
                    continue
                if kind == DEC_EXIT:
                    old = self._sub_lots(sub) * \
                        (1 if e.direction > 0 else -1)
                    e.confirm_exit(dec["price"])
                    self._safe_mark(context, dec["price"], "exit",
                                           self.name())
                    context.stra_log_text(
                        f"{self.name()}{tag} exit {old}"
                        f"@{dec['price']:.2f} reason={dec['reason']}")
                    continue
                if kind == DEC_ENTER:
                    d = dec["direction"]
                    lots = self._apply_enter_gates(context, sub, d,
                                                   dec, n, fctx)
                    if lots <= 0:
                        self._reject_trigger(e)
                        continue
                    e.confirm_enter(d, lots, dec["price"], n)
                    self._mark_fill_pending(context, sub["name"], e)
                    self._safe_mark(context,
                                    dec["price"],
                                    "buy" if d > 0 else "sell",
                                    self.name())
                    fs = " [FAIL-SAFE]" if dec.get("failsafe") else ""
                    context.stra_log_text(
                        f"{self.name()}{tag} enter "
                        f"{'LONG' if d > 0 else 'SHORT'} {lots} "
                        f"@~{dec['price']:.2f}, N={n:.2f}{fs}")
                    continue
                if kind == DEC_ADD:
                    lots = self._apply_add_gates(context, sub, dec, n)
                    if lots <= 0:
                        self._reject_trigger(e)
                        continue
                    e.confirm_add(lots, dec["price"], n)
                    self._mark_fill_pending(context, sub["name"], e)
                    dmark = "buy" if e.direction > 0 else "sell"
                    self._safe_mark(context, dec["price"], dmark,
                                           self.name())
                    context.stra_log_text(
                        f"{self.name()}{tag} add "
                        f"layer{len(e.layers)} {lots} "
                        f"@~{dec['price']:.2f}, N={n:.2f}")

        new_t = self._combined_target()
        cur = int(round(context.stra_get_position(code)))
        if new_t != cur:
            context.stra_set_position(code, new_t, "Dual")
        self._sync_risk_book(n)

    def _apply_enter_gates(self, context: CtaContext, sub: dict, d: int,
                           dec: dict, n: float,
                           fctx: FilterContext = None) -> int:
        """入场三道闸: 过滤链 → VaR 手数 → 组合风控。返回最终手数(0=否决)"""
        code = self._inst.code
        tag = f"[{sub['name']}]"
        if not dec.get("prefiltered") and \
                not self._run_filter_chain(context, d, dec, fctx):
            return 0
        lots = self._sizer.layer_lots(n, self._volscale, sub["units"])
        if lots <= 0:
            self._veto_log(context, f"lot{d}",
                           f"{tag} veto enter {d}@{dec['price']:.2f}: "
                           f"lots=0 (units={sub['units']}, "
                           f"N={n:.2f}×volscale={self._volscale})")
            return 0
        equity = self._risk.equity(context)
        final_lots, reasons = self._risk.cap_entry(
            code, lots=lots, n=n, volscale=self._volscale,
            price=dec["price"], stop_mult=self._base.stop_atr_mult,
            equity=equity, group=self._inst.group,
            share=self._sub_share(sub), avail=self._account_available())
        self._log_account_gate(context, f"{tag}enter", reasons, lots,
                               final_lots)
        if final_lots <= 0:
            self._veto_log(
                context, f"risk{d}",
                f"{tag} veto enter {d}@{dec['price']:.2f}: risk "
                f"({'/'.join(reasons)}), equity={equity:.0f}")
            return 0
        if final_lots < lots:
            self._veto_log(
                context, f"rscale{d}",
                f"{tag} risk scaled {lots}->{final_lots} lots "
                f"({'/'.join(reasons)}), equity={equity:.0f}")
        return final_lots

    def _apply_add_gates(self, context: CtaContext, sub: dict,
                         dec: dict, n: float) -> int:
        e = sub["engine"]
        tag = f"[{sub['name']}]"
        lots = self._sizer.layer_lots(n, self._volscale, sub["units"])
        if lots <= 0:
            self._veto_log(context, f"add{len(e.layers)}",
                           f"{tag} veto add @{dec['price']:.2f}: lots=0")
            return 0
        equity = self._risk.equity(context)
        final_lots, reasons = self._risk.cap_entry(
            self._inst.code, lots=lots, n=n, volscale=self._volscale,
            price=dec["price"], stop_mult=self._base.stop_atr_mult,
            equity=equity, group=self._inst.group,
            share=self._sub_share(sub), avail=self._account_available())
        self._log_account_gate(context, f"{tag}add{len(e.layers)}",
                               reasons, lots, final_lots)
        if final_lots <= 0:
            self._veto_log(
                context, f"addrisk{len(e.layers)}",
                f"{tag} veto add @{dec['price']:.2f}: risk "
                f"({'/'.join(reasons)})")
            return 0
        return final_lots

    def _sub_share(self, sub: dict) -> float:
        """子系统品种内预算份额 (双系统按 units 占比; 单系统=1.0)"""
        if getattr(self._inst, "speeds", None):
            return max(0.0, float(sub.get("share", 1.0)))
        if not self._is_dual or int(self._inst.units) <= 0:
            return 1.0
        return max(0.0, min(1.0, sub["units"] / float(self._inst.units)))

    def _sync_risk_book(self, n: float):
        """按两子系统合并仓位覆盖式同步风控账本。

        风险值按**当前实际止损位**汇总 (engine.booked_risk(stop=...)):
        即"所有层在共同止损位离场"的最坏净亏损。用 2N 口径会显著虚高
        (2026-09-16 实盘实测 52.7 万 vs 20.4 万), 把组合预算占满导致
        后续加仓全被 `total_risk(cap=0)` 否决。
        """
        code = self._inst.code
        tot_l = 0
        wsum = 0.0
        dirn = 0
        risk = 0.0
        for s in self._subs:
            e = s["engine"]
            if e.direction == 0 or not e.layers:
                continue
            L = sum(l for l, _ in e.layers)
            wsum += sum(l * pr for l, pr in e.layers)
            tot_l += L
            dirn = e.direction   # 双系统同向持仓 (反向并存时以首活跃为准)
            risk += e.booked_risk(self._volscale or 1.0,
                                  self._base.stop_atr_mult,
                                  stop=e.stop_anchor())
        if dirn == 0 or tot_l == 0:
            self._risk.on_closed(code)
            return
        price = wsum / tot_l
        self._risk.sync_position(
            code, dirn, tot_l, price,
            n if n and n > 0 else 1.0, self._volscale or 1.0,
            self._base.stop_atr_mult, self._inst.group, risk=risk)

    def _reject_trigger(self, engine):
        """trigger 模式: 策略层否决已触发决策 → 引擎解除当日武装"""
        if engine.entry_mode == "trigger":
            engine.reject_pending()

    def _veto_log(self, context: CtaContext, key: str, msg: str):
        """否决日志去重: 同 key 在冷却期内只记首次 (P2)。

        按 key 独立冷却 (原全局单槽在不同否决原因交替出现时不收敛);
        被抑制期间累计次数, 下次记录时带 [xN] 便于判断持续程度;
        首见同步告警 (风控否决=信号被拦截, 属生产须知事件)。
        """
        now = time.time()
        self._veto_hits[key] = self._veto_hits.get(key, 0) + 1
        last = self._veto_last.get(key)
        if last is not None and now - last < self._VETO_LOG_COOLDOWN:
            return
        n = self._veto_hits.pop(key, 1)
        self._veto_last[key] = now
        extra = f" [x{n} since last log]" if n > 1 else ""
        context.stra_log_text(f"{self.name()} {msg}{extra}", level=2)
        self._alert(f"veto_{key}", msg)

    @staticmethod
    def _get_fees(context) -> float:
        """累计手续费 (flag=3)。

        降级用代理: 若**真值源不可用**, 只能用 fees 近似"真实成交"。
        注意其口径存疑 (2026-09-15 源码复核): fees 在
        do_set_position 内累加 (CtaStraBaseCtx.cpp:1469), 而该函数由
        on_tick 消费信号时触发 (:811), 并非券商成交回报回调 → 源码
        口径下"信号落地即计费", 代理可能失效 (fail-open); 反之费率
        未配置/为 0 的品种 (实测 INE.SC fees 恒 0) 表现为 fail-closed
        (永久疑似幽灵)。故新增通道真值源 (helpers/LiveTruth) 优先,
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

    def _fill_confirmed(self, context, fp: dict, code: str,
                        target: int) -> tuple:
        """真实成交是否已到位 → (confirmed, src, pos)。

        src: "truth" = 交易通道真值(增量)判定; "framework" = 框架信号口径。

        口径 (2026-09-15 修订为**增量**): 账户持仓含其他策略同合约仓位,
        绝对值无法归属 (实测 CZTA 账户 112/本策略 64)。故只看**本次下单
        窗口内的增量**: 登记时刻记 truth0, 之后要求 (now - truth0) 与
        本层手数同号且足额。这样即使账户已有别家仓位也不误判。
        真值不可用时退回 fees 代理 (fees 不增视为未成交), 超过 2× 时限
        放行, 避免免费品种永久阻塞记账价回写。
        """
        pos = float(context.stra_get_position(code))
        engine = fp.get("engine")
        sign = 1 if (engine is not None and engine.direction > 0) else -1
        if engine is None or engine.direction == 0:
            sign = 1
        lots = abs(float(fp.get("lots") or 0))
        t0 = fp.get("truth0")
        if t0 is not None and lots > 0:
            now = self._truth_net(code)
            if now is not None:
                delta = now - t0
                if delta * sign >= lots:
                    return True, "truth", now
                return False, "truth", now      # 增量未到位 (或未成交)
            # 真值中途失效 → 落 fees 代理
        if int(round(pos)) != target:
            return False, "framework", pos
        pos_src = "framework"       # 真值不可用 → fees 兜底口径
        fees0 = fp.get("fees0")
        if fees0 is None:
            return True, pos_src, pos
        fees = self._get_fees(context)
        if fees is None or fees > fees0:
            return True, pos_src, pos
        waited = time.time() - fp.get("ts", 0)
        limit = 600 if fees0 > 0 else 1800
        if waited > limit * 2:
            if not fp.get("unblocked"):
                fp["unblocked"] = True
                context.stra_log_text(
                    f"{self.name()} fees unchanged {waited:.0f}s "
                    f"(fees0={fees0:.2f}) — 按免费/未配置费率品种放行 "
                    f"avgpx 回写 (真值源不可用)", level=3)
            return True, pos_src, pos
        return False, pos_src, pos

    def _maybe_ghost_trim(self, context, key: str, fp: dict,
                          broker_net: float):
        """ghost_fill 自愈 (P0-3, 2026-09-17 CF 34 手幽灵仓事故)。

        真值源确认"订单未到柜台"后若只告警不处置, 引擎会永久按未持仓
        手数管理风险 (风控预算虚占、止损虚算、极端情况下按虚高手数
        平仓被拒)。处置 = 把本策略信号层从尾部裁剪到"与券商同向且
        不超过券商"的手数:
          - **只缩量, 绝不放大**: 仅丢弃本策略未确认的尾部层, 不按
            券商绝对值建仓 → 共享账户免疫 (券商持仓含别家同向仓位时
            最多保守多裁, 不会错加);
          - 跨子系统从尾部子系统开始裁, 保持净额一致;
          - 裁剪后 set_position 对齐信号账 + 风控账本重建 + 告警;
          - base.ghost_auto_trim=False 退回旧行为 (只告警人工处置)。
        券商绝对值含别家同向仓位 → 本策略持仓被低估的残余风险由
        运行时对账 (reconcile) 兜底, 裁错的层会以 truth_mismatch 告警。
        """
        if not self._base.ghost_auto_trim:
            return
        broker_net = float(broker_net)
        tot = self._combined_target()
        if tot == 0 or broker_net == 0:
            return                     # 无持仓/券商口径为零 → 不动作
        if (tot > 0) != (broker_net > 0):
            return                     # 方向相反 → 不动作 (告警已发)
        target = abs(int(round(broker_net)))
        cur = abs(tot)
        if target >= cur:
            return                     # 券商不少于信号 → 非本策略幽灵
        code = self._inst.code
        drop = cur - target
        trimmed = 0
        for sub in reversed(self._subs):      # 尾部层=最后加仓=最易幽灵
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
                    f"{self.name()}[{sub['name']}] ghost trim: -{dropped} "
                    f"lots -> {sum(l for l, _ in e.layers)} (broker="
                    f"{broker_net})", level=3)
        if trimmed <= 0:
            return
        self._fill_pending.pop(key, None)     # 被裁层无需再等待回写
        new_target = self._combined_target()
        context.stra_set_position(code, new_target, "GhostTrim")
        self._sync_risk_book(self._last_n)
        self._alert("ghost_trim",
                    f"{code} signal trimmed {tot}->{new_target} "
                    f"to broker {broker_net} ({trimmed} lots ghost)")

    def _maybe_exec_recover(self, context, detail: str):
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
        t = self._truth_source()
        if t is None or t.stale():
            context.stra_log_text(
                f"{self.name()} exec recover skipped: 真值源缺失/陈旧",
                level=3)
            return
        import datetime as _dt
        day = _dt.datetime.now().strftime("%Y%m%d")
        marker = os.path.join(os.environ.get("WT_RECOVER_DIR") or _GROUP_DIR,
                              f".exec_recover_{day}")
        if os.path.exists(marker):
            context.stra_log_text(
                f"{self.name()} exec recover skipped: 本日已重启过一次 "
                f"({marker})", level=3)
            self._alert("exec_recover_skip",
                        "ghost_fill 仍在, 但本日已自愈重启过 → 需人工"
                        "处置 (查执行单元/通道)")
            return
        try:
            with open(marker, "w", encoding="utf-8") as f:
                f.write(f"{self.name()} {detail}\n")
        except OSError:
            pass
        context.stra_log_text(
            f"{self.name()} exec recover: 进程自退触发 WatchDog 重启 "
            f"(ghost_fill 未消解: {detail})", level=3)
        self._alert("exec_recover",
                    f"exec unit stalled, restarting process: {detail}")
        os._exit(1)      # noqa: SLF001  立即退出 (跳过 C++ 析构),
                         # 状态已随每根 bar/触发事件持久化 + 真值源兜底

    def _reanchor_actual_fill(self, context: CtaContext):
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
        for key in list(self._fill_pending):
            fp = self._fill_pending.get(key)
            if fp is not None:
                # 同合约多笔待回写: 按确认顺序逐笔分配成交手数 (见
                # TradeJournal.layer_price 的口径说明)
                code = self._inst.code
                gkey = (code, fp.get("cursor"))
                used = self._reanchor_one_fill(context, key, fp,
                                               alloc.get(gkey, 0.0))
                if used:
                    alloc[gkey] = alloc.get(gkey, 0.0) + used

    def _reanchor_one_fill(self, context: CtaContext, key: str, fp: dict,
                           alloc: float = 0.0):
        """单个子系统的成交价回写 (队列元素处理, 完成即出队)。

        alloc: 同合约在本轮已分配给前序记录的成交手数 (逐笔成交口径)。
        返回本轮消耗的手数 (用了逐笔成交时), 否则 None。
        """
        e = fp["engine"]
        if e.direction == 0 or not e.layers:
            self._fill_pending.pop(key, None)
            return
        code = self._inst.code
        target = self._combined_target()
        confirmed, pos_src, pos = self._fill_confirmed(context, fp, code,
                                                       target)
        if not confirmed or pos == 0:
            # 分批窗口超时 (>10min 未达目标仓位) → 告警一次
            # (执行单元放弃追单/涨跌停无法成交的可观测代理, P0-2;
            #  真值源可用时这是"订单未到柜台"的直接证据)
            waited = time.time() - fp.get("ts", 0)
            if not fp.get("alerted") and waited > 600:
                fp["alerted"] = True
                # 真值源判定 = 券商侧确实没有这笔仓位 (幽灵仓直接证据)
                evt = "ghost_fill" if pos_src == "truth" else "fill_timeout"
                txt = (f"target={target} vs broker={pos} [{pos_src}], "
                       f"{waited:.0f}s 未达标")
                context.stra_log_text(
                    f"{self.name()} {evt}: {txt}", level=3)
                self._alert(evt, txt)
                if evt == "ghost_fill":
                    self._maybe_exec_recover(context, txt)
                    self._maybe_ghost_trim(context, key, fp, pos)
            return                # 分批成交中/未成交, 下次再试
        # F2 幽灵仓位判定已在 _fill_confirmed 完成: 真值源优先 (订单
        # 真没到柜台), fees 仅在真值缺失时兜底; 故此处到得仓已确认,
        # 可以用 avgpx 反推成交均价
        last_lots, recorded = e.layers[-1]
        # === 口径 1 (P2, 首选): 逐笔成交 → 本层真实成交均价 ===
        # 每层独立可解, 不受"多笔待回写"影响 (净均价方程欠定)
        actual = self._journal_layer_price(code, fp, e, alloc)
        src = "trades"
        used = (fp.get("lots") or last_lots) if actual is not None else None
        if actual is None:
            actual, src = self._avgpx_layer_price(context, e, code, pos), \
                "avgpx"
            if actual is None:
                return            # 净均价不可用 → 继续等待 (不猜)
        self._fill_pending.pop(key, None)
        if actual <= 0:
            return                # 反推异常(持仓干预等), 放弃修正
        if self._pricetick and abs(actual - recorded) > 0.5 * self._pricetick:
            e.reanchor_last_price(actual)
            context.stra_log_text(
                f"{self.name()} fill re-anchor layer{len(e.layers)} "
                f"{recorded:.2f} -> {actual:.2f} ({src}), "
                f"stop/add levels shifted")
        return used

    def _avgpx_layer_price(self, context, engine, code: str, pos: float):
        """净均价反推本层成交价 (逐笔成交不可用时的兜底口径)。

        数学限制: N 个待回写层 → N 个未知价, 只有净均价 1 个方程 →
        仅当**只有一层未知**(其余层已确认)时精确; 该口径下其余层按
        记账价处理 (与升级前一致)。多笔并存时优先用逐笔成交。
        """
        if len(self._fill_pending) > 1:
            return None          # 欠定: 交由逐笔成交/聚合漂移修正
        fw_avg = context.stra_get_position_avgpx(code)
        if fw_avg == 0:
            return None
        dirs = {s["engine"].direction for s in self._subs
                if s["engine"].direction != 0}
        if len(dirs) > 1:
            return None          # 反向并存, 净仓成本无法归因
        sign = 1 if engine.direction > 0 else -1
        last_lots = engine.layers[-1][0]
        rest = 0.0                # 除最后确认层外全部层的带符号成本
        for s in self._subs:
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

    def _reanchor_on_roll(self, context: CtaContext):
        """avgpx 漂移对账: 持仓量不变但框架 avgpx 偏离引擎记账 → 各层平移。

        覆盖两类漂移: ①HOT 换月价差; ②重启丢失 _fill_pending 后残留的
        记账价偏差 (fill 回写的崩溃窗口兜底)。框架 avgpx 带方向符号
        (空头为负), 与层价格 (恒正) 比较时取绝对值。
        实盘 d1 主K不闭合 → on_calculate 路径不执行, 故同时挂在
        _session_eval_daily/_catchup_eval 两条日评估路径。

        双系统 (S1+S2): 旧实现只按首子系统层数比对净仓, 两子系统同时
        持仓时恒不相等 → 静默跳过 (B5, 换月漂移永不修正)。现按**全部
        持仓子系统**汇总后平移: 同一次换月的价差对所有子系统是同一
        个 Δ, 故 net 平均成本与实际 netavgpx 的差即为 Δ (反方向并存时
        按净成本口径仍成立)。
        """
        subs = [s["engine"] for s in self._subs
                if s["engine"].direction != 0 and s["engine"].layers]
        if not subs:
            return
        code = self._inst.code
        pos = context.stra_get_position(code)
        net_lots = self._combined_target()
        tot_lots = sum(sum(l for l, _ in e.layers) for e in subs)
        if pos == 0 or tot_lots == 0 or abs(net_lots) == 0 \
                or abs(int(round(pos))) != abs(net_lots):
            return   # 持仓量不符(部分成交/手工干预)时不做 re-anchor
        net_cost = 0.0
        for e in subs:
            sign = 1.0 if e.direction > 0 else -1.0
            net_cost += sign * sum(l * pr for l, pr in e.layers)
        our_avg = abs(net_cost) / tot_lots        # 与框架同一正数口径
        fw_avg = abs(context.stra_get_position_avgpx(code))
        shift = fw_avg - our_avg
        if self._pricetick and abs(shift) > 0.5 * self._pricetick:
            for e in subs:
                e.shift_prices(shift)
            context.stra_log_text(
                f"{self.name()} avgpx drift re-anchor: shift "
                f"{shift:+.2f} (hot roll or stale fill, "
                f"{len(subs)} sub) — layer prices re-anchored")
