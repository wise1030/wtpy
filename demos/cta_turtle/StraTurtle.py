"""StraTurtle — 海龟趋势跟踪策略 (wtpy CTA 框架)

架构: 薄壳模式, 信号逻辑全部在 core/TurtleEngine (纯逻辑可单测)。
本层职责:
- 生命周期: on_init 订阅K线/注册图表/恢复状态, on_calculate 主流程
- 指标计算: ATR(N) + Donchian 通道 (core/indicators)
- 执行: 方向闸 → VaR 手数 → stra_set_position 目标仓位
- 换月适配: avgpx 漂移 re-anchor 引擎层价格 (.HOT 自动移仓)
- 持久化: user_save_data 引擎快照, 重启恢复
- 图表: 通道线/止损线叠加主图, ATR/持仓副图, 进出场 mark

框架对齐:
- 交易 .HOT 主力连续合约, C++ 核心自动换月
- 合约乘数取 stra_get_comminfo().volscale (非配置)
- set_position 目标语义, live 由 executers.yaml 执行单元落单
"""
import dataclasses
import hashlib
import json
import os
import time

import numpy as np
from wtpy import BaseCtaStrategy, CtaContext

from schemas import InstrumentConfig, TurtleBaseConfig, RiskConfig
from filters import build_chain, FilterContext
from core.aggregate import aggregate_h1, norm_m5_time
from core.hotsutil import (align_window, align_window_hotfill, find_common,
                           load_hots, parse_contracts, split_code)
