"""执行模式模型 (A2-v1, 2026-10-22): 模式分支的单一决策点

背景: StraTurtle 运行期散布 26 处模式分支
(close/trigger × bt/live × d1/h1 × intrabar_stop × bt_no_simtick),
历史缺陷 (`with_oi=False` 只在 session 路径、`_reanchor_on_roll` 漏挂
一条评估路径) 全部来自"同一决策在多处各算一遍"。

v1 口径 (诚实边界): **决策收敛, 不急于改写执行路径** ——
`StrategyMode` 在 on_init 一次算定全部派生旗标, 分支点改为读
mode.xxx 而非各自重算条件; ExecutionModel 接口 (撮合/证据/时钟
完整抽象) 留待 v2 (需要先把 h1/trigger 组合回测与生产模式矩阵
跑齐, 才能安全定义接口边界)。
"""
import dataclasses


@dataclasses.dataclass(frozen=True)
class StrategyMode:
    """on_init 一次算定的运行模式面 (运行期只读)。"""
    is_backtest: bool
    period: str              # 信号周期 d1|h1
    entry_mode: str          # close|trigger
    intrabar_stop: bool
    bt_no_simtick: bool

    # ---- 派生 ----
    @property
    def is_h1(self) -> bool:
        return self.period == "h1"

    @property
    def main_period(self) -> str:
        """主K周期: h1→m5(策略层聚合原料); intrabar_stop→盘中主K;
        否则=信号周期"""
        if self.is_h1 or self.intrabar_stop:
            return self._intrabar_period
        return self.period

    @property
    def calc_path(self) -> str:
        """on_calculate 分派: h1|intrabar|daily"""
        if self.is_h1:
            return "h1"
        if self.intrabar_stop:
            return "intrabar"
        return "daily"

    @property
    def session_flush(self) -> str:
        """session_end 分派: h1_flush(尾部桶闭合)|daily_synth(实盘日线
        m5 合成评估 — trigger 武装 + close 收盘确认决策共同依赖;
        实盘 d1 永不闭合 F3, 两种 entry_mode 都须由会话末合成日K 驱动;
        bt 日K 闭合走 on_calculate, 不启用)|none"""
        if self.is_h1:
            return "h1_flush"
        if self.entry_mode == "trigger":
            return "daily_synth"
        if self.entry_mode == "close" and not self.is_backtest:
            # close+live+intrabar_stop: session_end 合成日K 驱动收盘
            # 决策, m5 主K盘中扫描止损, 两者互补不冲突
            return "daily_synth"
        return "none"

    @property
    def wants_ticks(self) -> bool:
        """trigger 模式且有 m5 模拟链 → 订阅 tick 盘中触发"""
        return self.entry_mode == "trigger" and not self.bt_no_simtick

    @property
    def wants_catchup(self) -> bool:
        """实盘日线的日级 catchup 自愈 (h1/盘中模式/bt 关闭): trigger
        武装与 close 收盘决策共同依赖 —— 重启/数据缺口时首 tick 用
        his 补评估, 否则空跑到 session_end (2026-09-21 事故)"""
        return (self.entry_mode in ("trigger", "close")
                and not self.is_h1
                and not self.intrabar_stop
                and not self.is_backtest)

    _intrabar_period: str = "m5"

    @classmethod
    def resolve(cls, base, inst, is_backtest: bool,
                bt_no_simtick: bool) -> "StrategyMode":
        return cls(is_backtest=bool(is_backtest),
                   period=inst.period,
                   entry_mode=base.entry_mode,
                   intrabar_stop=bool(inst.intrabar_stop),
                   bt_no_simtick=bool(bt_no_simtick),
                   _intrabar_period=base.intrabar_period)
