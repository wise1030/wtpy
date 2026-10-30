"""真值监控层 (A1-4, 2026-10-22): 从 StraTurtle 迁出的交易通道真值/
对账/账户资金闸。

TruthMonitor 持有策略回指 (self.s), 方法体与迁出前逐字等价
(self.X → self.h.X 机械重写; 真值缓存/告警状态仍在策略实例上)。
**本层唯一约定: 所有外部真值必须先过时效闸再用于决策** ——
2026-09-15 陈旧真值污染 / 09-21 真值链死亡两次事故的防线归口。
黄金回测端到端回归覆盖。
"""
import os
import time

import numpy as np

from execution.paths import GROUP_DIR as _GROUP_DIR


class TruthMonitor:
    """rtdata.json/trades.csv 真值源 + 运行时对账 + 共享账户资金闸"""

    # 显式依赖声明 (A1 尾项): 模块全部读写依赖白名单; 访问未声明名
    # 立即抛 AttributeError (隐式耦合 loud-failure, 替代静默降级)
    DEPS = (
        "_TRUTH_CHECK_INTERVAL",
        "_TRUTH_ESCALATE_SEC",
        "_alert",
        "_base",
        "_combined_target",
        "_inst",
        "_is_backtest",
        "_last_n",
        "_recon_miss",
        "_risk",
        "_subs",
        "_trade_journal",
        "_truth",
        "_truth_check_ts",
        "_truth_mismatch_alert",
        "_truth_mismatch_since",
        "_truth_mode",
        "_truth_probed",
        "_truth_retry_ts",
        "_truth_stale_warned",
        "_veto_log",
        "_volscale",
        "name",
    )

    def __init__(self, host):
        self.h = host   # StrategyHost 显式依赖面 (A1 尾项, 见 host.py)     # StraTurtle 回指 (过渡期)

    def truth_source(self):
        """交易通道真值源 (core/LiveTruth); 不可用返回 None。

        框架侧持仓/资金都是信号口径 (见 LiveTruth 模块头), 无法识别
        "执行单元卡死但信号仓已达标"的幽灵仓位; 本真值源来自交易通道
        落盘的 rtdata.json (真实券商持仓+账户资金)。
        文件可能在进程启动后才生成 → 每 600s 允许重探一次。

        回测隔离: `on_init` 检测到 context.is_backtest 后置位
        `_is_backtest`, 此处直接返回 None — 回测的成交确认/对账/ghost
        trim 一律走框架口径, 不读实盘账户。
        """
        if self.h._is_backtest:
            return None
        if self.h._truth is not None:
            return self.h._truth
        now = time.time()
        if self.h._truth_probed and now < self.h._truth_retry_ts:
            return None
        self.h._truth_probed = True
        self.h._truth_retry_ts = now + 600.0
        try:
            from core.LiveTruth import ChannelTruth, resolve_truth_path
            path = resolve_truth_path(
                _GROUP_DIR, enabled=getattr(self.h._base, "truth_json", ""))
            if path:
                self.h._truth = ChannelTruth(
                    path, max_age=float(
                        getattr(self.h._base, "truth_max_age", 1800.0)))
        except Exception:
            self.h._truth = None
        return self.h._truth
    def truth_position(self, context, code: str):
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
        t = self.truth_source()
        if t is None:
            return fw, "framework"
        v = t.net_position(code)
        if v is None:
            return fw, "framework"
        if t.stale():
            if not self.h._truth_stale_warned:
                self.h._truth_stale_warned = True
                age = t.age()
                context.stra_log_text(
                    f"{self.h.name()} truth source stale "
                    f"(age={age if age is None else round(age)}s > "
                    f"{t.max_age:.0f}s) → 回退框架信号口径, 不做"
                    f"真值对账/重建", level=3)
                self.h._alert("truth_stale", f"{t.path} 陈旧, 真值对账已停用")
            return fw, "framework"
        if self.h._truth_mode == "rebuild":
            return float(v), "truth"
        self.h._truth_mismatch_alert(context, code, fw, float(v))
        return fw, "framework"
    def truth_net(self, code: str):
        """账户真值净仓 (新鲜时); 不可用/陈旧 → None"""
        t = self.truth_source()
        if t is None or t.stale():
            return None
        v = t.net_position(code)
        return None if v is None else float(v)
    def truth_stale_now(self) -> bool:
        """真值源已配置且文件存在但已陈旧 (fees 代理不可信的判据)。

        真值未配置 ("off") 或文件缺失 → False (环境无真值, fees 是
        唯一代理, 维持旧行为); 已配置但超时 → True。
        """
        t = self.truth_source()
        return t is not None and t.stale()
    def trade_journal_source(self):
        """逐笔成交读取器 (与真值源同目录 trades.csv); 不可用 → None"""
        if self.h._trade_journal is not None:
            return self.h._trade_journal
        t = self.truth_source()
        if t is None or not t.path:
            return None
        try:
            from core.LiveTruth import TradeJournal
            p = os.path.join(os.path.dirname(t.path), "trades.csv")
            self.h._trade_journal = TradeJournal(p)
        except Exception:
            self.h._trade_journal = None
        return self.h._trade_journal
    def account_available(self):
        """券商口径账户可用资金 (真值源提供); 不可用/文件陈旧 → None。

        共享账户下该值是**扣除其他策略占用后**的真实可用资金, 是
        "资金耗尽被拒单" (2026-09-09) 的防线; 陈旧文件不参与决策。
        """
        t = self.truth_source()
        if t is None or t.stale():
            return None
        return t.available()
    def log_account_gate(self, context, tag: str, reasons: list,
                          want: int, final: int):
        """账户资金约束的 warn 模式留痕 (veto 模式的缩量/否决走已有日志)"""
        hit = next((r for r in reasons if r.startswith("account_margin(")),
                   None)
        if hit is None:
            return
        mode = getattr(self.h._risk.cfg, "account_gate_mode", "warn")
        if mode == "warn":
            self.h._veto_log(
                context, f"accwarn{tag}",
                f"{tag} 账户可用资金偏紧 (warn 模式, 未减手数): {hit}, "
                f"want={want}, final={final}")
    def periodic_truth_check(self, context):
        """周期性真值/框架仓对账 (不依赖日评估路径, 每 300s 一次)。

        2026-09-17 事故: 交易通道登录失败 → 引擎按止损"平仓"但券商未成交
        (sc 3 手 / m 12 手裸奔), 而差额告警只挂在日评估上, 那两天日评估
        又因会话事件缺失未触发 → 全程静默。现改为 tick 驱动定时检查,
        差额持续 >10 分钟升级为 truth_mismatch_persist 高危告警。
        """
        now = time.time()
        if now - self.h._truth_check_ts < self.h._TRUTH_CHECK_INTERVAL:
            return
        self.h._truth_check_ts = now
        code = self.h._inst.code
        tv = self.truth_net(code)
        if tv is None:
            return
        fw = float(context.stra_get_position(code))
        if int(round(tv)) == int(round(fw)):
            self.h._truth_mismatch_since = None
            return
        if self.h._truth_mismatch_since is None:
            self.h._truth_mismatch_since = now
        self.h._truth_mismatch_alert(context, code, fw, float(tv))
        held = now - self.h._truth_mismatch_since
        if held > self.h._TRUTH_ESCALATE_SEC:
            self.h._alert(
                "truth_mismatch_persist",
                f"{code} 账户真值 {tv:g} vs 框架仓 {fw:g} 持续 "
                f"{int(held / 60)} 分钟 — 疑似下单未成交/交易通道故障, "
                f"需人工核对券商持仓")
    def reconcile(self, context, closes):
        """引擎层与实际持仓持续失配检测。

        实盘部分成交/拒单/手工干预会造成实际持仓与引擎 layers 脱节
        (on_init 只收养一次)。连续 recon_patience 根 bar 失配则告警并
        按实际持仓重建为单层, 风控账本同步覆盖。单根失配(订单在途)不动作。

        持仓量优先取交易通道真值 (P0-3): 框架 stra_get_position 是信号
        口径 (set_position 即变), 与之对比等于同源自比, 抓不到执行层
        问题; 真值不可用时回退框架口径 (行为与升级前一致)。
        """
        code = self.h._inst.code
        pos, pos_src = self.truth_position(context, code)
        eng_pos = self.h._combined_target()
        try:
            pos_i = int(round(pos))
        except (TypeError, ValueError):
            return
        if pos_i == eng_pos:
            self.h._recon_miss = 0
            return

        self.h._recon_miss += 1
        if self.h._recon_miss < int(self.h._base.recon_patience):
            return

        n = self.h._last_n if np.isfinite(self.h._last_n) else 0.0
        if pos_i == 0:
            # 外部全平: 全部活跃子系统确认出场, 风控清账
            px = float(closes[-1])
            held = [s for s in self.h._subs
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
                f"{self.h.name()} RECONCILE: engine held "
                f"({','.join(s['name'] for s in held) or '-'}) but broker "
                f"flat, flatten @~{px:.2f} (entry~{avg_entry:.2f})", level=3)
            self.h._alert("reconcile", f"broker flat, engine flattened "
                                     f"@~{px:.2f}")
            for s in held:
                s["engine"].confirm_exit(px)
            self.h._risk.on_closed(code)
        else:
            d = 1 if pos_i > 0 else -1
            avgpx = abs(float(context.stra_get_position_avgpx(code) or 0))
            if avgpx <= 0:
                avgpx = float(closes[-1])
            context.stra_log_text(
                f"{self.h.name()} RECONCILE: mismatch persisted "
                f"(engine net={eng_pos}; broker {pos_i} [{pos_src}]), "
                f"rebuilt into "
                f"{self.h._subs[0]['name']} as single layer "
                f"[{abs(pos_i)}@{avgpx:.2f}]", level=3)
            self.h._alert("reconcile", f"mismatch persisted, rebuilt "
                                     f"{abs(pos_i)}@{avgpx:.2f}")
            first = self.h._subs[0]["engine"]
            first.adopt_position(d, abs(pos_i), avgpx, n if n > 0 else None)
            for s in self.h._subs[1:]:
                s["engine"].flatten_book()
            # 风险按重建后各层冻结 N 记账 (与单系统路径同口径)
            risk = sum(s["engine"].booked_risk(
                           self.h._volscale or 1.0,
                           self.h._base.stop_atr_mult,
                           stop=s["engine"].stop_anchor())
                       for s in self.h._subs)
            # 重建把所有净仓收进首 profile，其余 profile 已 flatten；
            # 先清 code 全量旧账，再只给首 profile 记账，避免旧分档残留。
            self.h._risk.on_closed(code)
            self.h._risk.sync_position(
                code, d, abs(pos_i), avgpx,
                n if n > 0 else 1.0, self.h._volscale or 1.0,
                self.h._base.stop_atr_mult, self.h._inst.group,
                risk=risk, sid=self.h.name(),
                units=float(self.h._inst.units),
                profile=self.h._subs[0]["name"])
        self.h._recon_miss = 0