from core.indicators import wilder_atr, donchian, with_next_channels
from core.TurtleEngine import (TurtleEngine)
from core.PositionSizer import PositionSizer
from execution import ExecutionRouter
from execution.telemetry import Telemetry
from execution.fill_tracker import FillTracker
from execution.truth_monitor import TruthMonitor
from execution.models import StrategyMode
from execution.batch import TargetIntent
from core.RiskManager import RiskManager
from core.profiles import (resolve_profiles, required_bars, snapshot_meta,
                           validate_snapshot)

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
                 bt_no_simtick: bool = False,
                 alloc_total: int = 1):
        BaseCtaStrategy.__init__(self, name)
        self._inst = inst_cfg
        self._base = base_cfg
        self._notify = notify    # core/notifier.WebhookNotifier 或 None
        # bt 日线 trigger 退化: 无 m5 数据时不订 tick/m5 副K (模拟tick
        # 只能由非日K bar 生成, simTicks 跳过 KP_DAY), 触发判定仍走
        # on_bar 的 check_bar, 撮合退化为 bar 收盘口径 (成交价偏保守,
        # 与 rb 有 m5 时的 tick 撮合基线不可直接对拍)
        self._bt_no_simtick = bt_no_simtick
        # A2: 运行模式面 (on_init 用真实 is_backtest 重解)
        self._mode = StrategyMode.resolve(self._base, self._inst,
                                          False, bt_no_simtick)
        # Phase 1 资本模型: 组合品种总数 (派生 unit_var 的等权分配分母;
        # from_config/runLive 传 len(instruments), 单测缺省 1)
        self._alloc_total = max(1, int(alloc_total))

        # === 子系统装配 ===
        # profiles/system/speeds 由 core.profiles 唯一展开为 ProfileSpec:
        # profiles 是唯一推荐入口 (preset=窗口+filter 预设简写);
        # system/speeds 是 deprecated 别名, 展开结果逐字等价。各 profile
        # 只独立通道/引擎状态；ATR、过滤链、执行和风控仍共享。
        self._profiles = resolve_profiles(inst_cfg, base_cfg)
        self._subs = []
        for spec in self._profiles:
            self._subs.append({
                "name": spec.key, "units": spec.units,
                "entry": spec.entry, "exit": spec.exit, "fs": spec.fs,
                "share": spec.share, "system": spec.system,
                "engine": TurtleEngine(
                    filter=spec.filter,
                    stop_atr_mult=base_cfg.stop_atr_mult,
                    pyramid_layers=base_cfg.pyramid_layers,
                    pyramid_step_atr=base_cfg.pyramid_step_atr,
                    entry_mode=base_cfg.entry_mode,
                    stop_n_mode=base_cfg.stop_n_mode,
                    trigger_envelope=base_cfg.trigger_envelope,
                    allow_same_bar_reversal=
                    base_cfg.allow_same_bar_reversal,
                    gap_stop=base_cfg.gap_stop,
                    arm_exclude_signal=base_cfg.arm_exclude_signal)})
        # 兼容别名: 单系统即唯一引擎; 双系统指向 S1 子系统
        self._engine = self._subs[0]["engine"]

        self._sizer = PositionSizer(base_cfg.unit_var)
        # 组合风控: runLive 多品种共享同一实例; 单品种回测私有实例等价
        self._risk = risk_mgr or RiskManager(risk_cfg or RiskConfig())
        # A1 尾项: 模块不再持有策略回指, 改持 StrategyHost 显式依赖面
        # (各模块 DEPS 白名单声明全部读写依赖, 未声明访问 loud-failure)
        from execution.host import StrategyHost
        self._router = ExecutionRouter(StrategyHost(self, *ExecutionRouter.DEPS))
        self._telemetry = Telemetry(StrategyHost(self, *Telemetry.DEPS))
        self._fills = FillTracker(StrategyHost(self, *FillTracker.DEPS))
        self._truthmon = TruthMonitor(StrategyHost(self, *TruthMonitor.DEPS))
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
        # 快照恢复失败 → DEGRADED (Phase 0, 2026-11-09): 引擎状态真实性
        # 不可信, 禁止新开/加仓 (出场/止损不受影响 — 按收养/对账的近似
        # 状态管理仍优于完全裸奔); 首次成功 _persist 后解除。Alert 每日
        # 至多一次 (_degrade_date)。
        self._state_degraded = False
        self._degrade_date = None
        # 盘中去重状态 (P1: None=待首个 calculate 建基线, 不回放历史)
        self._d1_date = None        # 已消费的信号周期末根 bar 日期
        self._h1_key = None         # h1: 已评估桶锚点 tdate*10000+label
        self._h1_m5_key = None      # h1: m5 推进基线 (float键, 快照持久化)
        self._m5_key = None         # 已扫描的盘中止损主K末根键值
        self._catchup_date = None   # 自愈评估已检查的交易日 (每日至多一次)
        self._in_schedule = False   # on_calculate 期间置位 (图表API仅此时可用)
        # 日损闸 (2026-10-20): 相对当日首笔权益的回撤; 0=关闭。
        # 用策略自身 equity (fallback+净盈亏), 不用共享账户总额。
        self._day_guard_date = None
        self._day_start_equity = None
        self._daily_blocked = False     # 停止新开/加仓
        self._daily_flattened = False   # 已执行日损全平
        # 成交均价待回写队列: {子系统名: {"engine","ts","fees0",...}}
        # (P2: 原实现是单槽, 同一批次里后确认的子系统会覆盖前一个,
        #  导致先确认那层的记账价永不被成交均价修正)
        self._fill_pending = {}
        # 实盘出场未知态: {子系统名: 证据游标/原因/手数/时间}; 引擎层
        # _exit_pending 保留持仓，二者一起入快照。回测始终为空。
        self._exit_orders = {}
        # enter/add 的 write-ahead 目标意图: 先持久化，再提交绝对目标；
        # 成交证据到达后才确认引擎。崩溃恢复禁止假确认。回测恒空。
        self._open_intents = {}
        self._outbox_seq = 0
        # 订单级归因链 (2026-10-29, docs/ORDER_ATTRIBUTION_NO_CPP.md):
        # 本地id 归因替代 C++ userTag; 回测/侧车缺失时恒 None → 行为不变。
        self._attr = None
        # 实盘多 profile 出场批次挂起时延后的 enter/add 决策。pending 保留
        # 在引擎中防重复触发；批次真实成交确认后再统一重放。入快照，崩溃
        # 恢复后仍不会把"延后"退化成"取消"。
        self._deferred_decisions = {}
        # 涨跌停锁死疑似状态: {code: True}; on_tick 价格钉住检测置位,
        # 价格恢复移动即清除; 置位期间 veto 加仓 (出场/止损不受影响)
        self._limit_lock = {}
        self._pin_price = None      # [price, first_ts] 价格钉住检测
        self._ghost_trim_date = 0   # ghost trim 发生当日 (yyyymmdd), 当日
        #                           # 禁止 enter/add 再触发 (循环修复)
        self._fund_live_ts = 0.0    # fund_live.csv 写入节流
        # K线旁路日志 (数据完整性: 崩溃窗口兜底, core/BarJournal)
        self._journal = None        # 惰性创建 BarJournal
        self._j_d1_date = None      # 已落日志的信号周期日期
        self._j_m5_key = None       # 已落日志的盘中键值
        # Phase 0 心跳/周期快照 (2026-11-09): 实盘每 ≤5s 写
        # generated/heartbeat/<name>.json (tmp+rename), 供 heartbeat_watch
        # / doctor 做进程存活、tick 断流 (2026-09-28 类事故)、状态概览的
        # 外部检测; 回测不写。周期快照 (300s 节流) + generated/snapshots/
        # 3 深轮换备份, 缩小崩溃窗口。旁路失败静默跳过, 绝不阻断交易。
        self._last_tick_ts = 0.0
        self._heartbeat_ts = 0.0
        self._periodic_persist_ts = 0.0
        self._snap_hash = ""
        # -1 = 从未告警 (日期不可得时 day=None, None!=-1 → 首见即告警,
        # 之后当日抑制; 若用 None 初值会把"日期不可得"的首见告警吞掉)
        self._no_price_arm_date = -1
        # Phase 1 合约信号源 (2026-11-10): signal_source=contract 时按
        # hots.json 当前主力解析信号合约 (_sig_contract), 指标/通道在其
        # 自身真实序列上计算 (无复权); None = hot 口径或未解析成功。
        self._sig_contract = None
        self._sig_calendar = None        # bt 逐日主力 [(start,end,yymm)]
        self._sig_bars_cache = {}        # full_code -> (dates, h, l, c)
        self._sig_cur_dom = None         # 当前评估日主力 (roll 事件检测)
        self._sig_roll_alert_day = -1
        # 交易通道真值源 (P0-3): generated/traders/<id>/rtdata.json
        self._truth = None          # core.LiveTruth.ChannelTruth 或 None
        self._truth_probed = False  # 已探测过 (不可用时不反复重试)
        # 回测隔离 (2026-10-20): 回测绝不允许读实盘真值。
        # 否则同机/同目录下的新鲜 rtdata.json 会参与成交确认与 ghost trim
        # (alert 模式只挡重建, 挡不住 _fill_confirmed/_maybe_ghost_trim),
        # 比 2026-09-15 的"陈旧真值污染"更隐蔽。
        self._is_backtest = False
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
        """→ execution.Telemetry.journal_writer (A1-2 迁出, 薄转发)"""
        return self._telemetry.journal_writer()

    # ------------------------------------------------------------------
    # 交易通道真值源 (P0-3)
    # ------------------------------------------------------------------
    def _truth_source(self):
        """→ execution.TruthMonitor.truth_source (A1-4 迁出, 薄转发)"""
        return self._truthmon.truth_source()

    def _truth_position(self, context, code: str):
        """→ execution.TruthMonitor.truth_position (A1-4 迁出, 薄转发)"""
        return self._truthmon.truth_position(context, code)

    _TRUTH_CHECK_INTERVAL = 300.0   # 周期对账间隔 (秒)
    _TRUTH_ESCALATE_SEC = 600.0     # 差额持续多久升级为高危告警

    def _periodic_truth_check(self, context):
        """→ execution.TruthMonitor.periodic_truth_check (A1-4 迁出, 薄转发)"""
        return self._truthmon.periodic_truth_check(context)

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
        """→ execution.TruthMonitor.account_available (A1-4 迁出, 薄转发)"""
        return self._truthmon.account_available()

    def _log_account_gate(self, context, tag: str, reasons: list, want: int, final: int):
        """→ execution.TruthMonitor.log_account_gate (A1-4 迁出, 薄转发)"""
        return self._truthmon.log_account_gate(context, tag, reasons, want, final)

    def _bar_row(self, bars, i: int = -1):
        """→ execution.Telemetry.bar_row (A1-2 迁出, 薄转发)"""
        return self._telemetry.bar_row(bars, i)

    def _journal_signal_bar(self, bars):
        """→ execution.Telemetry.journal_signal_bar (A1-2 迁出, 薄转发)"""
        return self._telemetry.journal_signal_bar(bars)

    def _journal_intrabar_bars(self, mbars, mkey):
        """→ execution.Telemetry.journal_intrabar_bars (A1-2 迁出, 薄转发)"""
        return self._telemetry.journal_intrabar_bars(mbars, mkey)

    # ------------------------------------------------------------------
    # 工厂
    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls, name: str, code: str, config_path: str,
                    risk_mgr: RiskManager = None, notify=None,
                    bt_no_simtick: bool = False, capital: float = None):
        """按配置装配; capital 非空时覆盖 risk.fallback_capital。

        回测一致性 (2026-10-20): runBT/run_matrix 的 --capital 必须同时
        作为 RiskManager 的权益基数, 否则"500 万报告分母 + 1500 万风控
        基数"会让手数比报告资金大 3 倍, 收益/回撤不可比。
        """
        from core.ConfigLoader import (ConfigLoader, allocated_capital,
                                          risk_coherence_warning)
        cfg = ConfigLoader.load(config_path, warn=False)
        if capital is not None:
            cap = float(capital)
            if not (cap > 0 and cap == cap):
                raise ValueError(f"capital 非法: {capital!r}")
            if cfg.risk.portfolio_capital > 0:
                # 分配启用时 --capital 表达的是组合总资金
                cfg.risk.portfolio_capital = cap
            else:
                cfg.risk.fallback_capital = cap
        # 资金分配解析 (2026-10-22 S1): 启用时权益基数 = 本品种分配额,
        # 直接替换 fallback_capital 使下游 (RiskManager.equity/一致性
        # WARN/账本) 全部走分配口径
        codes = [i.code for i in cfg.instruments]
        alloc = allocated_capital(cfg.risk, code, codes)
        if alloc != cfg.risk.fallback_capital:
            cfg.risk = dataclasses.replace(cfg.risk, fallback_capital=alloc)
        warn = risk_coherence_warning(cfg.base, cfg.risk)
        if warn:
            print(warn)
        for inst in cfg.instruments:
            if inst.code == code:
                return cls(name, inst, cfg.base, risk_cfg=cfg.risk,
                           risk_mgr=risk_mgr, notify=notify,
                           bt_no_simtick=bt_no_simtick,
                           alloc_total=len(cfg.instruments))
        raise ValueError(f"{code} 不在 {config_path} 的 instruments 中")

    def _unit_var(self) -> float:
        """有效单单位 1N 风险预算 (Phase 1 资本模型 v2)。

        base.unit_var > 0 → 显式配置值 (旧口径, 黄金基线即此);
        = 0 → 派生: 分配资金 × unit_risk_frac (分配资金 =
        portfolio_capital 分配额, 未启用则 fallback_capital)。
        """
        if self._base.unit_var > 0:
            return float(self._base.unit_var)
        cfg = self._risk.cfg
        if cfg.portfolio_capital > 0:
            alloc = self._risk.capital_base(self.name())
        else:
            alloc = float(cfg.fallback_capital)
        frac = float(getattr(cfg, "unit_risk_frac", 0.01) or 0.01)
        return alloc * frac

    def _shadow_size(self, context, n: float, lots: int):
        """资本模型 shadow 观察 (2026-11-10): shadow_capital>0 时按
        25M 分配口径旁路计算新模型手数, 与现行手数对比只记录不改变
        行为 (veto_log 带冷却+首见告警)。验证后翻转正式口径。"""
        cap = float(getattr(self._risk.cfg, "shadow_capital", 0.0) or 0.0)
        if cap <= 0 or lots <= 0 or not (n and n == n and n > 0) \
                or not self._volscale:
            return
        w = (self._risk.cfg.alloc_weights or {}).get(self._inst.code, 1.0)
        # shadow 等权分母用品种数; 带权重时近似按 Σweight=品种数
        alloc = cap * float(w) / max(1.0, float(self._alloc_total))
        frac = float(getattr(self._risk.cfg, "unit_risk_frac", 0.01) or 0.01)
        new_lots = self._sizer.layer_lots(
            n, self._volscale, self._inst.units,
            unit_var=alloc * frac)
        if new_lots != lots:
            self._veto_log(
                context, "shadow_size",
                f"shadow sizing: current={lots} vs 25M-model={new_lots} "
                f"lots (N={n:.2f}, alloc={alloc:,.0f}, "
                f"unit_var={alloc * frac:,.0f}) — 观察期不生效")

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
        """→ execution.Telemetry.alert (A1-2 迁出, 薄转发)"""
        return self._telemetry.alert(key, text)

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
        """→ execution.FillTracker.mark_fill_pending (A1-3 迁出, 薄转发)"""
        return self._fills.mark_fill_pending(context, key, engine)

    def _begin_exit_order(self, context, key: str, engine, dec: dict):
        return self._fills.begin_exit_order(context, key, engine, dec)

    def _cancel_exit_order(self, context, key: str, engine):
        return self._fills.cancel_exit_order(context, key, engine)

    def _begin_dual_exit(self, context, intent, engines):
        return self._fills.begin_dual_exit(context, intent, engines)

    def _cancel_dual_exit(self, context):
        return self._fills.cancel_dual_exit(context)

    def _process_exit_orders(self, context):
        return self._fills.process_exit_orders(context)

    def _exit_order_snapshot(self) -> dict:
        """可 JSON 持久化的出场证据状态（引擎对象按 profile 名恢复）。"""
        out = {}
        for key, fp in self._exit_orders.items():
            if fp.get("batch"):
                row = {k: v for k, v in fp.items()
                       if k not in ("intent", "engines")}
                row["intent"] = fp["intent"].snapshot()
                row["engine_names"] = [s["name"] for s in self._subs
                                       if s["engine"] in fp["engines"]]
                out[key] = row
            else:
                out[key] = {k: v for k, v in fp.items() if k != "engine"}
        return out

    def _restore_exit_orders(self, saved):
        batched_names = set()
        for key, fp in (saved or {}).items():
            if fp.get("batch"):
                engines = [s["engine"] for s in self._subs
                           if s["name"] in fp.get("engine_names", [])]
                batched_names.update(fp.get("engine_names", []))
                self._exit_orders[key] = {
                    **fp, "intent": TargetIntent.restore(fp["intent"]),
                    "engines": engines}
                continue
            sub = self._sub(key)
            if sub is not None and sub["engine"]._exit_pending is not None:
                self._exit_orders[key] = {**fp, "engine": sub["engine"]}
        # 兼容只含引擎 exit_pending、缺外层证据字段的恢复快照：宁可
        # 进入 UNKNOWN 等待人工/新证据，也不能静默当作已平仓。已属于
        # __dual__ 批次的 profile 不得再补一条单 profile 记录，否则批次
        # 成交后该记录会把仍持仓的其他 profile 风险账误清。
        for sub in self._subs:
            ep = sub["engine"]._exit_pending
            if ep is not None and sub["name"] not in self._exit_orders \
                    and sub["name"] not in batched_names:
                self._exit_orders[sub["name"]] = {
                    "engine": sub["engine"], "ts": time.time(),
                    "lots": ep.get("lots", 0),
                    "direction": ep.get("direction", 0),
                    "decision_price": ep.get("price"),
                    "reason": ep.get("reason", "unknown"),
                    "cursor": None,
                }

    # ------------------------------------------------------------------
    # enter/add write-ahead 目标意图 (outbox)
    # ------------------------------------------------------------------
    def _begin_position_intent(self, context: CtaContext, kind: str,
                               sub: dict, dec: dict, lots: int):
        """实盘 enter/add：目标意图先于绝对目标单持久化。

        只登记，不确认引擎；返回 intent id。回测返回 None 且不改变任何
        行为。崩溃恢复时根据框架目标仓与真实成交证据决定重放/确认/UNKNOWN。
        """
        if self._is_backtest:
            return None
        code = self._inst.code
        engine = sub["engine"]
        before = int(round(context.stra_get_position(code)))
        d = int(dec.get("direction") or engine.direction)
        self._outbox_seq += 1
        get_day = getattr(context, "stra_get_date", None)
        tdate = None
        if get_day is not None:
            try:
                tdate = int(get_day())
            except (TypeError, ValueError):
                tdate = None
        journal = self._trade_journal_source()
        rec = {
            "id": f"{self.name()}#{self._outbox_seq}",
            "kind": kind,
            "profile": sub["name"],
            "intent": {"code": code, "before": before,
                       "after": before + d * int(lots)},
            "decision": dict(dec),
            "lots": int(lots),
            "state": "new",
            "ts": time.time(),
            "tdate": tdate,
            "cursor": self._fills.journal_cursor(journal),
            "truth0": self._truth_net(code),
            "fees0": self._fills.get_fees(context),
        }
        self._open_intents[rec["id"]] = rec
        # 同步进订单级归因队列 (先于目标单; 与快照同一持久化时点)。
        self._attr_register_intent(context, rec)
        # 意图必须先落盘；set_position 之后即使 confirm 前崩溃，也能恢复。
        # 直测桩可能未实现 user_save_data；真实框架上下文必定实现。
        try:
            self._persist(context)
        except AttributeError:
            pass
        return rec["id"]

    def _complete_position_intent(self, intent_id: str):
        if intent_id is not None:
            self._open_intents.pop(intent_id, None)
            if self._attr is not None:
                # 引擎已同步确认: 归因队列出队, 簿镜像保留在 after。
                self._attr.attr.complete(intent_id)

    def _position_intent_snapshot(self) -> dict:
        return {k: {kk: vv for kk, vv in rec.items() if kk != "engine"}
                for k, rec in self._open_intents.items()}

    def _restore_position_intents(self, saved, seq: int):
        self._open_intents = {}
        self._outbox_seq = int(seq or 0)
        for iid, rec in (saved or {}).items():
            if isinstance(rec, dict) and isinstance(rec.get("intent"), dict):
                self._open_intents[str(iid)] = dict(rec)

    # ------------------------------------------------------------------
    # 订单级归因链 (localid attribution, 零 C++ 改动)
    # ------------------------------------------------------------------
    def _attr_ensure(self, context: CtaContext):
        """惰性构建 ExecAttribution (仅实盘; 回测恒 None)。

        数据源: 侧车 exec_*.jsonl (A5-1 已部署) + trades.csv (TraderAdapter
        同步落盘, 路径与真值源同目录)。簿镜像初值取券商多空两簿 (rtdata
        positions, 精确到多空), 真值缺失时退化引擎净仓; 漂移由意图 before
        校准兜住, 不会假确认。"""
        if self._attr is not None or self._is_backtest:
            return
        from core.OrderAttributor import ExecAttribution
        from execution.paths import GROUP_DIR
        jdir = os.path.join(GROUP_DIR, "generated", "exec_journal")
        tcsv = ""
        t = self._truth_source()
        if t is not None and t.path:
            tcsv = os.path.join(os.path.dirname(t.path), "trades.csv")
        self._attr = ExecAttribution(self._inst.code, jdir, tcsv)
        broker_pos = None
        if t is not None and t.ok:
            try:
                broker_pos = t.positions()
            except Exception:
                broker_pos = None
        self._attr.init_book(broker_pos,
                             fallback_net=float(self._combined_target()))
        # 恢复路径: 已持久化的 enter/add 意图按落盘顺序重放进队列
        for rec in self._open_intents.values():
            intent = rec.get("intent") or {}
            try:
                self._attr.on_intent(
                    rec["id"], int(intent["before"]), int(intent["after"]),
                    float(rec.get("ts") or 0.0))
            except (KeyError, TypeError, ValueError):
                continue
        # 已持久化的出场证据: 单 profile 按 key, 批次按 __dual__
        for key, fp in self._exit_orders.items():
            try:
                if fp.get("batch"):
                    it = fp["intent"]
                    self._attr.on_intent("__dual__", int(it.before),
                                         int(it.after),
                                         float(fp.get("ts") or 0.0))
                else:
                    d = 1 if float(fp.get("direction") or 0) > 0 else -1
                    lots = int(round(float(fp.get("lots") or 0)))
                    before = self._combined_target()
                    self._attr.on_intent(str(key), before,
                                         before - d * lots,
                                         float(fp.get("ts") or 0.0))
            except (KeyError, TypeError, ValueError):
                continue

    def _attr_register_intent(self, context: CtaContext, rec: dict):
        """write-ahead 意图同步进归因队列 (先于目标单, 与快照同一次持久化)。"""
        self._attr_ensure(context)
        if self._attr is None:
            return
        try:
            self._attr.on_intent(rec["id"], int(rec["intent"]["before"]),
                                 int(rec["intent"]["after"]),
                                 float(rec.get("ts") or 0.0))
        except (KeyError, TypeError, ValueError):
            pass

    def _attr_feed(self, context: CtaContext) -> bool:
        """增量事件喂入归因器; 有未匹配成交 (守恒破坏) → 告警, 保持 UNKNOWN。

        纯出场场景 (重启后无 enter/add) 首次调用时惰性构建并回放已持久化
        意图, 出场确认同样享受订单级证据。"""
        if self._attr is None:
            self._attr_ensure(context)
        if self._attr is None:
            return False
        n = self._attr.poll()
        if n > 0:
            bad = self._attr.attr.unmatched[-n:]
            codes = sorted({str(x.get("code")) for x in bad})
            txt = (f"{self.name()} attribution unmatched fills: {n} 笔 "
                   f"({','.join(codes)}) — 守恒破坏, 相关意图保持 UNKNOWN")
            context.stra_log_text(f"{self.name()} {txt}", level=3)
            self._alert("attribution_mismatch", txt)
        return True

    def _attr_progress(self, iid: str):
        if self._attr is None:
            return None
        return self._attr.progress(iid)

    def _process_open_intents(self, context: CtaContext):
        """推进 enter/add outbox；未知时保持封锁，绝不按框架信号假成交。"""
        if not self._open_intents:
            return
        self._attr_feed(context)
        code = self._inst.code
        get_day = getattr(context, "stra_get_date", None)
        cur_day = None
        if get_day is not None:
            try:
                cur_day = int(get_day())
            except (TypeError, ValueError):
                cur_day = None
        changed = False
        for iid, rec in list(self._open_intents.items()):
            if cur_day is not None and rec.get("tdate") is not None \
                    and int(rec["tdate"]) != cur_day:
                sub = self._sub(rec.get("profile"))
                if sub is not None:
                    sub["engine"].clear_pending()
                txt = (f"{code} {rec['kind']} intent expired on day change "
                       f"({rec.get('tdate')} -> {cur_day})")
                context.stra_log_text(f"{self.name()} {txt}", level=2)
                self._alert("intent_expired", txt)
                self._open_intents.pop(iid, None)
                if self._attr is not None:
                    self._attr.discard(iid)
                changed = True
                continue

            target = int(rec["intent"]["after"])
            before = int(rec["intent"]["before"])
            try:
                fw = int(round(context.stra_get_position(code)))
            except Exception:
                continue
            sub = self._sub(rec.get("profile"))
            if sub is None:
                self._open_intents.pop(iid, None)
                changed = True
                continue
            e = sub["engine"]

            # W1/提交丢失：绝对目标重放是幂等的，不重新走触发链，避免用
            # 重启后的新 tick 价替代原决策价。
            if fw == before and rec.get("state") == "new":
                tag = rec.get("decision", {}).get("tag") or (
                    "Enter" if rec["kind"] == "enter" else "Add")
                try:
                    context.stra_set_position(code, target, tag)
                    rec["state"] = "submitted"
                    try:
                        self._persist(context)
                    except AttributeError:
                        pass
                except Exception:
                    context.stra_log_text(
                        f"{self.name()} intent {iid} resubmit failed", level=3)
                continue

            if fw != target:
                if not rec.get("alerted"):
                    rec["alerted"] = True
                    txt = (f"{code}[{rec['profile']}] {rec['kind']} intent "
                           f"UNKNOWN: fw={fw}, want={target}, "
                           f"from={before}; blocked")
                    context.stra_log_text(f"{self.name()} {txt}", level=3)
                    self._alert("intent_unknown", txt)
                    changed = True
                continue

            # 订单级归因证据 (2026-10-29, 优先于时序/fees 链): localid
            # 绑定的全部真实成交到位 → 以精确 vwap 确认。失败静默回落
            # 下方既有证据链 (truth→逐笔时序→fees), 任何不确定性都保持
            # UNKNOWN, 绝不假确认。
            ap = self._attr_progress(iid)
            if ap is not None and ap.get("done"):
                dec = rec.get("decision") or {}
                d = int(dec.get("direction") or e.direction)
                lots = int(rec.get("lots") or 0)
                price = float(ap.get("vwap") or dec.get("price") or 0.0)
                n = float(dec.get("n") or e._n or 1.0)
                already = False
                if e.direction == d and e.layers:
                    last_l, last_p = e.layers[-1]
                    already = int(last_l) == lots and abs(last_p - price) < 1e-9
                if not already:
                    if rec["kind"] == "enter":
                        e.confirm_enter(d, lots, price, n)
                    else:
                        e.confirm_add(lots, price, n)
                fp = {"engine": e, "ts": float(rec.get("ts") or time.time()),
                      "fees0": rec.get("fees0"),
                      "lots": float(rec.get("lots") or 0),
                      "cursor": rec.get("cursor"),
                      "truth0": rec.get("truth0"), "skip": 0.0}
                self._fill_pending[rec["profile"]] = fp
                self._sync_risk_book(n)
                self._open_intents.pop(iid, None)
                changed = True
                context.stra_log_text(
                    f"{self.name()}[{rec['profile']}] {rec['kind']} intent "
                    f"confirmed evidence=orders vwap={price:.2f}")
                continue

            # 目标账已到位只说明订单已提交；必须走与运行时一致的成交证据链。
            fp = {"engine": e, "ts": float(rec.get("ts") or time.time()),
                  "fees0": rec.get("fees0"),
                  "lots": float(rec.get("lots") or 0),
                  "cursor": rec.get("cursor"),
                  "truth0": rec.get("truth0"), "skip": 0.0}
            confirmed, source, _pos = self._fill_confirmed(
                context, fp, code, target)
            if not confirmed:
                waited = time.time() - float(rec.get("ts") or 0)
                if waited > 600 and not rec.get("alerted"):
                    rec["alerted"] = True
                    txt = (f"{code}[{rec['profile']}] {rec['kind']} "
                           f"intent unconfirmed {waited:.0f}s; blocked")
                    context.stra_log_text(f"{self.name()} {txt}", level=3)
                    self._alert("intent_unconfirmed", txt)
                    changed = True
                continue

            dec = rec.get("decision") or {}
            d = int(dec.get("direction") or e.direction)
            price = float(dec.get("price") or 0.0)
            n = float(dec.get("n") or e._n or 1.0)
            lots = int(rec.get("lots") or 0)
            already = False
            if e.direction == d and e.layers:
                last_l, last_p = e.layers[-1]
                already = int(last_l) == lots and abs(last_p - price) < 1e-9
            if not already:
                if rec["kind"] == "enter":
                    e.confirm_enter(d, lots, price, n)
                else:
                    e.confirm_add(lots, price, n)
            self._fill_pending[rec["profile"]] = fp
            self._sync_risk_book(n)
            self._open_intents.pop(iid, None)
            changed = True
            context.stra_log_text(
                f"{self.name()}[{rec['profile']}] {rec['kind']} intent "
                f"confirmed evidence={source}; target restored")
        if changed:
            try:
                self._persist(context)
            except AttributeError:
                pass

    def _truth_net(self, code: str):
        """→ execution.TruthMonitor.truth_net (A1-4 迁出, 薄转发)"""
        return self._truthmon.truth_net(code)

    def _trade_journal_source(self):
        """→ execution.TruthMonitor.trade_journal_source (A1-4 迁出, 薄转发)"""
        return self._truthmon.trade_journal_source()

    def _journal_layer_price(self, code: str, fp: dict, engine, alloc: float):
        """→ execution.FillTracker.journal_layer_price (A1-3 迁出, 薄转发)"""
        return self._fills.journal_layer_price(code, fp, engine, alloc)

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
        self._risk.register_sid_context(self.name(), context)

        # 回测隔离 (2026-10-20): 先于任何真值探测置位。
        # 回测环境的 generated/traders/<id>/rtdata.json 可能是同机实盘
        # 进程刚写的"新鲜"文件 — 时效闸挡不住, 必须按运行模式禁用。
        self._is_backtest = bool(getattr(context, "is_backtest", False))
        self._mode = StrategyMode.resolve(self._base, self._inst,
                                          self._is_backtest,
                                          self._bt_no_simtick)
        if self._is_backtest:
            self._truth = None
            self._truth_probed = True
            self._truth_retry_ts = float("inf")
            context.stra_log_text(
                f"{self.name()} backtest mode: truth source disabled "
                f"(framework caliber only)")

        # h1 一期 (2026-10-21): 无原生 h1 K 线 (WtDt 只合成 min1/
        # min5/day, his 重建对运行进程不可见), 策略层 m5→h1 聚合
        # (core/aggregate, backtest/live 同源); 主K=m5, on_calculate
        # 每 m5 闭合驱动, 信号序列=聚合 h1。intrabar_stop 对 h1 无意义
        # (m5 已是驱动主K), 忽略之。
        path = self._mode.calc_path          # A2: 模式面单一决策点
        self._main_period = self._mode.main_period
        if path == "h1":
            context.stra_prepare_bars(code, self._main_period,
                                      self._h1_m5_cnt(), isMain=True)
        elif path == "intrabar":
            # 盘中止损模式: m5 主K驱动盘中扫描; 信号周期(d1)作副K —
            # 副K必须显式订阅 (实盘 C++ 未订阅返回 None →
            # _on_calc_intrabar 首行静默 return 零评估零扫描,
            # 2026-11-12 激活包 intrabar 首活实锤, 注释原设计意图
            # 即"信号周期作副K" 实现漏订)
            context.stra_prepare_bars(code, self._main_period,
                                      p.intrabar_cnt, isMain=True)
            context.stra_prepare_bars(code, self._inst.period,
                                      p.bar_cnt, isMain=False)
        else:
            context.stra_prepare_bars(code, self._main_period,
                                      p.bar_cnt, isMain=True)

        # trigger 模式: 订阅 tick 驱动盘中触发; 日线主K时另备 m5 副K
        # (实盘日线不闭合 F3 → session_end 用 m5 聚合合成当日日K评估)
        # bt_no_simtick: bt 且无 m5 数据 → 不订 tick/副K, 触发判定走
        # on_bar check_bar, 撮合退化为 bar 收盘口径
        if self._mode.wants_tick_feed:
            context.stra_sub_ticks(code)
        # m5 副K: trigger 盘中触发 + 实盘日线 session_end 合成日K 共同
        # 原料 (F3: 实盘 d1 永不闭合, trigger 武装与 close 收盘决策都
        # 依赖会话末 m5 聚合; close+live 是 2026-11-11 Phase1 激活包的
        # 新组合)
        if self._mode.calc_path == "daily" and (
                self._mode.wants_ticks
                or (self._mode.session_flush == "daily_synth"
                    and not self._is_backtest)):
            context.stra_prepare_bars(code, p.intrabar_period,
                                      p.intrabar_cnt, isMain=False)
        elif self._mode.entry_mode == "trigger":
            context.stra_log_text(
                f"{self.name()} bt_no_simtick: trigger 判定走 "
                f"check_bar, 撮合=bar收盘口径 (无 m5 模拟tick)")

        # Phase 1 (2026-11-10): contract 信号源解析 + 订阅主力合约 d1 副K
        self._resolve_signal_source(context)

        comm = context.stra_get_comminfo(code)
        self._volscale = float(comm.volscale)
        self._pricetick = float(comm.pricetick)

        # === 图表注册 ===
        context.set_chart_kline(
            code, "m5" if self._inst.period == "h1"
            else self._inst.period)
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

        # === 状态恢复 (v5=filter指纹; v4/v3/v2/v1 兼容, v4 自动迁移) ===
        raw = context.user_load_data(f"{self.name()}_engine", "", str)
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("v") in (3, 4, 5):
                    # 原名找不到会静默漏恢复，随后净仓可能被收养成
                    # 首 profile 单层。身份/语义不一致必须 fail-loud。
                    validate_snapshot(data, self._profiles)
                    for nm, snap in (data.get("subs") or {}).items():
                        sub = self._sub(nm)
                        sub["engine"].restore(snap)
                    self._d1_date = data.get("d1_date")
                    self._m5_key = data.get("m5_key")
                    self._h1_key = data.get("h1_key")
                    self._h1_m5_key = data.get("h1_m5_key")
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
                self._restore_exit_orders(data.get("exit_orders"))
                # 对账计数跨重启持久 (2026-11-02 生产事故根治): 生产
                # 每日双重启 (08:47 盘前 + 20:40 hotdaily) 使进程内
                # _recon_miss 归零, 而 reconcile 只在日评估路径运行、
                # 每日仅 1 次 → 失配永远无法累计到 recon_patience,
                # l/pp 引擎陈旧层账因此潜伏 7 个交易日零触发。
                self._recon_miss = int(data.get("recon_miss") or 0)
                if int(data.get("v") or 0) >= 4:
                    self._restore_position_intents(data.get("intents"),
                                                   data.get("outbox_seq"))
                    saved_def = data.get("deferred") or {}
                    if isinstance(saved_def, dict):
                        self._deferred_decisions = {
                            str(k): [dict(x) for x in v]
                            for k, v in saved_def.items()
                            if isinstance(v, list)}
                context.stra_log_text(
                    f"{self.name()} restored: dir={self._engine.direction}, "
                    f"layers={len(self._engine.layers)}")
            except (json.JSONDecodeError, TypeError, ValueError,
                    KeyError) as e:
                # Phase 0 (2026-11-09): 快照损坏不再静默继续 — 进入
                # DEGRADED (禁止新开/加仓, 出场保留), 告警每日一次。
                # 注意 on_init 内 _alert 走 notifier + alerts.log 双通道;
                # adopt/reconcile 路径维持原样 (近似管理 > 完全裸奔)。
                self._state_degraded = True
                context.stra_log_text(
                    f"{self.name()} state restore failed: {e} "
                    f"→ DEGRADED (no new entries/adds until re-persist)",
                    level=3)
                try:
                    self._alert("state_degraded",
                                f"state restore failed: {e}; "
                                f"new entries/adds blocked")
                except Exception:
                    pass

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
        if pos != 0 and self._combined_target() == 0 \
                and not self._open_intents:
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
        # (2026-09-04 审计发现)。按恢复/收养的引擎持仓逐 profile
        # 重建记账；单位数仍由 code 级 meta 去重。
        if self._risk.has_position(code):
            self._risk.on_closed(code)       # 清掉旧进程可能残留的各 profile
        restored_lots = 0
        restored_risk = 0.0
        for s in self._subs:
            e = s["engine"]
            if e.direction == 0 or not e.layers:
                continue
            L = sum(l for l, _ in e.layers)
            avg = sum(l * pr for l, pr in e.layers) / L
            n0 = e._n if e._n is not None and np.isfinite(e._n) else 1.0
            risk0 = e.booked_risk(self._volscale or 1.0,
                                  p.stop_atr_mult,
                                  stop=e.stop_anchor())
            self._risk.sync_position(
                code, e.direction, L, avg, n0, self._volscale or 1.0,
                p.stop_atr_mult, self._inst.group,
                risk=risk0,       # 锁盈到 0 也是有效风险，不得回退默认 2N
                sid=self.name(), units=float(self._inst.units),
                profile=s["name"])
            restored_lots += L
            restored_risk += risk0
        if restored_lots > 0:
            context.stra_log_text(
                f"{self.name()} risk book restored: "
                f"lots={restored_lots} risk={restored_risk:.0f} "
                f"profiles={len(self._subs)} (stop caliber)")

        # 引擎层账 vs 信号持仓启动失配播种 (2026-11-02 生产事故根治,
        # 与快照内 recon_miss 持久化配套): 经快照恢复+收养后引擎净仓
        # 仍与信号仓不符 → 差异已跨重启存续, 非秒级在途单可自愈, 把
        # 对账计数顶到 patience-1, 下一次日评估即触发重建
        # (TruthMonitor.reconcile)。生产每日双重启下不播种则计数
        # 每天归零、永远无法累计到 patience (l/pp 脱管潜伏一周的
        # 根因); 在途单场景的重建目标=信号仓, 偏差由
        # fill_timeout/ghost_fill 告警链兜底观测。
        try:
            fw_pos = int(round(float(context.stra_get_position(code))))
            eng_net = self._combined_target()
            if fw_pos != eng_net:
                seed = max(0, int(self._base.recon_patience) - 1)
                if self._recon_miss < seed:
                    self._recon_miss = seed
                context.stra_log_text(
                    f"{self.name()} restored with engine/book mismatch "
                    f"(engine net={eng_net} vs signal {fw_pos}), "
                    f"reconcile seeded to {self._recon_miss}/"
                    f"{int(self._base.recon_patience)}, next daily eval "
                    f"rebuilds", level=3)
                self._alert("reconcile_seed",
                            f"engine net={eng_net} vs signal {fw_pos}, "
                            f"rebuild on next daily eval")
        except Exception:
            pass

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
            # Phase 0: 启动即落首帧心跳, watchdog 从进程拉起第一分钟起
            # 就有存活判据 (否则要等首个 tick/session 事件才可见)
            self._write_heartbeat(context, force=True)

    def on_calculate(self, context: CtaContext):
        self._in_schedule = True
        try:
            self._process_open_intents(context)
            self._process_exit_orders(context)
            if self._exit_orders or self._open_intents:
                # 目标/出场未确认：禁止评估/重入，仅推进证据并落快照。
                self._persist(context)
                return
            path = self._mode.calc_path
            if path == "h1":
                self._on_calc_h1(context)
            elif path == "intrabar":
                self._on_calc_intrabar(context)
            else:
                self._on_calc_daily(context)
        finally:
            self._in_schedule = False
            self._write_heartbeat(context)

    # ------------------------------------------------------------------
    # h1 路径 (2026-10-21 一期): 策略层 m5→h1 聚合评估
    # ------------------------------------------------------------------
    def _h1_m5_cnt(self) -> int:
        """h1 暖机所需 m5 根数: 信号 bar 需求 × 每交易日约 280 根 m5,
        留 5 根 h1 余量 (约 40 个交易日滑动窗口)"""
        return (self._need_bars() + 5) * 40

    def _build_h1_series(self, context: CtaContext):
        """m5 滑动窗口全量重建 h1 序列 (无状态; 几千根聚合开销可忽略)。
        交易日历取 his d1 (夜盘归属映射用)。数据不足返回 None。
        返回 (h1, mbars, mkeys): h1 含 keys/closed_by/bar_bucket
        (m5 bar → 闭合桶映射, 时序交错评估用), mkeys 为 m5 浮点键。"""
        code = self._inst.code
        p = self._base
        need = self._need_bars()
        mbars = context.stra_get_bars(code, p.intrabar_period,
                                      self._h1_m5_cnt(), isMain=True)
        if mbars is None or len(mbars) < (need + 2) * 12:
            return None
        d1bars = context.stra_get_bars(code, "d1", 40)
        days_f = self._nd_field(d1bars, "date") \
            if d1bars is not None else None
        if days_f is None or len(days_f) == 0:
            return None
        days = sorted({int(d) for d in days_f})
        dates = self._nd_field(mbars, "date")
        times = self._nd_field(mbars, "time")
        if dates is None or times is None or len(dates) != len(times):
            return None
        highs = np.asarray(mbars.highs, dtype=float)
        lows = np.asarray(mbars.lows, dtype=float)
        closes = np.asarray(mbars.closes, dtype=float)
        opens = getattr(mbars, "opens", None)
        vols = getattr(mbars, "volumes", None)
        ois = self._extract_oi(mbars)
        rows = []
        for i in range(len(dates)):
            o = float(opens[i]) if opens is not None \
                else float(closes[i])
            v = float(vols[i]) if vols is not None else 0.0
            oi = float(ois[i]) if ois is not None and i < len(ois) \
                else 0.0
            rows.append((int(dates[i]), norm_m5_time(times[i]), o,
                         float(highs[i]), float(lows[i]),
                         float(closes[i]), v, oi))
        h1 = aggregate_h1(rows, days)
        if len(h1["keys"]) < need + 1:     # 尾部桶不计入闭合需求
            return None
        mkeys = self._bar_keys(mbars)
        if mkeys is None:
            return None
        return h1, mbars, mkeys

    def _on_calc_h1(self, context: CtaContext):
        """h1 主评估: m5 增量逐根推进, 桶闭合即评估。

        - 首 call 建基线 (锚到最新闭合桶, 不回放历史, 与 _m5_key 哲学
          一致); bt 逐日推进时每日的新桶区间随之展开 —— 闭合桶**全部**
          依序评估+触碰仿真, 与 live 逐小时驱动时序等价;
        - live 由 min1 schedule 高频进入, 每 call 通常只有 1 根新 m5;
          hang 恢复一次多桶时**只评最新** (决策已随时间失效, 与
          clear_pending 跨日自愈同一哲学), 其余 WARN 跳过;
        - trigger 的盘中触碰: live 走 on_price(tick) (本方法不重复
          仿真, 防 m5 粒度重复触发); bt 无盘中 schedule, 每根新 m5
          bar 闭合桶评估后接着做 check_bar 触碰仿真 (_run_trigger_bar),
          时序与 live tick 驱动一致。"""
        built = self._build_h1_series(context)
        if built is None:
            return
        h1, mbars, mkeys = built
        keys = h1["keys"]
        n_closed = h1["n_closed"]
        # 末根 m5 bar 下标 → 桶 j (桶在末根到达时闭合; 调用方按
        # j < n_closed 过滤尾部桶)
        closure_at = {}
        for j, lb in enumerate(h1["bucket_last_bar"]):
            if lb >= 0:
                closure_at[lb] = j
        is_bt = bool(getattr(context, "is_backtest", False))
        trig = self._base.entry_mode == "trigger"
        if self._h1_m5_key is None:
            self._h1_m5_key = float(mkeys[-1])
            if n_closed > 0:
                self._h1_key = int(keys[n_closed - 1])
                self._d1_date = int(h1["dates"][n_closed - 1])
            # 基线锚点立即持久化: 崩溃重启不重复基线/不丢推进位置
            self._persist(context)
            return
        opens = getattr(mbars, "opens", None)
        for i in range(len(mkeys)):
            if float(mkeys[i]) <= float(self._h1_m5_key):
                continue
            j = closure_at.get(i)
            # bt trigger 先用旧武装消费当前 m5，再闭合 h1 并武装未来；
            # 否则会用本 m5 收盘后才知道的 h1 通道回溯此前价格。
            if is_bt and trig:
                o = float(opens[i]) if opens is not None \
                    else float(mbars.closes[i])
                self._run_trigger_bar(context, o, float(mbars.highs[i]),
                                      float(mbars.lows[i]),
                                      float(mbars.closes[i]))
            if j is not None and j < n_closed and \
                    int(keys[j]) > int(self._h1_key):
                self._eval_h1_bar(context, h1, j)
            self._h1_m5_key = float(mkeys[i])
        # sweep 兜底: 闭合检测依赖"末根 bar 在本 call 内被消费"; 若末根
        # 恰好是基线前最后一根 (live hang 恢复的 15:00→15:05 场景),
        # 桶虽闭合但无 closure 事件 → 按锚点差补检。bt: 依序全评;
        # live: hang 窗口内闭合的桶不补评 (决策已失效), 只评最新。
        newly = [j for j in range(n_closed)
                 if int(keys[j]) > int(self._h1_key)]
        if newly:
            if is_bt:
                for j in newly:
                    self._eval_h1_bar(context, h1, j)
            else:
                for j in newly[:-1]:
                    context.stra_log_text(
                        f"{self.name()} h1: bucket {keys[j]} closed while "
                        f"down, skipped (no replay)", level=2)
                self._eval_h1_bar(context, h1, newly[-1])

    def _eval_h1_bar(self, context: CtaContext, h1: dict, i: int):
        """评估单根 h1 桶: 指标/引擎/执行全部走 _eval_core 统一骨架
        (双系统/多速度/过滤器/风控与 d1 完全同路径)。

        锚点语义: _h1_key=已评估桶键 (持久化, 重启重放不二次消费);
        _d1_date 跟随信号交易日 (日级机制 —— stale pending 清理/
        session_end 防重入 —— 照旧工作)。h1 合成序列**不写 journal**
        (回补路径只认 d1/m5, storage m5 本身就是数据源)。"""
        import types
        tdate = int(h1["dates"][i])
        if self._d1_date is not None and int(self._d1_date) != tdate:
            self._drop_stale_pending(context, f"h1_eval {tdate}")
        closes = np.asarray(h1["closes"][:i + 1], dtype=float)
        highs = np.asarray(h1["highs"][:i + 1], dtype=float)
        lows = np.asarray(h1["lows"][:i + 1], dtype=float)
        bars = types.SimpleNamespace(
            closes=closes, highs=highs, lows=lows,
            opens=np.asarray(h1["opens"][:i + 1], dtype=float),
            volumes=np.asarray(h1["volumes"][:i + 1], dtype=float),
            open_interests=np.asarray(h1["ois"][:i + 1], dtype=float),
            dates=np.asarray(h1["dates"][:i + 1], dtype=np.int64))
        h, l, c = float(highs[-1]), float(lows[-1]), float(closes[-1])
        prev_close = float(closes[-2])
        atr, n, chan = self._eval_core(
            context, bars, highs, lows, closes,
            h, l, c, prev_close, with_oi=True, synth="h1")
        if n is None:
            return
        self._h1_key = int(h1["keys"][i])
        self._d1_date = tdate
        try:
            context.stra_log_text(
                f"{self.name()} h1 eval: bucket={self._h1_key} "
                f"series={len(closes)} [{int(h1['dates'][0])}~{tdate}] "
                f"n={n:.2f}")
        except Exception:
            pass
        self._post_calculate(context, closes, n, chan)

    def _h1_flush_eval(self, context: CtaContext, curTDate: int):
        """session_end 尾部桶闭合评估: 尾部 h1 桶在会话末无下根 m5
        触发闭合 (实盘/ bt 同), 与 _session_eval_daily 的 d1 合成同构。
        锚点已覆盖则幂等跳过 (bt 末根 m5 的 on_calculate 与 session_end
        可能先后到达, _h1_key 保证只评一次)。"""
        built = self._build_h1_series(context)
        if built is None:
            return
        h1 = built[0]
        if not h1["keys"]:
            return
        i = len(h1["keys"]) - 1
        key = int(h1["keys"][i])
        if self._h1_key is not None and key <= int(self._h1_key):
            return
        self._eval_h1_bar(context, h1, i)

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

        # Phase 1: contract 信号源替换指标窗口 (None=窗口不足, 跳过)
        sig = self._signal_arrays(context, bars, highs, lows, closes)
        if sig is None:
            return
        highs, lows, closes = sig

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
        (session_end 合成路径没有合成 bar 的成交量, 但 his bars 有;
        传 True 让 LiquidityFloor 使用最近完整 bar 的数据, 而不是
        fail-open)。
        返回 (atr, n, chan); n 无效 → (None, None, None), 调用方须
        直接返回, 不得继续任何后续步骤。
        """
        atr, n, chan = self._calc_indicators(highs, lows, closes)
        if n is None:
            return None, None, None
        # S4-3: 引擎层跳空止损需要当根开盘价 (bars 无 opens 时传 None,
        # 引擎自动退化为旧"按止损价"行为)
        _opens = getattr(bars, "opens", None)
        o = float(_opens[-1]) if _opens is not None else None
        self._set_n(n)
        self._log_eval_window(context, bars, closes, n, synth)
        # 2026-10-20: 先做末层精确回写, 再做换月/漂移的全层平移。
        # 旧顺序 (roll 先于 fill) 会在 _fill_pending 未清时把末层误差
        # 摊到所有层, 再修末层 → 首层被污染; 与 _reanchor_actual_fill
        # docstring 的"先精确修末层"也相反。
        self._reanchor_actual_fill(context)
        self._reanchor_on_roll(context)
        fctx = FilterContext(
            code=self._inst.code, price=float(c), n=n, n_series=atr,
            highs=highs, lows=lows, closes=closes,
            volumes=getattr(bars, "volumes", None) if with_oi else None,
            open_interests=self._extract_oi(bars) if with_oi else None)
        if self._is_dual:
            dec_map = {s["name"]: s["engine"].on_bar(
                h, l, c, prev_close, n, chan[s["name"]], open=o)
                for s in self._subs}
            self._execute_dual(context, dec_map, n, fctx)
        else:
            # 单子系统: key 用子系统名而不是 inst.system — speeds 的
            # name 可以是 fast/slow (2026-10-20 多速度修复)
            for dec in self._engine.on_bar(h, l, c, prev_close, n,
                                           chan[self._subs[0]["name"]],
                                           open=o):
                self._execute(context, dec, n, fctx)
        # ratchet/成交价变化后统一刷新实际止损口径风险，避免账本长期
        # 虚占或低估。同步是纯覆盖操作，单/双 profile 均幂等。
        self._sync_risk_book(n)
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
                'prev_exit_upper': float(xu[-2]),
                'prev_exit_lower': float(xl[-2]),
                'prev_fs_upper': float(fu[-2]),
                'prev_fs_lower': float(fl[-2]),
            }

        chan = {}
        for sub in self._subs:
            # 每子系统自己的窗口 (多速度); 缺省沿用 S1/S2 全局口径
            # 用 system 而非 name 判定 S2 (name 可为 fast/slow)
            dflt = (p.s2_entry, p.s2_exit) if sub.get("system") == "S2" \
                else (p.s1_entry, p.s1_exit)
            en = int(sub.get("entry") or dflt[0])
            ex = int(sub.get("exit") or dflt[1])
            fsp = int(sub.get("fs") or p.s2_entry)
            a, b = ch(en)
            c2, d2 = ch(ex)
            f1, f2 = ch(fsp)
            current = _mk(a, b, c2, d2, f1, f2)
            # trigger 次日常规通道 = 最近 N 根（含刚闭合 bar），与
            # close 的 current/prev 分离；arm_exclude 时不用 next_*。
            nxt = {
                "entry_upper": float(np.max(highs[-en:])),
                "entry_lower": float(np.min(lows[-en:])),
                "exit_upper": float(np.max(highs[-ex:])),
                "exit_lower": float(np.min(lows[-ex:])),
                "fs_upper": float(np.max(highs[-fsp:])),
                "fs_lower": float(np.min(lows[-fsp:])),
            }
            chan[sub["name"]] = with_next_channels(current, nxt)
        return atr, n, chan

    def _need_bars(self) -> int:
        """暖机所需信号周期 bar 数：与 ConfigLoader 共用 profiles 口径，
        覆盖 base/全部 speed 窗口 + ATR + prev 通道余量。"""
        return required_bars(self._inst, self._base)

    # ------------------------------------------------------------------
    # Phase 1 (2026-11-10): 合约信号源 — 移仓后新合约自身序列, 无复权
    # ------------------------------------------------------------------
    def _sig_source(self) -> str:
        """有效信号源: 品种级覆盖优先, 空则继承 base (Phase 1)。"""
        return self._inst.signal_source or self._base.signal_source

    def _resolve_signal_source(self, context: CtaContext):
        """contract 模式信号源解析 (Phase 1, 无复权):

        - 回测: 读 common/roll_calendar.json, **逐日主力** —— 每个历史
          日期的指标/通道在当日主力合约自身真实序列上计算 (移仓即切,
          无价格调整); init 时预订阅窗口内全部主力合约副K。
        - 实盘: hots.json 当前主力单合约 (与每日 20:35 hots 更新 +
          20:40 重启的运维节奏配套; 盘中换月由 _check_sig_roll 告警)。
        执行/持仓始终是 HOT, 由框架换月映射; 触发位即真实可交易价。
        """
        if self._sig_source() != "contract":
            return
        common = find_common(_GROUP_DIR)
        if self._is_backtest:
            cal = self._load_roll_calendar(common)
            if cal:
                self._sig_calendar = cal
                self._sig_contract = self._full_contract(cal[-1][2])
                context.stra_log_text(
                    f"{self.name()} signal source: roll_calendar "
                    f"{len(cal)} segments (逐日主力, 无复权; 数据经 "
                    f"contracts 缓存直读, 不走 C++ 副K; 执行仍为 "
                    f"{self._inst.code})")
                return
        hots = load_hots(os.path.join(common, "hots.json"))
        cur = parse_contracts(hots, [self._inst.code]).get(self._inst.code)
        if not cur:
            txt = (f"{self._inst.code} signal_source=contract 但 "
                   f"hots.json/roll_calendar 均无主力信息 — 退化为 HOT")
            context.stra_log_text(f"{self.name()} {txt}", level=3)
            self._alert("signal_source_fallback", txt)
            return
        self._sig_contract = self._full_contract_from_contract(cur)
        context.stra_prepare_bars(self._sig_contract, self._inst.period,
                                  self._base.bar_cnt, isMain=False)
        # 注意: 主力合约**不订 m5 副K** —— session_end 合成日K的原料
        # 统一读 HOT m5 (主K级成熟订阅, 当日 HOT=当日主力映射, 等价
        # 真实价口径); 2026-11-12 实锤新订阅副K命中 C++ reader 拼装
        # 缓存冻结 (无夜盘品种当日完全空/夜盘品种白盘段缺失)
        context.stra_log_text(
            f"{self.name()} signal source: {self._sig_contract} "
            f"(自身真实序列, 无复权; 执行仍为 {self._inst.code})")

    def _full_contract(self, yymm: str) -> str:
        ex, prod = split_code(self._inst.code)[:2]
        return f"{ex}.{prod}.{yymm}"

    def _full_contract_from_contract(self, contract: str) -> str:
        """hots.json 规则 'to' 字段 -> WT 原生合约码。

        'to' 两种格式: SHFE 系 'rb2701' (品种+4位) -> SHFE.rb.2701;
        CZCE 'TA701' (品种+3位, contracts.json 键口径) -> CZCE.TA701
        (2026-11-11 激活包实锤: 旧实现输出 CZCE.TA701.A701 双重前缀,
        框架查无此合约, 信号副K静默无数据)。"""
        ex = self._inst.code.split(".")[0]
        if "." in contract:
            return contract
        if contract[-4:].isdigit():
            head = contract[:-4]
            return f"{ex}.{head}.{contract[-4:]}"
        return f"{ex}.{contract}"

    def _load_roll_calendar(self, common: str):
        """roll_calendar.json -> [(start, end, yymm)] 或 None。"""
        try:
            with open(os.path.join(common, "roll_calendar.json"),
                      encoding="utf-8") as f:
                raw = json.load(f) or {}
            entry = (raw.get("codes") or {}).get(self._inst.code)
            segs = (entry or {}).get("segments") or []
            out = [(int(s[0]), int(s[1]), str(s[2])) for s in segs]
            return out or None
        except (OSError, ValueError, TypeError, IndexError):
            return None

    def _dominant_for(self, date: int):
        """回测逐日主力: 日历段查找 -> 合约全码 或 None。"""
        cal = self._sig_calendar
        if not cal:
            return self._sig_contract
        for s, e, yymm in cal:
            if int(s) <= int(date) <= int(e):
                return self._full_contract(yymm)
        # 窗外 (数据延伸): 用最近一段
        if int(date) < cal[0][0]:
            return self._full_contract(cal[0][2])
        return self._full_contract(cal[-1][2])

    def _check_sig_roll(self, context, dom: str):
        """换月事件检测/告警: 评估日主力变化 → 日志+告警 (实盘配套
        每日 20:40 重启切换; bt 逐日切换无告警, 只记录首次)。"""
        if dom is None or dom == self._sig_cur_dom:
            return
        prev = self._sig_cur_dom
        self._sig_cur_dom = dom
        if prev is None:
            return
        get_date = getattr(context, "stra_get_date", None)
        day = -1
        if get_date is not None:
            try:
                day = int(get_date())
            except (TypeError, ValueError):
                pass
        if day == self._sig_roll_alert_day:
            return
        self._sig_roll_alert_day = day
        txt = (f"signal roll: {prev} -> {dom}"
               + ("" if self._is_backtest else
                  "; 实盘信号保持旧合约至 20:40 重启切换"))
        context.stra_log_text(f"{self.name()} {txt}", level=2)
        self._alert("signal_source_roll", txt)

    def _sig_bars(self, context, full_code):
        """合约信号序列读取 (带缓存) -> (dates, h, l, c) 或 None。

        回测: 直读 storage/csv/contracts/<code>.json 抓取缓存 —— 绕开
        C++ 副K (逐合约 sub 注册触发 CtaMocker 段错误风险, 2026-11-10
        实证) 且免去 24 个副K订阅开销; 实盘: 单主力合约走框架副K。"""
        cached = self._sig_bars_cache.get(full_code)
        if cached is not None:
            return cached if cached[0] is not None else None
        if self._is_backtest:
            rows = self._load_contract_cache(full_code)
            if not rows:
                self._sig_bars_cache[full_code] = (None,)
                return None
            out = ([int(r["date"]) for r in rows],
                   [float(r["high"]) for r in rows],
                   [float(r["low"]) for r in rows],
                   [float(r["close"]) for r in rows])
            self._sig_bars_cache[full_code] = out
            return out
        sbars = context.stra_get_bars(full_code, self._inst.period,
                                      self._base.bar_cnt, isMain=False)
        if sbars is None or len(sbars) == 0:
            self._sig_bars_cache[full_code] = (None,)
            return None
        sd = self._nd_field(sbars, "date")
        if sd is None:
            self._sig_bars_cache[full_code] = (None,)
            return None
        out = ([int(d) for d in sd],
               list(sbars.highs), list(sbars.lows), list(sbars.closes))
        self._sig_bars_cache[full_code] = out
        return out

    def _load_contract_cache(self, full_code: str):
        """storage/csv/contracts/<code>.json -> [{date,high,low,close}]

        缓存来自 scripts/build_contract_signals.py 的抓取 (幂等, 缺文件
        = 该合约无数据, 上层窗口截断处理)。"""
        common = find_common(_GROUP_DIR)
        storage = os.path.abspath(os.path.join(common, "..", "storage"))
        path = os.path.join(storage, "csv", "contracts",
                            f"{full_code}.json")
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _signal_arrays(self, context, bars, highs, lows, closes,
                       extra_date=None):
        """contract 模式: 把指标窗口替换为**评估日主力合约**自身序列
        (align_window 尾部对齐, 上市前头部截断, 缺失日内沿前收)。

        返回 (highs, lows, closes) np 数组; None = 窗口不足, 调用方
        跳过本次评估 (锚点不动)。hot/未解析 → 原样返回 (零漂移)。
        """
        if self._sig_source() != "contract":
            return highs, lows, closes
        hd = self._nd_field(bars, "date")
        if hd is None or len(highs) not in (len(hd), len(hd) + 1):
            return None
        dates = [int(d) for d in hd]
        if extra_date is not None and len(highs) == len(dates) + 1:
            dates.append(int(extra_date))
        eval_date = dates[-1]
        dom = self._dominant_for(eval_date) if self._sig_calendar \
            else self._sig_contract
        if not dom:
            return highs, lows, closes      # 未解析 → hot 退化
        self._check_sig_roll(context, dom)
        sb = self._sig_bars(context, dom)
        if sb is None:
            return None
        sd, sh, sl, sc = sb
        aligned = align_window(
            dates, list(highs), list(lows), list(closes),
            sd, sh, sl, sc, min_bars=self._need_bars())
        if aligned is None:
            if self._is_backtest:
                context.stra_log_text(
                    f"{self.name()} signal window insufficient: "
                    f"{dom} bars < {self._need_bars()}", level=2)
                return None
            # live 新主力上市初期自身历史 < need_bars (换月盲区,
            # 2026-11-12 激活包实锤 rb.2701 bars<57): 以 HOT 为底 +
            # 主力重叠段覆盖 (align_window_hotfill; 与 bt 逐日主力
            # 同构)。used=0 (主力完全无覆盖) → None 不得静默退 HOT。
            aligned = align_window_hotfill(
                dates, list(highs), list(lows), list(closes),
                sd, sh, sl, sc, min_bars=self._need_bars())
            if aligned is None:
                context.stra_log_text(
                    f"{self.name()} signal window insufficient: "
                    f"{dom} bars < {self._need_bars()} (hotfill "
                    f"unavailable)", level=2)
                return None
        return (np.asarray(aligned[0], dtype=float),
                np.asarray(aligned[1], dtype=float),
                np.asarray(aligned[2], dtype=float))


    def _post_calculate(self, context: CtaContext, closes, n: float,
                        chan: dict, mark: bool = True):
        """对账 → 图表 → 持久化 (daily/intrabar 共用收尾)

        图表段仅 schedule 时段可用 (C++ set_index_value 非 schedule 打
        error 丢弃, CtaStraBaseCtx:2036) — mark 再叠 _in_schedule 闸,
        on_tick/session_end 路径自动跳过。"""
        code = self._inst.code
        self._process_exit_orders(context)
        if self._exit_orders:
            self._persist(context)
            return
        # 日损闸先于对账/持久化: 触发后本 bar 的目标仓已归零
        self._update_daily_guard(context)
        self._reconcile(context, closes)
        self._reanchor_actual_fill(context)
        self._sync_risk_book(n)
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
        snap = {"v": 5,
                "subs": {s["name"]: s["engine"].snapshot()
                         for s in self._subs},
                "profiles": snapshot_meta(self._profiles),
                "d1_date": self._d1_date, "m5_key": self._m5_key,
                "h1_key": self._h1_key,
                "h1_m5_key": self._h1_m5_key,
                "exit_orders": self._exit_order_snapshot(),
                "deferred": self._deferred_decisions,
                "intents": self._position_intent_snapshot(),
                "outbox_seq": self._outbox_seq,
                "recon_miss": self._recon_miss}
        context.user_save_data(f"{self.name()}_engine", json.dumps(snap))

    def _dump_fund_live(self, context):
        """→ execution.Telemetry.dump_fund_live (A1-2 迁出, 薄转发)"""
        return self._telemetry.dump_fund_live(context)

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
        # Phase 1: contract 信号源替换指标窗口 (与 _on_calc_daily 同
        # 口径; None=窗口不足跳过本次; intrabar 路径 2026-11-12 激活
        # 包首活时缺失, 信号会静默退回 HOT 口径)
        sig = self._signal_arrays(context, bars, highs, lows, closes)
        if sig is None:
            return
        highs, lows, closes = sig
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

        # 与 _eval_core 同序: 先精确回写末层, 再全层平移
        self._reanchor_actual_fill(context)
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
            _opens = getattr(bars, "opens", None)
            last_o = float(_opens[-1]) if _opens is not None else None
            if self._is_dual:
                dec_map = {s["name"]: s["engine"].on_bar(
                    last_h, last_l, last_c, prev_c, n, chan[s["name"]],
                    open=last_o)
                    for s in self._subs}
                self._execute_dual(context, dec_map, n, fctx)
            else:
                decisions = self._engine.on_bar(
                    last_h, last_l, last_c, prev_c, n,
                    chan[self._subs[0]["name"]], open=last_o)
                for dec in decisions:
                    self._execute(context, dec, n, fctx)

        # K线旁路日志 (与决策去重独立, 幂等)
        self._journal_signal_bar(bars)

        self._post_calculate(context, closes, n, chan)

    def _tick_stop_scan(self, context, tick):
        """close 模式盘中 2N 止损触碰 (tick 级, 2026-11-14)。

        close 的收盘确认决策本身无盘中触发; `intrabar_stop: true`
        品种由 m5 主K扫描承载盘中保护, 但 **CZCE 三品种的 d1 副K
        在实盘 reader 读取失败** (2026-11-12/13 实锤: session_end
        与盘中评估双双静默, 因 intrabar 模式 d1=副K) → 回退
        daily 模式后盘中保护由本扫描承载。命中挂出场批次
        (_exit_orders 非空后续 tick 直接跳过, 不重复触发), 成交
        确认走 FillTracker 证据链, 与收盘决策的 exit 完全同路径。
        """
        if self._exit_orders or self._open_intents:
            return
        px = float(getattr(tick, "price", 0.0) or 0.0)
        if px <= 0:
            return
        for sub in self._subs:
            e = sub["engine"]
            if e.direction == 0 or not e.layers:
                continue
            stop = e.stop_price()
            if stop != stop:            # NaN 守卫 (脏状态)
                continue
            hit = (e.direction > 0 and px <= stop) or \
                  (e.direction < 0 and px >= stop)
            if hit:
                context.stra_log_text(
                    f"{self.name()}[{sub['name']}] tick stop hit: "
                    f"px={px:.2f} stop={stop:.2f} dir={e.direction}",
                    level=2)
                self._alert("tick_stop",
                            f"{self._inst.code}[{sub['name']}] "
                            f"tick stop {px:.2f} vs {stop:.2f}")
                dec = {"decision": "exit", "reason": "stop_tick",
                       "price": stop, "tag": "StopTick"}
                self._execute(context, dec, self._last_n)
                return                  # 单批次, 成交确认前不再扫描

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
        if not self._is_backtest and self._is_dual:
            # 多 profile 实盘盘中止损与常规出场同一语义：目标仓下降不
            # 等于成交完成，必须登记批次并等待真实平仓证据。
            dec_map = {}
            for sub, sp, _i in hits:
                dec_map.setdefault(sub["name"], []).append({
                    "decision": "exit", "reason": "stop", "price": sp,
                    "tag": "ExitI"})
            self._execute_dual(context, dec_map, n)
        else:
            for sub, sp, i in hits:
                tstr = f"{int(times[i])}" if times is not None else "?"
                old = self._sub_lots(sub) * (1 if sub["engine"].direction > 0
                                             else -1)
                if not self._is_backtest:
                    self._execute(context, {
                        "decision": "exit", "reason": "stop", "price": sp,
                        "tag": "ExitI"}, n)
                    continue
                sub["engine"].confirm_exit(sp)
                self._safe_mark(context, sp, "exit", self.name())
                context.stra_log_text(
                    f"{self.name()}[{sub['name']}] intrabar exit "
                    f"{old}@{sp:.2f} (stop touched @m5 {tstr})", level=1)
        if self._exit_orders:
            self._persist(context)
            return
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
        dec_map = {}
        for sub in self._subs:
            decs = sub["engine"].check_bar(o, h, l, c)
            if os.environ.get("WT_TRIG_DEBUG") == "1":
                context.stra_log_text(
                    f"[trigdbg] {sub['name']} d={context.stra_get_date()} "
                    f"OHLC={o:.0f}/{h:.0f}/{l:.0f}/{c:.0f} "
                    f"armed={sub['engine'].armed} "
                    f"decs={[d.get('decision') for d in decs]}")
            if decs:
                dec_map[sub["name"]] = decs
        self._exec_trigger_batch(context, dec_map)

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
        """实盘 tick 驱动触发 (trigger 模式)。

        心跳/断流基准在模式闸**之前**更新: tick 静默检测是模式无关
        的运维防线 (close 模式同样依赖数据链健康, 2026-11-12 激活包
        重启后 cu/al/sc tick_silent 误报实锤 —— 旧实现 close 首行
        return 永不刷新 tick 时间戳)。周期快照/心跳写盘保持轻量。"""
        self._last_tick_ts = time.time()    # Phase 0: 心跳断流检测基准
        self._maybe_periodic_persist(context)   # 周期快照 (300s 节流)
        self._write_heartbeat(context)          # 心跳 (5s 节流)
        if stdCode != self._inst.code:
            return
        if self._mode.entry_mode == "close":
            # close 模式 tick: 心跳基准 (上方) + 意图/出场证据推进
            # (与 trigger 同前段; 收盘确认批次成交后需及时确认引擎
            # 状态, 否则要等下一 session_end) + 盘中 2N 止损触碰
            # (intrabar_stop=false 品种的盘中保护; intrabar 品种由
            # m5 主K扫描, 不经此路径)
            self._process_open_intents(context)
            self._process_exit_orders(context)
            if self._exit_orders or self._open_intents:
                self._persist(context)
                return
            self._tick_stop_scan(context, newTick)
            return
        self._process_open_intents(context)
        self._process_exit_orders(context)
        if self._exit_orders or self._open_intents:
            self._persist(context)
            return
        # 新交易日自愈评估: session_end 未触发(实盘收盘后无 tick,
        # ticker 兜底要求行情时间推进过收盘点, 白盘 15:00 后无行情
        # → 1515 永不触发) 或盘后启动时, 首个 tick 检测 his 末根
        # 未评估则立即评估武装 (每日至多检查一次)
        tdate = context.stra_get_tdate()
        if tdate and self._catchup_date != int(tdate):
            # 闸门的消费权在 _catchup_eval 内部: 只有"结论性"出口
            # (已评估到最新/评估成功) 才落 _catchup_date; 数据未就绪
            # 等暂时性失败不消费 → 下一 tick 自动重试 (2026-09-21 实测:
            # 首 tick 09:00:00 读 bars 未就绪静默失败, 闸被消费后当日
            # 不再自愈, 商品通道缺一根 bar)
            self._catchup_eval(context, int(tdate))
        # 日损闸: 先于触发判定; 触发后 _size_and_cap 会继续拦截新开/加仓
        self._update_daily_guard(context)
        px = context.stra_get_price(stdCode)
        if px is None or px != px or px <= 0:
            return
        self._detect_limit_lock(context, stdCode, float(px))
        self._periodic_truth_check(context)   # 定时真值对账 (独立于日评估)
        self._reanchor_actual_fill(context)   # 分批窗口闭合即回写均价
        # P2-11 补充: fund_live.csv 只挂日评估路径时盘中无行 (2026-09-21
        # 实测: 全天仅 IF catchup 写了一行); tick 路径节流 60s 增补
        self._dump_fund_live(context)
        dec_map = {}
        for sub in self._subs:
            decs = sub["engine"].on_price(float(px))
            if decs:
                dec_map[sub["name"]] = decs
        if dec_map:
            # 同一 tick 全 profile 先收集，再一次净额执行，避免中间目标单。
            self._exec_trigger_batch(context, dec_map)
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

    def _exec_trigger_batch(self, context, dec_map: dict):
        """同一 tick/bar 的全 profile 决策，一次净额执行。"""
        if not dec_map:
            return
        if not np.isfinite(self._last_n):
            context.stra_log_text(
                f"{self.name()} trigger decisions dropped: N unavailable",
                level=2)
            for sub in self._subs:
                if sub["name"] not in dec_map:
                    continue
                dropped = sub["engine"].clear_pending()
                if dropped is not None:
                    context.stra_log_text(
                        f"{self.name()}[{sub['name']}] clear pending: "
                        f"{dropped}", level=2)
            return
        for decs in dec_map.values():
            for dec in decs:
                dec["prefiltered"] = True
        if self._is_dual:
            self._execute_dual(context, dec_map, self._last_n)
        else:
            for sub in self._subs:
                if sub["name"] in dec_map:
                    self._exec_trigger_decs(context, sub, dec_map[sub["name"]])

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
            # 2026-10-20: 决策丢弃路径必须同步清 pending。引擎在
            # on_price/check_bar 里已置 _pending, 若此处只 return,
            # pending 会经快照持久化/恢复永久挂起, 吞掉后续全部触发
            # (含止损) — 与 2026-09-04 孤儿 pending 事故同型。
            dropped = sub["engine"].clear_pending()
            if dropped is not None:
                context.stra_log_text(
                    f"{self.name()}[{sub['name']}] clear pending on "
                    f"N-missing drop: {dropped}", level=2)
            return
        for dec in decs:
            dec["prefiltered"] = True      # 武装时已裁决, 免重复过滤
        if self._is_dual:
            self._execute_dual(context, {sub["name"]: decs}, self._last_n)
        else:
            for dec in decs:
                self._execute(context, dec, self._last_n)

    def _on_arm(self, context, engine, fctx):
        """→ execution.ExecutionRouter.on_arm (A1-1 迁出, 薄转发)

        Phase 0 (2026-11-09) 前置门: 行情价不可用 (stra_get_price 为 0/
        None/NaN —— 2026-09-28 商品通道断流的双实证特征之一) 时不允许
        留下新的入场武装位: 数据恢复后 armed 位已陈旧, 会以过期通道位
        触发假突破入场。两方向解除武装 + 每日一次告警; 出场/止损位不
        动 (持仓管理优先)。回测恒可用, 不影响基线。
        """
        if not self._is_backtest:
            px = context.stra_get_price(self._inst.code)
            if px is None or px != px or float(px) <= 0:
                engine.disarm_entry(1)
                engine.disarm_entry(-1)
                get_date = getattr(context, "stra_get_date", None)
                day = None
                if get_date is not None:
                    try:
                        day = int(get_date())
                    except (TypeError, ValueError):
                        day = None
                if day != self._no_price_arm_date:
                    self._no_price_arm_date = day
                    txt = (f"{self._inst.code} price unavailable "
                           f"(stra_get_price={px!r}) — entries disarmed, "
                           f"no new arms until feed recovers")
                    context.stra_log_text(
                        f"{self.name()} {txt}", level=3)
                    self._alert("no_price_arm_block", txt)
                return
        return self._router.on_arm(context, engine, fctx)

    def _persist(self, context: CtaContext):
        """轻量持久化 (触发事件后): 仅引擎快照+去重键"""
        snap = {"v": 5,
                "subs": {s["name"]: s["engine"].snapshot()
                         for s in self._subs},
                "profiles": snapshot_meta(self._profiles),
                "d1_date": self._d1_date, "m5_key": self._m5_key,
                "h1_key": self._h1_key,
                "h1_m5_key": self._h1_m5_key,
                "exit_orders": self._exit_order_snapshot(),
                "deferred": self._deferred_decisions,
                "intents": self._position_intent_snapshot(),
                "outbox_seq": self._outbox_seq,
                "recon_miss": self._recon_miss}
        context.user_save_data(f"{self.name()}_engine", json.dumps(snap))
        # 快照重新落盘成功 = 状态恢复真实性 → 解除 DEGRADED
        self._state_degraded = False
        self._backup_snapshot(json.dumps(snap))

    # ------------------------------------------------------------------
    # Phase 0 (2026-11-09): 心跳 / 周期快照 / 快照备份
    # ------------------------------------------------------------------
    _HEARTBEAT_INTERVAL = 5.0       # 心跳写入节流 (秒)
    _PERIODIC_PERSIST_INTERVAL = 300.0   # 周期快照节流 (秒)

    def _backup_snapshot(self, payload: str):
        """快照 3 深轮换备份 (generated/snapshots/), 内容变化才写。

        原实现只有框架 userdata 单文件, 崩溃/写坏即无回退; 备份让 doctor
        与人工恢复有最近 3 个时间点可用。旁路失败静默跳过。
        """
        if self._is_backtest:
            return
        try:
            h = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            if h == self._snap_hash:
                return
            self._snap_hash = h
            d = os.path.join(_GROUP_DIR, "generated", "snapshots")
            os.makedirs(d, exist_ok=True)
            base = os.path.join(d, f"{self.name()}.json")
            # 轮换: .2→.3, .1→.2, current→.1
            for i in (2, 1):
                src = f"{base}.{i}"
                if os.path.exists(src):
                    os.replace(src, f"{base}.{i + 1}")
            if os.path.exists(base):
                os.replace(base, f"{base}.1")
            tmp = f"{base}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, base)
        except OSError:
            pass

    def _write_heartbeat(self, context, force: bool = False):
        """实盘心跳落盘; 回测跳过, 写失败静默 (旁路不阻断交易)。

        字段覆盖 watchdog 三类检测: 进程存活 (文件 mtime)、tick 断流
        (last_tick_ts)、状态概览 (方向/净仓/锚点/未决窗口/DEGRADED)。
        """
        if self._is_backtest:
            return
        now = time.time()
        if not force and now - self._heartbeat_ts < self._HEARTBEAT_INTERVAL:
            return
        self._heartbeat_ts = now
        try:
            code = self._inst.code
            px = context.stra_get_price(code)
            hb = {
                "name": self.name(), "code": code, "ts": now,
                "pid": os.getpid(),
                "last_tick_ts": self._last_tick_ts,
                "tick_age": (now - self._last_tick_ts)
                if self._last_tick_ts else None,
                "price": float(px) if (px and px == px and px > 0) else 0.0,
                "direction": self._engine.direction,
                "net_lots": self._combined_target(),
                "d1_date": self._d1_date, "m5_key": self._m5_key,
                "exit_orders": len(self._exit_orders),
                "open_intents": len(self._open_intents),
                "fill_pending": len(self._fill_pending),
                "degraded": self._state_degraded,
            }
            d = os.path.join(_GROUP_DIR, "generated", "heartbeat")
            os.makedirs(d, exist_ok=True)
            tmp = os.path.join(d, f".{self.name()}.{os.getpid()}.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(hb, f)
            os.replace(tmp, os.path.join(d, f"{self.name()}.json"))
        except Exception:
            pass

    def _maybe_periodic_persist(self, context):
        """周期快照 (300s 节流): 事件驱动 _persist 之外的崩溃窗口兜底。

        原有 _persist 只挂在决策/出场/意图事件上, 长时间无事件时崩溃会
        丢失最近状态 (armed 位/锚点/账本同步点); 周期落盘把丢失窗收敛到
        ≤5min。bt 同样执行 (user_save_data 无副作用差异, 黄金基线覆盖)。
        """
        now = time.time()
        if now - self._periodic_persist_ts < self._PERIODIC_PERSIST_INTERVAL:
            return
        self._periodic_persist_ts = now
        try:
            self._persist(context)
        except Exception:
            pass

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
        self._write_heartbeat(context, force=True)   # Phase 0: 会话末强制心跳
        self._process_open_intents(context)
        self._process_exit_orders(context)
        if self._exit_orders or self._open_intents:
            self._persist(context)
            return
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
        # h1: 尾部桶在会话末无下根 m5 触发闭合 → flush 强制闭合评估
        # (trigger 武装与 close 决策都依赖; d1 合成路径对 h1 不适用)
        flush = self._mode.session_flush
        if flush == "h1_flush":
            self._h1_flush_eval(context, curTDate)
        # trigger 模式: 实盘日线永不闭合(F3) → session end 用当日 m5
        # 聚合合成日K, 做收盘日线评估并武装次日触发位
        elif flush == "daily_synth":
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
        """实盘日线收盘评估 (trigger 武装 / close 决策): m5 聚合当日
        日K → 指标 → on_bar (引擎按 entry_mode 各自产出武装或决策)"""
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
            # 2026-11-14 诊断: 曾静默 return 无痕迹 (CZCE d1 副K疑似
            # 读取失败, session_end 整天无声); 升级 warning 钉死根因
            context.stra_log_text(
                f"{self.name()} session eval aborted: d1 bars="
                f"{'None' if bars is None else len(bars)} < {need - 1}",
                level=2)
            return
        last_date = self._bar_last_date(bars)
        if last_date is None:
            context.stra_log_text(
                f"{self.name()} session eval aborted: d1 bars 无日期字段",
                level=2)
            return
        # 当日 m5 聚合 (his 无当日, 见 DATA_OPS F3)。
        # 合成原料**统一读 HOT 的 m5** (主K级成熟订阅): 当日 HOT 由
        # hots.json 主力映射 = 当日主力合约自身 m5, contract 口径的
        # "真实价"语义不变 (hots 每日 20:35 更新 + 20:40 重启, 换月日
        # 当日 HOT 即新主力)。2026-11-12 实锤: 直接读主力合约 m5
        # (新订阅副K) 命中 C++ reader 拼装缓存冻结 —— SHFE 夜盘品种
        # 缓存停在重启时刻只有夜盘段 (白盘 09:00-15:00 缺失, 当日
        # 合成残缺), 无夜盘的 CZCE/IF 当日完全空 (session eval 跳过)。
        synth = None
        mcode = code
        mbars = context.stra_get_bars(mcode, p.intrabar_period,
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
            # 无当日 m5 (数据链路异常): 绝不能用 his 末根冒充当日评估。
            # 否则会把锚点推进到 curTDate，次日真实日线补齐后 catch-up
            # 误判"已是最新"并永久跳过当日评估。保持锚点不动，等数据
            # 恢复后由 catch-up 用真实 his 日期补评估。
            if int(last_date) < int(curTDate):
                txt = (f"session eval skipped: 当日 m5 缺失, his last="
                       f"{last_date} < tdate={curTDate}; 等待数据恢复后 "
                       "catch-up 补评估")
                context.stra_log_text(f"{self.name()} {txt}", level=2)
                self._alert("session_eval_missing_m5", txt)
                return
            # his 已含当日 bar 的边界（如盘后启动/数据链已补齐）:
            # 用实际数据日期做评估锚。
            o, h, l, c = float(highs[-1]), float(highs[-1]), \
                float(lows[-1]), prev_close
        # with_oi=True (2026-10-20): his bars 本身带 volume/OI, 之前传
        # False 会让 LiquidityFloor 在 live 主评估路径拿不到数据而按
        # on_warmup 默认放行 (与 _on_calc_daily/_catchup_eval 的
        # with_oi=True 不一致)。合成 bar 没有成交量, 过滤器使用最近
        # 完整 bar 的成交量是刻意口径 (优于 fail-open)。
        # Phase 1: contract 模式指标窗口替换 (extra_date=合成 bar 日)
        sig = self._signal_arrays(
            context, bars, highs, lows, closes,
            extra_date=(int(curTDate) if synth is not None else None))
        if sig is None:
            return
        highs, lows, closes = sig
        atr, n, chan = self._eval_core(context, bars, highs, lows, closes,
                                       h, l, c, prev_close, with_oi=True,
                                       synth=synth)
        if n is None:
            return
        # 评估锚必须是"实际参与评估的数据日期"，不是目标交易日。
        self._d1_date = int(curTDate) if synth is not None \
            else int(last_date)
        # 当日合成日K落 journal (崩溃窗口兜底)
        if synth is not None and self._use_journal:
            self._journal_writer().append(
                code, "d", {"date": int(curTDate), "time": 1500,
                            "open": o, "high": h, "low": l, "close": c})
        self._post_calculate(context, closes, n, chan)

    def _catchup_eval(self, context: CtaContext, tdate: int = None):
        """新交易日自愈评估 (仅实盘 trigger 日线): 用 his 数据
        (不含当日) 补做日线评估并武装。与 session_end 的 synth=None
        兜底分支同口径; bt 不启用 (bt 日K闭合, on_bar 路径完整)。

        tdate: 当前交易日 (由 on_tick 传入)。**闸门消费权在本方法**:
        只有结论性出口 (已评估到最新 / 评估成功 / 模式性禁用) 才落
        _catchup_date=tdate; 数据未就绪的暂时性失败不消费 → 下一
        tick 重试。2026-09-21 实测: 11 个商品策略在 09:00:00 首 tick
        读 bars 未就绪静默失败, 旧实现闸被消费 → 当日不再自愈,
        通道缺一根 bar (session_end 15:15 正常触发才未酿成事故)。
        """
        if not self._mode.wants_catchup:
            # 模式性禁用 → 结论性出口消费闸门 (close/intrabar/h1/bt;
            # h1 评估由 m5 闭合驱动无需日级 catchup; 防 09:00 首 tick
            # 竞态重试, 9/21 事故修复的同一闸门语义)
            if tdate is not None:
                self._catchup_date = tdate
            return
        code = self._inst.code
        p = self._base
        bars = context.stra_get_bars(code, self._inst.period, p.bar_cnt)
        need = self._need_bars()
        # 本路径只用 his (不追加合成bar), 故要求完整 need 根;
        # 未就绪 → 不消费闸 (下一 tick 重试)
        if bars is None or len(bars) < need:
            return
        last_date = self._bar_last_date(bars)
        if last_date is None:
            return
        if self._d1_date is not None and int(self._d1_date) >= int(last_date):
            # his 末根已评估过 → 结论性, 消费闸 (避免每 tick 空转)
            if tdate is not None:
                self._catchup_date = tdate
            return
        self._drop_stale_pending(context, f"catchup {last_date}")
        highs = np.asarray(bars.highs, dtype=float)
        lows = np.asarray(bars.lows, dtype=float)
        closes = np.asarray(bars.closes, dtype=float)
        # Phase 1: contract 模式指标窗口替换 (None=窗口不足, 不消费闸)
        sig = self._signal_arrays(context, bars, highs, lows, closes)
        if sig is None:
            return
        highs, lows, closes = sig
        atr, n, chan = self._eval_core(
            context, bars, highs, lows, closes,
            float(highs[-1]), float(lows[-1]), float(closes[-1]),
            float(closes[-2]), with_oi=True)
        if n is None:
            return
        self._d1_date = int(last_date)
        if tdate is not None:
            self._catchup_date = tdate          # 评估成功 → 消费闸
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
    def _update_daily_guard(self, context: CtaContext):
        """策略层日损闸 (2026-10-20)。

        与 C++ SimpleRiskMon 的分工: riskmon 是进程/组合级且当前
        risk_scale 可能被设成 1.0 (no-op); 本闸按**单策略**权益做
        "当日首笔 → 当前"回撤:
          - daily_loss_stop_frac:    停止新开/加仓 (只减不加)
          - daily_loss_flatten_frac: 全平 (更强, 应 >= stop)
        0 = 关闭 (默认; 开启前需先定单策略分配资金)。
        """
        cfg = self._risk.cfg
        stop = float(getattr(cfg, "daily_loss_stop_frac", 0.0) or 0.0)
        flat = float(getattr(cfg, "daily_loss_flatten_frac", 0.0) or 0.0)
        if stop <= 0 and flat <= 0:
            return
        get_date = getattr(context, "stra_get_date", None)
        if get_date is None:
            return
        try:
            day = int(get_date())
        except (TypeError, ValueError):
            return
        eq = self._risk.equity(context, sid=self.name())
        if day != self._day_guard_date:
            self._day_guard_date = day
            self._day_start_equity = eq
            self._daily_blocked = False
            self._daily_flattened = False
            return
        start = self._day_start_equity
        if not start or start <= 0:
            self._day_start_equity = eq
            return
        dd = eq / start - 1.0
        if stop > 0 and dd <= -stop and not self._daily_blocked:
            self._daily_blocked = True
            txt = (f"{self._inst.code} daily loss {dd:.2%} <= "
                   f"-{stop:.2%} → 停止新开/加仓 (只减不加)")
            context.stra_log_text(f"{self.name()} {txt}", level=3)
            self._alert("daily_loss_stop", txt)
        if flat > 0 and dd <= -flat and not self._daily_flattened:
            self._daily_flattened = True
            # 全平后必须同时封锁再开仓 (即使只配了 flatten、没配 stop)
            self._daily_blocked = True
            self._flatten_all(context, f"daily_loss {dd:.2%}")

    def _flatten_all(self, context: CtaContext, reason: str):
        """全平所有子系统 (日损闸); 只走引擎确认+目标仓 0, 不新开。"""
        code = self._inst.code
        try:
            pos = float(context.stra_get_position(code))
        except (AttributeError, TypeError, ValueError):
            return
        if int(round(pos)) == 0:
            return
        px = None
        get_px = getattr(context, "stra_get_price", None)
        if get_px is not None:
            try:
                px = float(get_px(code))
            except (TypeError, ValueError):
                px = None
        if px is None or px != px or px <= 0:
            px = 0.0
        if not self._is_backtest:
            # 单/多 profile 实盘统一走异步出场：目标仓 0 不代表券商已平，
            # 成交证据到达前保留引擎与风险账。
            if not self._is_dual:
                e = self._engine
                fill = px if px > 0 else (
                    e.layers[-1][1] if e.layers else 0.0)
                self._execute(context, {
                    "decision": "exit", "reason": reason, "price": fill,
                    "tag": "DailyStop"}, self._last_n)
            else:
                dec_map = {}
                for s in self._subs:
                    e = s["engine"]
                    if e.direction == 0:
                        continue
                    fill = px if px > 0 else (
                        e.layers[-1][1] if e.layers else 0.0)
                    dec_map.setdefault(s["name"], []).append({
                        "decision": "exit", "reason": reason,
                        "price": fill, "tag": "DailyStop"})
                self._execute_dual(context, dec_map, self._last_n)
            txt = (f"{code} {reason} → flatten pending "
                   f"{int(round(pos))} lots (等待真实平仓确认)")
            context.stra_log_text(f"{self.name()} {txt}", level=3)
            self._alert("daily_loss_flatten_pending", txt)
            return
        context.stra_set_position(code, 0, "DailyStop")
        for s in self._subs:
            e = s["engine"]
            if e.direction == 0:
                continue
            fill = px if px > 0 else (e.layers[-1][1] if e.layers else 0.0)
            e.confirm_exit(fill)
        self._risk.on_closed(code)
        txt = f"{code} {reason} → flatten {int(round(pos))} lots"
        context.stra_log_text(f"{self.name()} {txt}", level=3)
        self._alert("daily_loss_flatten", txt)

    def _ghost_blocked(self, context) -> bool:
        """ghost trim 当日禁止再触发 (enter/add 共用, 2026-10-09 循环修复)。"""
        if not self._ghost_trim_date:
            return False
        get_date = getattr(context, "stra_get_date", None)
        if get_date is None:
            return False               # 测试桩无日期 → 不阻断
        try:
            return int(get_date()) == self._ghost_trim_date
        except Exception:
            return False

    def _size_and_cap(self, context, kind: str, n: float, price: float, tag: str):
        """→ execution.ExecutionRouter.size_and_cap (A1-1 迁出, 薄转发)"""
        return self._router.size_and_cap(context, kind, n, price, tag)

    def _execute(self, context, dec: dict, n: float, fctx: FilterContext = None):
        """→ execution.ExecutionRouter.execute (A1-1 迁出, 薄转发)"""
        return self._router.execute(context, dec, n, fctx)

    def _run_filter_chain(self, context, sig_dir: int, dec: dict, fctx: FilterContext = None):
        """→ execution.ExecutionRouter.run_filter_chain (A1-1 迁出, 薄转发)"""
        return self._router.run_filter_chain(context, sig_dir, dec, fctx)

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
    def _reconcile(self, context, closes):
        """→ execution.TruthMonitor.reconcile (A1-4 迁出, 薄转发)"""
        return self._truthmon.reconcile(context, closes)

    # ------------------------------------------------------------------
    # S1+S2 双系统批量执行 (净仓语义: 框架持仓 = Σ子系统目标)
    # ------------------------------------------------------------------
    def _execute_dual(self, context, dec_map: dict, n: float, fctx: FilterContext = None):
        """→ execution.ExecutionRouter.execute_dual (A1-1 迁出, 薄转发)"""
        return self._router.execute_dual(context, dec_map, n, fctx)

    def _replay_deferred_decisions(self, context):
        """出场批次真实成交后重放被延后的 enter/add。

        重放前先从队列取出，避免重放路径再次进入本函数造成递归；当前队列
        只保存 enter/add，不会生成新的出场批次。风控/过滤会按重放时点重新
        裁决，风控否决会正常撤武装并丢弃该决策。
        """
        dec_map = self._deferred_decisions
        if not dec_map:
            return
        self._deferred_decisions = {}
        # 延迟决策只在原自然日内有效。出场 UNKNOWN 跨日后，旧突破价不再
        # 具有执行意义；清 pending 防挂起，禁止跨日补旧单。
        get_day = getattr(context, "stra_get_date", None)
        cur_day = None
        if get_day is not None:
            try:
                cur_day = int(get_day())
            except (TypeError, ValueError):
                cur_day = None
        if cur_day is not None:
            fresh = {}
            for name, decs in dec_map.items():
                same_day = [d for d in decs
                            if d.get("_defer_date", cur_day) == cur_day]
                stale = [d for d in decs
                         if d.get("_defer_date", cur_day) != cur_day]
                if same_day:
                    fresh[name] = same_day
                for _d in stale:
                    sub = self._sub(name)
                    dropped = sub["engine"].clear_pending() \
                        if sub is not None else None
                    txt = (f"deferred {name} decision expired on day "
                           f"change ({_d.get('_defer_date')} -> {cur_day}); "
                           f"pending={dropped}")
                    context.stra_log_text(f"{self.name()} {txt}", level=2)
                    self._alert("deferred_expired", txt)
            dec_map = fresh
            if not dec_map:
                self._persist(context)
                return
        context.stra_log_text(
            f"{self.name()} replay deferred decisions after exit fill: "
            f"{ {k: [d['decision'] for d in v] for k, v in dec_map.items()} }")
        self._execute_dual(context, dec_map, self._last_n)
        self._persist(context)

    def _apply_enter_gates(self, context, sub: dict, d: int, dec: dict, n: float, fctx: FilterContext = None):
        """→ execution.ExecutionRouter.apply_enter_gates (A1-1 迁出, 薄转发)"""
        return self._router.apply_enter_gates(context, sub, d, dec, n, fctx)

    def _apply_add_gates(self, context, sub: dict, dec: dict, n: float):
        """→ execution.ExecutionRouter.apply_add_gates (A1-1 迁出, 薄转发)"""
        return self._router.apply_add_gates(context, sub, dec, n)

    def _sub_share(self, sub: dict) -> float:
        """子系统品种内预算份额。直接读 normalized ProfileSpec 展开值：
        legacy 双系统保留 ceil/floor 比例，speeds 保留显式 share。"""
        return max(0.0, min(1.0, float(sub.get("share", 1.0))))

    def _sync_risk_book(self, n: float):
        """逐 profile 同步风控账本；单位数由 code 级 meta 去重。

        风险值按**当前实际止损位**逐子系统记账 (engine.booked_risk(
        stop=...)): 各 profile 的价格与风险不再被 net 口径压平，第二个
        profile 的 cap_entry 也能精确看到第一个 profile 的批次内预约。
        """
        code = self._inst.code
        any_pos = False
        for s in self._subs:
            e = s["engine"]
            if e.direction == 0 or not e.layers:
                self._risk.on_closed(code, profile=s["name"])
                continue
            any_pos = True
            L = sum(l for l, _ in e.layers)
            avg = sum(l * pr for l, pr in e.layers) / L
            risk = e.booked_risk(self._volscale or 1.0,
                                 self._base.stop_atr_mult,
                                 stop=e.stop_anchor())
            self._risk.sync_position(
                code, e.direction, L, avg,
                n if n and n > 0 else 1.0, self._volscale or 1.0,
                self._base.stop_atr_mult, self._inst.group, risk=risk,
                sid=self.name(), units=float(self._inst.units),
                profile=s["name"])
        if not any_pos:
            self._risk.on_closed(code)

    def _reject_trigger(self, engine):
        """→ execution.ExecutionRouter.reject_trigger (A1-1 迁出, 薄转发)"""
        return self._router.reject_trigger(engine)

    def _veto_log(self, context, key: str, msg: str):
        """→ execution.Telemetry.veto_log (A1-2 迁出, 薄转发)"""
        return self._telemetry.veto_log(context, key, msg)

    def _get_fees(self, context):
        """→ execution.FillTracker.get_fees (A1-3 迁出, 薄转发)

        注意: 必须是普通方法 (带 self)。A1 迁移时遗留了
        ``@staticmethod`` 装饰器, 但签名仍是 ``(self, context)``,
        调用时 context 被绑到 self 形参 → TypeError, 导致
        fund_live.csv 旁路自 2026-10-30 部署起静默失效
        (2026-11-02 生产审计发现, Runner 日志每秒刷
        "fund_live dump failed: missing 1 required positional
        argument: 'context'")。
        """
        return self._fills.get_fees(context)

    def _fill_confirmed(self, context, fp: dict, code: str, target: int):
        """→ execution.FillTracker.fill_confirmed (A1-3 迁出, 薄转发)"""
        return self._fills.fill_confirmed(context, fp, code, target)

    def _truth_stale_now(self):
        """→ execution.TruthMonitor.truth_stale_now (A1-4 迁出, 薄转发)"""
        return self._truthmon.truth_stale_now()

    def _maybe_ghost_trim(self, context, key: str, fp: dict, broker_net: float):
        """→ execution.FillTracker.maybe_ghost_trim (A1-3 迁出, 薄转发)"""
        return self._fills.maybe_ghost_trim(context, key, fp, broker_net)

    def _maybe_exec_recover(self, context, detail: str):
        """→ execution.FillTracker.maybe_exec_recover (A1-3 迁出, 薄转发)"""
        return self._fills.maybe_exec_recover(context, detail)
                         # 状态已随每根 bar/触发事件持久化 + 真值源兜底

    def _reanchor_actual_fill(self, context):
        """→ execution.FillTracker.reanchor_actual_fill (A1-3 迁出, 薄转发)"""
        return self._fills.reanchor_actual_fill(context)

    def _reanchor_one_fill(self, context, key: str, fp: dict, alloc: float = 0.0):
        """→ execution.FillTracker.reanchor_one_fill (A1-3 迁出, 薄转发)"""
        return self._fills.reanchor_one_fill(context, key, fp, alloc)

    def _avgpx_layer_price(self, context, engine, code: str, pos: float):
        """→ execution.FillTracker.avgpx_layer_price (A1-3 迁出, 薄转发)"""
        return self._fills.avgpx_layer_price(context, engine, code, pos)

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
