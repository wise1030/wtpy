"""组合级风险管理闸门 (P0-2)

在 PositionSizer(VaR 反算)之后做最后缩量/否决:
1. 单品种风险预算: 本笔持仓 2N 止损潜亏 ≤ equity × max_risk_per_mkt
2. 组合总敞口预算: 全部在途持仓 2N 总潜亏 ≤ equity × max_total_risk_frac
3. 保证金占用预估: 本策略 Σ(lots×price×volscale×margin_rate)
   ≤ equity × max_margin_usage (2026-10-08 方案B: 按策略 sid 收窄,
   只扣减本策略自身持仓的预估保证金; 旧组合全局口径曾因
   "单策略预算基数 vs 全组合扣减" 错配把全体品种的保证金预算占满)
4. 市场单位限制 (经典海龟): 同相关性组同时持仓品种数 ≤ max_units_per_group,
   全组合 ≤ max_units_total; 单位限制只作用于新开仓(加仓不改变单位数)

口径说明:
- 单手 2N 潜亏 rpl = n × volscale × stop_mult; 加仓层按加仓时点 N 记账,
  存量风险为近似值(不逐日重估), 用于预算扣减足够保守。
- 权益由调用方传入 (stra_get_fund_data(0) 净盈亏 + fallback_capital
  基准资金, 见 equity() 口径注释); 接口不可用时退回 fallback_capital,
  保证回测/实盘/测试三态行为一致。
- 进程内多品种共享同一 RiskManager 实例即可协同 (runLive 装配);
  单品种回测每进程私有实例, 行为等价。
"""
import math

from schemas import RiskConfig


