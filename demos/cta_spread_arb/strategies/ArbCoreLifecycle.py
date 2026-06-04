"""
ArbCoreLifecycle — ArbCore 的 WT 生命周期适配层

职责: 把 WT BaseCtaStrategy 的回调 (on_init / on_session_begin / on_session_end)
从 ArbCore 业务核心剥离出来, 让 ArbCore 聚焦于流水线编排和决策计算。

设计 (P4M-1, 2026-06-04):
- 组合关系: ArbCoreLifecycle(core) 持有 core 引用, 通过 core.<attr> 访问组件
- ArbCore.on_init / on_session_begin / on_session_end 改为薄 facade, 内部转发到 lifecycle
- 启动期 EMERGENCY 状态告警仍写入 core._startup_emergency, 保持对外可读语义不变
- 未来任何 "启动期 / 收盘期" 副作用(预热、对账、收盘报表) 统一归宿到本文件
"""
from __future__ import annotations


class ArbCoreLifecycle:
    """ArbCore 的 WT 生命周期回调适配器。

    所有对 core 内部组件的访问都走 self._core.<attr>, 不在本类持有重复状态。
    """

    def __init__(self, core):
        # 仅持引用, 避免循环初始化
        self._core = core

    # ------------------------------------------------------------------
    # WT on_init: K线/Tick订阅 + 指标注册 + 持久化状态恢复 + EMERGENCY 告警
    # ------------------------------------------------------------------
    def on_init(self, context, is_main_holder: bool = True):
        """注册K线/指标/恢复状态。

        - is_main_holder=True: 首腿用 isMain=True (CtaMocker 单 _main_key 限制)
        - is_main_holder=False: 所有 K 线都 isMain=False
        """
        core = self._core
        legs = core.legs
        main_code = legs[0].code

        # === K 线订阅 ===
        context.stra_prepare_bars(
            main_code, core.main_period, core.main_bar_cnt, isMain=is_main_holder)
        for leg in legs[1:]:
            context.stra_prepare_bars(
                leg.code, core.main_period, core.main_bar_cnt, isMain=False)
        for aux in core.aux_periods:
            for leg in legs:
                context.stra_prepare_bars(
                    leg.code, aux['period'], aux['bar_cnt'], isMain=False)

        # === Tick 订阅 ===
        for leg in legs:
            context.stra_sub_ticks(leg.code)

        # === 指标注册 ===
        idx_name = f"Spread_{core.name}"
        context.register_index(idx_name, idxType=1)
        context.register_index_line(idx_name, "spread", lineType=0)
        context.register_index_line(idx_name, "upper", lineType=0)
        context.register_index_line(idx_name, "lower", lineType=0)
        context.register_index_line(idx_name, "signal", lineType=1)
        context.add_index_baseline(idx_name, "signal", 0.0)
        context.register_index_line(idx_name, "regime_val", lineType=1)
        context.register_index_line(idx_name, "regime_dir", lineType=1)
        context.register_index_line(idx_name, "regime_conf", lineType=0)

        fidx_name = f"Factors_{core.name}"
        context.register_index(fidx_name, idxType=1)
        for fname in core.fe.get_enabled_factor_names():
            context.register_index_line(fidx_name, fname, lineType=0)

        context.set_chart_kline(main_code, core.main_period)

        # === 持久化状态恢复 ===
        direction, last_signal_time = core.sm.load_all(context, core.rm)
        # P0-9: 恢复执行状态
        # R2-A: 收集 EMERGENCY 重置告警, 末尾醒目输出
        emergency_payload = core.sm.load_exec_state(context, core.eh, diag=core._diag)
        if direction != 0:
            core.eh.restore_state(direction)
            context.stra_log_text(
                f"{core.name} restored direction={direction}, state=opened")

        # R2-A: 启动期 EMERGENCY 醒目告警 — 用 ★★★ 前缀让 WT 监控前端易抓取
        if emergency_payload is not None:
            banner = (
                "★★★ EMERGENCY: exec_state interrupted on previous run ★★★ "
                f"sub={emergency_payload['sub_name']} "
                f"prev_state={emergency_payload['interrupted_state']} "
                f"direction={emergency_payload['direction']} "
                f"abandoned_pending={emergency_payload['abandoned_pending']} "
                "→ state reset to IDLE; MANUAL position reconciliation required."
            )
            # level=3 是 WT 的 error 级别
            context.stra_log_text(banner, level=3)
            # 同步保存供 SpreadArbStrategy.on_init 二次输出与单测断言
            core._startup_emergency = emergency_payload
        else:
            core._startup_emergency = None

    # ------------------------------------------------------------------
    # WT on_session_begin: 交易日开始
    # ------------------------------------------------------------------
    def on_session_begin(self, context):
        """交易日开始: 重置因子refit。"""
        self._core.fe.reset_refit()

    # ------------------------------------------------------------------
    # WT on_session_end: 交易日结束
    # ------------------------------------------------------------------
    def on_session_end(self, context):
        """交易日结束: 输出绩效 + flush诊断日志。"""
        core = self._core
        unrealized = sum(
            context.stra_get_position_profit(leg.code) for leg in core.legs)
        total_pnl = core.rm.realized_pnl + unrealized
        context.stra_log_text(
            f"{core.name} session end, realized={core.rm.realized_pnl:.2f}, "
            f"unrealized={unrealized:.2f}, total={total_pnl:.2f}")
        core._diag.flush_all()
