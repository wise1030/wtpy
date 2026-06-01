"""
SpreadArbStrategy — CTA跨品种套利策略(协调者)

v8架构: 使用schemas.py结构化配置，新增品种只需在YAML中加一条配置。

优化:
- ConfigManager直接返回结构化对象(SignalConfig/RiskConfig/RegimeConfig)
- SubStrategy接收结构化对象，不再需要isinstance(dict)兼容代码
- 添加on_backtest_end回调(确保DiagLogger最终flush)
- on_tick中不再调用on_tick_check(避免与process()状态机冲突)
"""
from wtpy import BaseCtaStrategy
from wtpy import CtaContext

from ConfigManager import ConfigManager
from SubStrategy import SubStrategy
from schemas import SubStrategyConfig, SignalConfig, RiskConfig, RegimeConfig, ExecConfig
from dataclasses import replace as _dc_replace
import yaml as _yaml


class SpreadArbStrategy(BaseCtaStrategy):

    def __init__(self, name: str, strategy_config_path: str,
                 factor_config_path: str, is_backtest: bool = False):
        BaseCtaStrategy.__init__(self, name)

        self._is_backtest = is_backtest
        self._cfg_mgr = ConfigManager(strategy_config_path)
        self._factor_config_path = factor_config_path
        self._cur_date = 0
        self._code_to_subs = {}  # code -> [SubStrategy, ...]

        # 结构化配置(直接从ConfigManager获取，不再手动from_yaml)
        self._signal_cfg = self._cfg_mgr.signal_cfg
        self._risk_cfg = self._cfg_mgr.risk_cfg
        self._regime_cfg = self._cfg_mgr.regime_cfg
        self._exec_cfg = self._cfg_mgr.exec_cfg  # P1-2: 执行参数配置

        # v7.4: 合约级param_profile支持
        # 每个strategy配置可声明signal_overrides / risk_overrides 字段，
        # 在全局signal/risk_cfg基础上覆盖部分参数，实现合约级动态调参。
        # 例: rb_hc_2510_m5 的 LONG 方向亏损严重，可单独提高 entry_threshold 和
        #     降低 short_entry_multiplier 而不影响 rb_hc_2601_m5。
        # 也支持顶层 param_profiles 段:
        #   param_profiles:
        #     trending_contract:
        #       signal: {entry_threshold: 0.60, short_entry_multiplier: 0.7}
        # 然后在 strategy 中 `param_profile: trending_contract` 引用。
        with open(strategy_config_path, encoding='utf-8') as f:
            _raw_cfg = _yaml.safe_load(f)
        self._param_profiles = _raw_cfg.get('param_profiles', {}) or {}
        # v8.1: 自动 profile 选择配置
        self._auto_profile_cfg = _raw_cfg.get('param_profile_auto', {}) or {}

        # 创建子策略容器 — 传入结构化对象而非raw dict
        self._subs = []
        for sub_cfg in self._cfg_mgr.strategies:
            # v8.1: auto_profile 在 _resolve_sub_cfg 之前介入, 把判定结果写回 sub_cfg
            self._maybe_apply_auto_profile(sub_cfg)
            sub_config = SubStrategyConfig.from_yaml(sub_cfg)
            # v7.4: 应用 param_profile / overrides
            sub_signal_cfg, sub_risk_cfg = self._resolve_sub_cfg(sub_cfg)
            sub = SubStrategy(
                sub_config=sub_config,
                risk_cfg=sub_risk_cfg,
                signal_cfg=sub_signal_cfg,
                adaptive_cfg=self._regime_cfg,
                factor_config_path=factor_config_path,
                exec_cfg=self._exec_cfg,  # P1-2: 传入ExecConfig
                is_backtest=is_backtest,
            )
            self._subs.append(sub)

            # 构建code查找表(用于on_tick路由)
            for leg in sub.legs:
                self._code_to_subs.setdefault(leg.code, []).append(sub)

        # P1-e: leg 唯一性校验 — WT stra_set_position 是 code 级覆盖语义,
        # 同一 stdCode 被多个 sub 操作会导致仓位互相覆盖。
        # PositionRouter (P3) 上线后此校验可放宽为 "同 code 允许多 sub, 但方向须一致"。
        self._check_leg_uniqueness()

    @classmethod
    def from_experiment(cls, name: str, experiment: str = 'default',
                        config_root: str = './config',
                        is_backtest: bool = False):
        """v9 工厂方法: 从分层配置创建策略实例。

        替代 __init__(strategy_config_path, factor_config_path) 的新入口,
        内部走 ConfigResolver 4级合并, 但装出与 __init__ 等价的实例状态。

        Args:
            name: 策略实例名 (传给 BaseCtaStrategy)
            experiment: 实验名 (对应 config/experiments/<name>.yaml)
            config_root: config/ 目录路径
            is_backtest: 是否回测模式
        """
        from ConfigResolver import ConfigResolver
        rc = ConfigResolver(config_root).resolve(experiment)

        obj = cls.__new__(cls)
        BaseCtaStrategy.__init__(obj, name)

        obj._is_backtest = is_backtest
        obj._cfg_mgr = rc   # ResolvedConfig 鸭子兼容 ConfigManager
        # v9: factor_config_path 直接指向 base/factors.yaml (FactorEngine 读 yaml)
        import os as _os
        obj._factor_config_path = _os.path.join(
            _os.path.abspath(config_root), 'base', 'factors.yaml')
        obj._cur_date = 0
        obj._code_to_subs = {}

        obj._signal_cfg = rc.signal_cfg
        obj._risk_cfg = rc.risk_cfg
        obj._regime_cfg = rc.regime_cfg
        obj._exec_cfg = rc.exec_cfg

        # v8.1 兼容字段 — ResolvedConfig.param_profiles 已加载 profiles/*.yaml
        obj._param_profiles = rc.param_profiles
        obj._auto_profile_cfg = rc.auto_profile_cfg

        # 创建子策略 (与 __init__ 同逻辑)
        obj._subs = []
        for sub_cfg in rc.strategies:
            obj._maybe_apply_auto_profile(sub_cfg)
            sub_config = SubStrategyConfig.from_yaml(sub_cfg)
            sub_signal_cfg, sub_risk_cfg = obj._resolve_sub_cfg(sub_cfg)
            sub = SubStrategy(
                sub_config=sub_config,
                risk_cfg=sub_risk_cfg,
                signal_cfg=sub_signal_cfg,
                adaptive_cfg=obj._regime_cfg,
                factor_config_path=obj._factor_config_path,
                exec_cfg=obj._exec_cfg,
                is_backtest=is_backtest,
            )
            obj._subs.append(sub)

            for leg in sub.legs:
                obj._code_to_subs.setdefault(leg.code, []).append(sub)

        obj._check_leg_uniqueness()
        return obj

    def _check_leg_uniqueness(self):
        """P1-e: 校验同一 stdCode 不出现在多个 sub 的 legs 中。

        WT 的 `stra_set_position(stdCode, qty)` 是 code 级覆盖语义,
        若两个 sub 都把 rb2510 列为腿, A 设置 +2 后 B 设置 -1, rb2510 实际净仓
        会被覆盖为 -1, A 的开仓意图丢失。

        解决方案 (按阶段):
          - P1 (当前): 启动时硬拦截, 同 code 不允许多 sub
          - P3 (PositionRouter): 同 code 允许多 sub, 通过外部账本净额化下单

        若用户确实需要并行多 sub 共享同一 code, 可在 experiment yaml 顶层加
        `allow_leg_overlap: true` 来 bypass (P3 前不推荐)。
        """
        # 检查 ResolvedConfig 是否设了 allow 开关
        allow = False
        if hasattr(self._cfg_mgr, 'experiment_meta'):
            allow = self._cfg_mgr.experiment_meta.get('allow_leg_overlap', False)
        elif hasattr(self._cfg_mgr, '_strat_cfg'):
            # v8.1 ConfigManager 兼容: 读 raw dict 顶层
            allow = self._cfg_mgr._strat_cfg.get('allow_leg_overlap', False)

        if allow:
            print("[leg_uniqueness] bypass enabled via experiment.allow_leg_overlap=true")
            return

        violations = []
        for code, subs in self._code_to_subs.items():
            if len(subs) > 1:
                sub_names = [s.name for s in subs]
                violations.append(f"  - {code}: shared by {sub_names}")

        if violations:
            raise ValueError(
                "P1-e leg uniqueness check failed — multiple subs operating same stdCode "
                "would corrupt positions under WT's code-level set_position semantics.\n"
                + "\n".join(violations) +
                "\n\nFix options:\n"
                "  1) Use distinct contract months for parallel subs (recommended)\n"
                "  2) Wait for P3 PositionRouter (账本化下单)\n"
                "  3) Set `allow_leg_overlap: true` in experiment yaml (UNSAFE, P1)"
            )

    def _maybe_apply_auto_profile(self, sub_cfg: dict):
        """v8.1: 若 sub 启用 auto_profile, 用历史 d1 价差 Hurst 自动判定 profile.

        触发条件 (任一即生效):
            - sub_cfg['auto_profile'] == True
            - 顶层 param_profile_auto.enable == True 且 sub 未声明 param_profile

        判定结果直接写回 sub_cfg['param_profile'], 后续 _resolve_sub_cfg 走原路径.
        若 sub_cfg 已有显式 param_profile, auto_profile 不覆盖 (显式优先).
        """
        # auto 关闭 或 已有显式 profile 则跳过
        sub_auto = sub_cfg.get('auto_profile', False)
        global_auto = self._auto_profile_cfg.get('enable', False)
        if not (sub_auto or global_auto):
            return
        if sub_cfg.get('param_profile') and not sub_auto:
            return  # 全局auto开启但sub已显式指定profile, 不覆盖

        # 提取阈值
        thresholds = self._auto_profile_cfg
        trend_th = thresholds.get('trend_hurst', 0.55)
        mr_th = thresholds.get('mr_hurst', 0.45)
        lookback = thresholds.get('lookback_days', 120)
        trend_r2_th = thresholds.get('trend_r2', 0.55)
        storage_dir = thresholds.get('storage_csv_dir', './storage/csv')

        # 用 SubStrategyConfig 解析 legs 拿到 ratio/code
        legs = SubStrategyConfig.from_yaml(sub_cfg).legs

        from auto_profile import auto_select_for_legs
        profile, hurst, info = auto_select_for_legs(
            legs, storage_dir,
            trend_threshold=trend_th, mr_threshold=mr_th, lookback=lookback,
            trend_r2_threshold=trend_r2_th)

        sub_name = sub_cfg.get('name')
        if profile is None:
            print(f"[auto_profile] {sub_name}: {info} → baseline (no profile)")
        else:
            print(f"[auto_profile] {sub_name}: {info} → '{profile}'")
            sub_cfg['param_profile'] = profile

    @property
    def sub_strategies(self) -> list:
        """获取子策略列表(只读)"""
        return self._subs

    def _resolve_sub_cfg(self, sub_cfg: dict):
        """v7.4: 解析子策略级参数覆盖

        优先级(从低到高):
        1. 全局 signal/risk_control 配置（基线）
        2. param_profile 引用（顶层 param_profiles 字典中查找）
        3. strategy 内联 signal_overrides / risk_overrides（最高优先级）

        返回: (SignalConfig, RiskConfig) — 已应用覆盖的新实例
        """
        signal_cfg = self._signal_cfg
        risk_cfg = self._risk_cfg

        # 收集 overrides (按优先级累积)
        sig_over = {}
        risk_over = {}

        # 1) param_profile 引用
        profile_name = sub_cfg.get('param_profile')
        if profile_name:
            profile = self._param_profiles.get(profile_name)
            if profile is None:
                raise ValueError(
                    f"strategy '{sub_cfg.get('name')}' 引用了未定义的 param_profile='{profile_name}'。"
                    f"可用 profile: {list(self._param_profiles.keys())}")
            sig_over.update(profile.get('signal', {}) or {})
            risk_over.update(profile.get('risk_control', {}) or {})

        # 2) 内联 overrides（最高优先级）
        sig_over.update(sub_cfg.get('signal_overrides', {}) or {})
        risk_over.update(sub_cfg.get('risk_overrides', {}) or {})

        # 应用到 dataclass(仅在有变更时复制实例)
        if sig_over:
            # 过滤未知字段，避免静默失效
            valid_fields = set(signal_cfg.__dataclass_fields__.keys())
            unknown = set(sig_over) - valid_fields
            if unknown:
                raise ValueError(
                    f"strategy '{sub_cfg.get('name')}' signal_overrides 包含未知字段: {unknown}")
            signal_cfg = _dc_replace(signal_cfg, **sig_over)

        if risk_over:
            valid_fields = set(risk_cfg.__dataclass_fields__.keys())
            unknown = set(risk_over) - valid_fields
            if unknown:
                raise ValueError(
                    f"strategy '{sub_cfg.get('name')}' risk_overrides 包含未知字段: {unknown}")
            risk_cfg = _dc_replace(risk_cfg, **risk_over)

        # 诊断输出
        if sig_over or risk_over:
            print(f"[param_profile] {sub_cfg.get('name')}: "
                  f"profile={profile_name or '-'}, "
                  f"signal_overrides={sig_over}, risk_overrides={risk_over}")

        return signal_cfg, risk_cfg

    def on_init(self, context: CtaContext):
        # v8.1: 多子策略并行支持 — 仅首个 sub 持有 main_key (CtaMocker.cpp:1784-1792)
        # 所有 sub 必须共用同一 main_period，否则 WT 引擎调度节拍不一致
        if len(self._subs) >= 2:
            ref_period = self._subs[0].main_period
            for sub in self._subs[1:]:
                if sub.main_period != ref_period:
                    raise ValueError(
                        f"多子策略并行要求所有 sub 共用同一 main_period; "
                        f"'{self._subs[0].name}' 用 {ref_period}, "
                        f"'{sub.name}' 用 {sub.main_period}")

        for i, sub in enumerate(self._subs):
            sub.on_init(context, is_main_holder=(i == 0))

        context.stra_log_text(
            f"SpreadArbStrategy inited with {len(self._subs)} sub(s); "
            f"main_holder={self._subs[0].name if self._subs else '-'}")

    def on_calculate(self, context: CtaContext):
        self._cur_date = context.stra_get_date()
        cur_time = context.stra_get_time()
        context.stra_log_text(
            f"on_calculate triggered: date={self._cur_date}, time={cur_time}")
        for sub in self._subs:
            sub.process(context, self._cur_date)

    def on_tick(self, context: CtaContext, stdCode: str, newTick: dict):
        # P3-2修复: on_tick不再调用on_tick_check
        # 原问题: on_tick逐笔回调中推进状态机，与process()中的on_tick_check
        #   形成两条推进路径，可能导致重复检测/状态冲突。
        # 修复: 状态机推进统一在process()开头执行，on_tick只做tick级数据缓存。
        # ArbExecHelper的挂单成交检测改为在process()中统一处理。
        pass

    def on_bar(self, context: CtaContext, stdCode: str, period: str, newBar: dict):
        pass

    def on_session_begin(self, context: CtaContext, curTDate: int):
        for sub in self._subs:
            sub.on_session_begin(context)

    def on_session_end(self, context: CtaContext, curTDate: int):
        for sub in self._subs:
            sub.on_session_end(context)

    def on_backtest_end(self, context: CtaContext):
        """回测结束时暴露/尝试清理未平仓，并flush所有DiagLogger缓冲区。

        WT回测的最后一天不一定触发session_end；如果此时仍有持仓，
        先发出目标仓位归零指令并记录高优先级日志，再flush诊断日志。
        """
        for sub in self._subs:
            open_positions = {}
            for leg in sub.legs:
                pos = context.stra_get_position(leg.code)
                if pos != 0:
                    open_positions[leg.code] = pos

            if open_positions:
                context.stra_log_text(
                    f"{sub.name} backtest end open positions: {open_positions}; forcing close_all",
                    level=3)
                sub.eh.close_all(context, sub._config, tag="BacktestEndClose")

            sub._diag.flush_all()
        context.stra_log_text("SpreadArbStrategy backtest ended, logs flushed")

    def register_risk_rule(self, name: str, rule_fn, sub_name: str = None):
        """注册自定义风控规则"""
        for sub in self._subs:
            if sub_name is None or sub.name == sub_name:
                sub.rm.register_rule(name, rule_fn)