class RiskManager:
    def __init__(self, cfg: RiskConfig = None):
        self.cfg = cfg or RiskConfig()
        self.cfg.validate()
        # (code, profile) -> {"dir","lots","price","n","volscale",
        #                     "stop_mult","group","risk","sid","units"}
        # profile="" 为缺省单 profile 口径，与旧行为完全等价。
        self._book = {}
        # code -> {"units","group"}：单位数/相关性组是 code 级属性，
        # 多 profile 同 code 只计一次，避免 open_units 双计。
        self._code_meta = {}
        self.last_equity_source = ""   # 观测用: dynamic|fallback
        self._sid_capital = {}   # sid → 分配资金基数 (S1 资金分配)
        self._sid_contexts = {}  # sid → 策略上下文（组合净盈亏汇总）

    # ------------------------------------------------------------------
    # 权益
    # ------------------------------------------------------------------

    def set_sid_capital(self, sid: str, capital: float):
        """注册策略分配资金基数 (共享账本 × 每策略独立权益口径)。

        2026-10-22 S1: runLive 全组合共享一个 RiskManager 账本, 但
        portfolio_capital 分配后每策略的权益基数不同 —— 单位/总敞口闸
        共享, 风险/保证金比例闸按各自分配额。未注册的 sid 回退
        cfg.fallback_capital。"""
        if capital > 0:
            self._sid_capital[sid] = float(capital)

    def capital_base(self, sid: str = None) -> float:
        return self._sid_capital.get(sid, float(self.cfg.fallback_capital))

    def register_sid_context(self, sid: str, context):
        """登记策略上下文，供分配模式汇总组合净盈亏（on_init 调用）。"""
        self._sid_contexts[sid] = context

    def portfolio_equity(self, context=None, sid: str = None) -> float:
        """组合总权益：分配开启时用总池+全部 sid 净盈亏。

        未开启 portfolio_capital 时保留旧的调用方 equity 口径，避免改变
        既有单资金池行为。分配模式下该值与发起策略 sid 无关。
        """
        if self.cfg.portfolio_capital <= 0:
            return self.equity(context, sid=sid)
        pnl = 0.0
        for ctx in self._sid_contexts.values():
            try:
                fd = ctx.stra_get_fund_data(0)
            except Exception:
                continue
            if isinstance(fd, (int, float)) and math.isfinite(fd):
                pnl += float(fd)
        return float(self.cfg.portfolio_capital) + pnl

    def equity(self, context, sid: str = None) -> float:
        """动态权益 = 基准资金 + 净盈亏; 接口不可用时退回基准资金。

        口径注意: stra_get_fund_data(0) 在 bt (CtaMocker.cpp:1883) 与
        实盘 (CtaStraBaseCtx.cpp:1711) 都是 profit-fees+dynprofit, 即
        **相对初始资金的净盈亏, 不含本金** — 因此权益 = 基准资金
        (分配额/fallback_capital, 与回测 capital/实盘初始权益对齐)
        + 净盈亏。旧口径曾把它误当含本金权益直接返回: bt 下首笔盈利后
        equity 塌缩为净盈亏值, 后续入场被预算闸全部否决
        (2026-09-02 cu 多品种回测暴露)。
        """
        try:
            fd = context.stra_get_fund_data(0)
        except AttributeError:
            fd = None
        except Exception:
            fd = None
        base = self.capital_base(sid)
        if isinstance(fd, (int, float)) and fd == fd:  # 排除 NaN
            self.last_equity_source = "dynamic"
            return base + float(fd)
        self.last_equity_source = "fallback"
        return base

    # ------------------------------------------------------------------
    # 下单前闸门
    # ------------------------------------------------------------------
    def cap_entry(self, code: str, *, lots: int, n: float, volscale: float,
                  price: float, stop_mult: float, equity: float,
                  group: str = "", share: float = 1.0,
                  code_share: float = None, avail: float = None,
                  sid: str = "", units: float = 1.0,
                  profile: str = "",
                  post_risk_base: float = None,
                  post_profile_risk_base: float = None,
                  post_risk_per_lot: float = None,
                  portfolio_equity: float = None) -> tuple:
        """对本次入场/加仓手数做缩量或否决。

        Args:
            share: 该 profile 请求占用的**本笔风险预算**份额 (S1+S2/
                speeds 按 ProfileSpec.share 传入; 单 profile=1.0)。
            code_share: 该 code 全部 profile 合计占用的品种预算份额
                (缺省=share，保持旧单 profile 行为)。份额预算的语义是
                "每个 profile 消费自己的份额、扣减本 profile 占用"；
                各 profile share 合计=code_share，因此合计不会突破 code
                全额预算，且首个 profile 不能挤占后续 profile 份额。
            profile: 账本分档键；出场/重建可按子系统精确清除。缺省
                "" 与旧单 profile 行为完全等价。
            sid: 发起策略标识 (方案B)。保证金扣减只计本策略自身持仓
                (账本按 sid 分组), 其他策略的保证金占用不影响本策略的
                保证金预算; 组合总敞口/单位限制仍为全局口径不变。
            avail: 券商口径**账户可用资金** (交易通道真值源提供, 共享
                 账户下含其他策略占用)。按 account_gate_mode 处理:
                 off 忽略 / warn 只记录原因 / veto 缩量优先、归零否决。
                 与第 3 项的区别: 第 3 项用本策略记账估算保证金, 本项
                 用券商真实可用资金 (已含其他策略占用), 是"资金耗尽"
                 事故 (2026-09-09 p 追单 3357 次被拒) 的防线。
            units: 新开该市场后将计入账本的名义单位数。单位闸按
                open_units + units 做容量前置检查，避免旧实现只查
                open_units >= max，导致已占 5/上限 6/新单位 2 仍放行到 7。
            post_risk_base: 加仓共同止损重锚后、尚未计新层时的该 code
                全 profile 风险；用于组合总敞口替换。缺省沿用旧风险。
            post_profile_risk_base: 同上，但仅当前 profile 的风险；用于
                本 profile 累计预算。缺省用当前 profile 的旧风险。
            post_risk_per_lot: 新层每手风险，需与 post_risk_base 同时提供。
            portfolio_equity: 组合总权益，仅用于总风险预算；缺省沿用
                equity，兼容单资金池。分配模式下必须显式传入统一组合值。

        Returns:
            (final_lots: int, reasons: list[str])
            final_lots == 0 表示整体否决, reasons 给出全部触发原因;
            0 < final_lots < lots 表示缩量放行, reasons 为触发的约束项。
        """
        reasons = []
        if lots <= 0 or n <= 0 or volscale <= 0 or price <= 0 \
                or stop_mult <= 0 or equity <= 0 or not 0 < share <= 1.0:
            return 0, ["invalid_input"]
        if (post_risk_base is None) != (post_risk_per_lot is None):
            return 0, ["invalid_post_risk"]
        if post_risk_base is not None and (
                not math.isfinite(post_risk_base) or post_risk_base < 0
                or not math.isfinite(post_risk_per_lot)
                or post_risk_per_lot <= 0):
            return 0, ["invalid_post_risk"]
        if portfolio_equity is not None and (
                not math.isfinite(portfolio_equity) or portfolio_equity <= 0):
            return 0, ["invalid_portfolio_equity"]

        rpl = n * volscale * stop_mult           # 单手 2N 潜亏
        # code_share 缺省回退 share：单 profile 公式与旧实现逐字等价。
        cs = share if code_share is None else float(code_share)
        if not 0 < cs <= 1.0:
            return 0, ["invalid_code_share"]
        new_unit = code not in self._code_meta

        # 1. 单品种风险预算
        #    1a. 本笔上限按 profile share；
        #    1b. 品种累计上限同样只消费本 profile 的 share 份额，并只扣
        #        本 profile 已占风险。各 profile share 合计=code_share，
        #        因此总体仍受 code 全额预算约束，但首个 profile 不能挤占
        #        后续 profile 的份额。
        order_cap = int(equity * share * self.cfg.max_risk_per_mkt // rpl)
        profile_key = (code, str(profile))
        old_profile_risk = float(self._book.get(profile_key, {})
                                 .get("risk", 0.0))
        profile_used = old_profile_risk if post_profile_risk_base is None \
            else max(0.0, float(post_profile_risk_base))
        remaining_rpl = rpl if post_risk_per_lot is None \
            else float(post_risk_per_lot)
        cum_cap = int(max(
            0.0,
            equity * share * self.cfg.max_risk_per_market - profile_used)
            // remaining_rpl)
        mkt_cap = min(order_cap, cum_cap)

        # 2. 组合总敞口预算 (全局口径不变)。加仓提供 code 全 profile
        # 的 post_risk_base 时，用重锚后的 code 风险替换旧值。
        old_code_risk = self.code_risk(code)
        code_used = old_code_risk if post_risk_base is None \
            else max(0.0, float(post_risk_base))
        used = sum(e["risk"] for e in self._book.values())
        if post_risk_base is not None:
            used = used - old_code_risk + code_used
        total_equity = equity if portfolio_equity is None else portfolio_equity
        tot_cap = int(max(
            0.0, total_equity * self.cfg.max_total_risk_frac - used)
            // remaining_rpl)

        # 3. 保证金占用预估：预算按本 profile share，扣减本 profile 本
        # sid 持仓。份额预算之和=code 全额预算，避免首 profile 挤占。
        per_lot_margin = price * volscale * self.cfg.margin_rate
        cur_margin = sum(e["lots"] * e["price"] * e["volscale"]
                         * self.cfg.margin_rate
                         for k, e in self._book.items()
                         if k == profile_key and e.get("sid", "") == sid)
        mar_cap = int(max(0.0, equity * share * self.cfg.max_margin_usage
                          - cur_margin) // per_lot_margin)

        final = min(lots, mkt_cap, tot_cap, mar_cap)
        if mkt_cap < lots:
            if cum_cap < order_cap:
                reasons.append(
                    f"per_market_cum(cap={cum_cap},used={profile_used:.0f})")
            else:
                reasons.append(f"per_market_risk(cap={mkt_cap})")
        if tot_cap < lots:
            reasons.append(f"total_risk(cap={tot_cap})")
        if mar_cap < lots:
            reasons.append(f"margin(cap={mar_cap})")

        # 5. 账户可用资金闸门 (共享账户; 真值源可用时)
        if avail is not None and self.cfg.account_gate_mode != "off" \
                and per_lot_margin > 0:
            base = max(0.0, float(avail))
            reserve = base * self.cfg.account_reserve_frac
            affordable = int(max(0.0, base - reserve) // per_lot_margin)
            if affordable < final:
                reasons.append(
                    f"account_margin(avail={base:.0f},"
                    f"reserve={reserve:.0f},affordable={affordable})")
                if self.cfg.account_gate_mode == "veto":
                    final = max(0, affordable)

        # 4. 市场单位限制 (只约束新开仓)：按**加入后的投影单位数**
        # 前置检查，而不是只看当前是否已满。units=2 的新市场在
        # 已占 5/上限 6 时也必须否决，不能放行后到 7。
        if new_unit and lots > 0:
            grp = group or ""
            market_units = max(0.0, float(units))
            total_units = self.open_units + market_units
            grp_units = self.units_in_group(grp) + market_units
            grp_veto = None
            if grp and self.cfg.max_units_per_group > 0 \
                    and grp_units > self.cfg.max_units_per_group:
                grp_veto = (f"group_units({grp}={grp_units:g}"
                            f">{self.cfg.max_units_per_group})")
            elif self.cfg.max_units_total > 0 \
                    and total_units > self.cfg.max_units_total:
                grp_veto = (f"total_units({total_units:g}"
                            f">{self.cfg.max_units_total})")
            if grp_veto:
                return 0, reasons + [grp_veto]

        if final <= 0 and not reasons:
            reasons.append("zero_capacity")
        return max(0, final), reasons

    # ------------------------------------------------------------------
    # 成交回报记账 (策略层 confirm_* 后调用)
    # ------------------------------------------------------------------
    def on_filled(self, code: str, direction: int, lots_added: int,
                  price: float, n: float, volscale: float,
                  stop_mult: float, group: str = "", sid: str = "",
                  units: float = 1.0, profile: str = ""):
        """成交记账；profile 分档，避免多子系统价格/风险互染。

        units 是 code 级经典单位数，存入 `_code_meta`；同 code 多
        profile 只计一次单位，units=2/双系统/多速度不再被绕过的同时
        也不会被重复计数。
        """
        key = (code, str(profile))
        u = max(0.0, float(units))
        self._code_meta[code] = {"units": u, "group": group or ""}
        if key in self._book and self._book[key]["dir"] == direction:
            e = self._book[key]
            tot = e["lots"] + lots_added
            e["price"] = (e["price"] * e["lots"]
                          + price * lots_added) / tot
            e["lots"] = tot
            e["n"] = n
            e["sid"] = sid           # 归属策略 (方案B 分组键)
            e["units"] = u
            e["risk"] += lots_added * n * volscale * stop_mult
        else:
            self._book[key] = {
                "dir": direction, "lots": lots_added, "price": price,
                "n": n, "volscale": volscale, "stop_mult": stop_mult,
                "group": group or "", "sid": sid, "units": u,
                "risk": lots_added * n * volscale * stop_mult}

    def on_closed(self, code: str, profile: str = None):
        """清除持仓账。

        profile=None（出场/全平/净仓语义）清除该 code 全部 profile；
        显式 profile 只清除一条，供多子系统部分出场使用。
        """
        if profile is None:
            self._book = {k: v for k, v in self._book.items()
                          if k[0] != code}
            self._code_meta.pop(code, None)
            return
        self._book.pop((code, str(profile)), None)
        if not any(k[0] == code for k in self._book):
            self._code_meta.pop(code, None)

    def on_rebuilt(self, code: str, direction: int, lots: int,
                   price: float, n: float, volscale: float,
                   stop_mult: float, group: str = "", risk: float = None,
                   sid: str = "", units: float = 1.0, profile: str = ""):
        """对账重建：覆盖该 (code, profile) 单条账，不动其他 profile。

        risk: 显式潜亏记账值。多子系统持仓应按各层冻结 N 分 profile
        汇总后传入，避免账面风险随当根 N 漂移 (B6)。
        sid: 持仓归属策略 (方案B 保证金分组键)。
        """
        self.on_closed(code, profile=profile)
        if lots > 0 and direction != 0:
            self.on_filled(code, direction, lots, price, n,
                           volscale, stop_mult, group, sid=sid, units=units,
                           profile=profile)
            if risk is not None:
                self._book[(code, str(profile))]["risk"] = float(risk)

    def sync_position(self, code: str, direction: int, lots: int,
                      price: float, n: float, volscale: float,
                      stop_mult: float, group: str = "",
                      risk: float = None, sid: str = "",
                      units: float = 1.0, profile: str = ""):
        """按 (code, profile) 同步账本；单位限制仍按 code 级 `_code_meta`。"""
        self.on_rebuilt(code, direction, lots, price, n,
                        volscale, stop_mult, group, risk=risk, sid=sid,
                        units=units, profile=profile)

    # ------------------------------------------------------------------
    # 观测
    # ------------------------------------------------------------------
    def open_risk(self) -> float:
        """在途总潜亏记账值（全 code × 全 profile 合计）"""
        return sum(e["risk"] for e in self._book.values())

    def code_risk(self, code: str) -> float:
        """该 code 全部 profile 已占风险（累计闸扣减值）"""
        return sum(e["risk"] for k, e in self._book.items() if k[0] == code)

    def entry(self, code: str, profile: str = ""):
        """读取单条 (code, profile) 账本；缺省 profile 兼容单 profile。"""
        return self._book.get((code, str(profile)))

    @property
    def open_units(self) -> float:
        """全组合占用单位数（code 级去重，多 profile 不双计）"""
        return sum(float(m.get("units", 1.0))
                   for m in self._code_meta.values())

    def units_in_group(self, group: str) -> float:
        if not group:
            return 0.0
        return sum(float(m.get("units", 1.0))
                   for code, m in self._code_meta.items()
                   if m.get("group", "") == group)

    def has_position(self, code: str) -> bool:
        return any(k[0] == code for k in self._book)

    def has_position_profile(self, code: str, profile: str) -> bool:
        return (code, str(profile)) in self._book
