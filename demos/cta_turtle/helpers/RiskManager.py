"""组合级风险管理闸门 (P0-2)

在 PositionSizer(VaR 反算)之后做最后缩量/否决:
1. 单品种风险预算: 本笔持仓 2N 止损潜亏 ≤ equity × max_risk_per_mkt
2. 组合总敞口预算: 全部在途持仓 2N 总潜亏 ≤ equity × max_total_risk_frac
3. 保证金占用预估: Σ(lots×price×volscale×margin_rate) ≤ equity × max_margin_usage
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
from schemas import RiskConfig


class RiskManager:
    def __init__(self, cfg: RiskConfig = None):
        self.cfg = cfg or RiskConfig()
        self.cfg.validate()
        # code -> {"dir","lots","price","n","volscale","stop_mult",
        #          "group","risk"}  risk=该笔 2N 潜亏记账
        self._book = {}
        self.last_equity_source = ""   # 观测用: dynamic|fallback

    # ------------------------------------------------------------------
    # 权益
    # ------------------------------------------------------------------
    def equity(self, context) -> float:
        """动态权益 = 基准资金 + 净盈亏; 接口不可用时退回基准资金。

        口径注意: stra_get_fund_data(0) 在 bt (CtaMocker.cpp:1883) 与
        实盘 (CtaStraBaseCtx.cpp:1711) 都是 profit-fees+dynprofit, 即
        **相对初始资金的净盈亏, 不含本金** — 因此权益 = 基准资金
        (fallback_capital, 与回测 capital/实盘初始权益对齐) + 净盈亏。
        旧口径曾把它误当含本金权益直接返回: bt 下首笔盈利后
        equity 塌缩为净盈亏值, 后续入场被预算闸全部否决
        (2026-09-02 cu 多品种回测暴露)。
        """
        try:
            fd = context.stra_get_fund_data(0)
        except AttributeError:
            fd = None
        except Exception:
            fd = None
        base = float(self.cfg.fallback_capital)
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
                  group: str = "", share: float = 1.0) -> tuple:
        """对本次入场/加仓手数做缩量或否决。

        Args:
            share: 该笔请求占用的品种内预算份额 (S1+S2 双系统按
                子系统 units 占比传入, 合计=1.0; 单系统=1.0)。
                仅缩放"单品种风险预算"与"保证金预算"的权益基数,
                使双系统公平共享品种额度而不互相挤占;
                组合总敞口仍为全局口径。

        Returns:
            (final_lots: int, reasons: list[str])
            final_lots == 0 表示整体否决, reasons 给出全部触发原因;
            0 < final_lots < lots 表示缩量放行, reasons 为触发的约束项。
        """
        reasons = []
        if lots <= 0 or n <= 0 or volscale <= 0 or price <= 0 \
                or stop_mult <= 0 or equity <= 0 or not 0 < share <= 1.0:
            return 0, ["invalid_input"]

        rpl = n * volscale * stop_mult           # 单手 2N 潜亏
        new_unit = code not in self._book

        # 1. 单品种风险预算 (按份额缩放)
        mkt_cap = int(equity * share * self.cfg.max_risk_per_mkt // rpl)

        # 2. 组合总敞口预算 (存量记账 + 本次按申请量计)
        used = sum(e["risk"] for e in self._book.values())
        tot_cap = int(max(0.0, equity * self.cfg.max_total_risk_frac - used)
                      // rpl)

        # 3. 保证金占用预估 (预算基数按份额缩放, 扣减全局已占)
        per_lot_margin = price * volscale * self.cfg.margin_rate
        cur_margin = sum(e["lots"] * e["price"] * e["volscale"]
                         * self.cfg.margin_rate
                         for e in self._book.values())
        mar_cap = int(max(0.0, equity * share * self.cfg.max_margin_usage
                          - cur_margin * share) // per_lot_margin)

        final = min(lots, mkt_cap, tot_cap, mar_cap)
        if mkt_cap < lots:
            reasons.append(f"per_market_risk(cap={mkt_cap})")
        if tot_cap < lots:
            reasons.append(f"total_risk(cap={tot_cap})")
        if mar_cap < lots:
            reasons.append(f"margin(cap={mar_cap})")

        # 4. 市场单位限制 (只约束新开仓)
        if new_unit and lots > 0:
            grp = group or ""
            total_units = len(self._book)
            grp_units = self.units_in_group(grp)
            grp_veto = None
            if grp and self.cfg.max_units_per_group > 0 \
                    and grp_units >= self.cfg.max_units_per_group:
                grp_veto = (f"group_units({grp}={grp_units}"
                            f">={self.cfg.max_units_per_group})")
            elif self.cfg.max_units_total > 0 \
                    and total_units >= self.cfg.max_units_total:
                grp_veto = (f"total_units({total_units}"
                            f">={self.cfg.max_units_total})")
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
                  stop_mult: float, group: str = ""):
        if code in self._book and \
                self._book[code]["dir"] == direction:
            e = self._book[code]
            tot = e["lots"] + lots_added
            e["price"] = (e["price"] * e["lots"]
                          + price * lots_added) / tot
            e["lots"] = tot
            e["n"] = n
            e["risk"] += lots_added * n * volscale * stop_mult
        else:
            self._book[code] = {
                "dir": direction, "lots": lots_added, "price": price,
                "n": n, "volscale": volscale, "stop_mult": stop_mult,
                "group": group or "",
                "risk": lots_added * n * volscale * stop_mult}

    def on_closed(self, code: str):
        self._book.pop(code, None)

    def on_rebuilt(self, code: str, direction: int, lots: int,
                   price: float, n: float, volscale: float,
                   stop_mult: float, group: str = ""):
        """对账重建: 以实际持仓覆盖式重记账"""
        self.on_closed(code)
        if lots > 0 and direction != 0:
            self.on_filled(code, direction, lots, price, n,
                           volscale, stop_mult, group)

    def sync_position(self, code: str, direction: int, lots: int,
                      price: float, n: float, volscale: float,
                      stop_mult: float, group: str = ""):
        """批量执行后按合并目标仓位同步账本 (S1+S2 双系统:
        两子系统共享同一 code 记账 — 单位限制/总敞口按市场口径)"""
        self.on_rebuilt(code, direction, lots, price, n,
                        volscale, stop_mult, group)

    # ------------------------------------------------------------------
    # 观测
    # ------------------------------------------------------------------
    def open_risk(self) -> float:
        """在途 2N 总潜亏记账值"""
        return sum(e["risk"] for e in self._book.values())

    @property
    def open_units(self) -> int:
        return len(self._book)

    def units_in_group(self, group: str) -> int:
        if not group:
            return 0
        return sum(1 for e in self._book.values() if e["group"] == group)

    def has_position(self, code: str) -> bool:
        return code in self._book
