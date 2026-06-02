"""
SpreadArbStrategy — CTA跨品种套利策略

v10架构: 去除多sub/PositionRouter概念。
- 单策略实例包含单个ArbCore(单个品种组合)
- 多品种组合用多策略实例(WT框架天然支持)
- 去除PositionRouter/PositionLedger/InstanceDecision等中间层
- 去除leg_uniqueness校验(单core不存在code冲突)
"""
from wtpy import BaseCtaStrategy
from wtpy import CtaContext

from ConfigManager import ConfigManager
from ArbCore import ArbCore
from schemas import ArbCoreConfig, SignalConfig, RiskConfig, RegimeConfig, ExecConfig
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

        # 结构化配置
        self._signal_cfg = self._cfg_mgr.signal_cfg
        self._risk_cfg = self._cfg_mgr.risk_cfg
        self._regime_cfg = self._cfg_mgr.regime_cfg
        self._exec_cfg = self._cfg_mgr.exec_cfg

        # v7.4: param_profile支持
        with open(strategy_config_path, encoding='utf-8') as f:
            _raw_cfg = _yaml.safe_load(f)
        self._param_profiles = _raw_cfg.get('param_profiles', {}) or {}
        self._auto_profile_cfg = _raw_cfg.get('param_profile_auto', {}) or {}

        # 取第一个(也是唯一的)策略配置
        sub_cfgs = self._cfg_mgr.strategies
        if len(sub_cfgs) != 1:
            raise ValueError(
                f"v10单pipeline模式要求恰好1个策略配置, 当前{len(sub_cfgs)}个。"
                f"多品种组合请用多策略实例。")
        sub_cfg = sub_cfgs[0]
        self._maybe_apply_auto_profile(sub_cfg)
        core_config = ArbCoreConfig.from_yaml(sub_cfg)
        core_signal_cfg, core_risk_cfg = self._resolve_sub_cfg(sub_cfg)

        self._core = ArbCore(
            core_config=core_config,
            risk_cfg=core_risk_cfg,
            signal_cfg=core_signal_cfg,
            adaptive_cfg=self._regime_cfg,
            factor_config_path=factor_config_path,
            exec_cfg=self._exec_cfg,
            is_backtest=is_backtest,
        )

    @classmethod
    def from_experiment(cls, name: str, experiment: str = 'default',
                        config_root: str = './config',
                        is_backtest: bool = False):
        """v9 工厂方法: 从分层配置创建策略实例"""
        from ConfigResolver import ConfigResolver
        rc = ConfigResolver(config_root).resolve(experiment)

        obj = cls.__new__(cls)
        BaseCtaStrategy.__init__(obj, name)

        obj._is_backtest = is_backtest
        obj._cfg_mgr = rc  # ResolvedConfig 鸭子兼容 ConfigManager
        import os as _os
        obj._factor_config_path = _os.path.join(
            _os.path.abspath(config_root), 'base', 'factors.yaml')
        obj._cur_date = 0

        obj._signal_cfg = rc.signal_cfg
        obj._risk_cfg = rc.risk_cfg
        obj._regime_cfg = rc.regime_cfg
        obj._exec_cfg = rc.exec_cfg

        obj._param_profiles = rc.param_profiles
        obj._auto_profile_cfg = rc.auto_profile_cfg

        sub_cfgs = rc.strategies
        if len(sub_cfgs) != 1:
            raise ValueError(
                f"v10单pipeline模式要求恰好1个策略配置, 当前{len(sub_cfgs)}个。"
                f"多品种组合请用多策略实例。")
        sub_cfg = sub_cfgs[0]
        obj._maybe_apply_auto_profile(sub_cfg)
        core_config = ArbCoreConfig.from_yaml(sub_cfg)
        core_signal_cfg, core_risk_cfg = obj._resolve_sub_cfg(sub_cfg)

        obj._core = ArbCore(
            core_config=core_config,
            risk_cfg=core_risk_cfg,
            signal_cfg=core_signal_cfg,
            adaptive_cfg=obj._regime_cfg,
            factor_config_path=obj._factor_config_path,
            exec_cfg=obj._exec_cfg,
            is_backtest=is_backtest,
        )
        return obj

    @property
    def core(self) -> ArbCore:
        """获取核心流水线(只读)"""
        return self._core

    # 保留向后兼容属性
    @property
    def sub_strategies(self) -> list:
        """向后兼容: 返回 [core] 列表"""
        return [self._core]

    def _maybe_apply_auto_profile(self, sub_cfg: dict):
        """若 sub 启用 auto_profile, 用历史 d1 价差 Hurst 自动判定 profile"""
        sub_auto = sub_cfg.get('auto_profile', False)
        global_auto = self._auto_profile_cfg.get('enable', False)
        if not (sub_auto or global_auto):
            return
        if sub_cfg.get('param_profile') and not sub_auto:
            return

        thresholds = self._auto_profile_cfg
        trend_th = thresholds.get('trend_hurst', 0.55)
        mr_th = thresholds.get('mr_hurst', 0.45)
        lookback = thresholds.get('lookback_days', 120)
        trend_r2_th = thresholds.get('trend_r2', 0.55)
        storage_dir = thresholds.get('storage_csv_dir', './storage/csv')

        legs = ArbCoreConfig.from_yaml(sub_cfg).legs

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

    def _resolve_sub_cfg(self, sub_cfg: dict):
        """解析参数覆盖

        优先级(从低到高):
        1. 全局 signal/risk_control 配置（基线）
        2. param_profile 引用（顶层 param_profiles 字典中查找）
        3. strategy 内联 signal_overrides / risk_overrides（最高优先级）
        """
        signal_cfg = self._signal_cfg
        risk_cfg = self._risk_cfg

        sig_over = {}
        risk_over = {}

        profile_name = sub_cfg.get('param_profile')
        if profile_name:
            profile = self._param_profiles.get(profile_name)
            if profile is None:
                raise ValueError(
                    f"strategy '{sub_cfg.get('name')}' 引用了未定义的 param_profile='{profile_name}'。"
                    f"可用 profile: {list(self._param_profiles.keys())}")
            sig_over.update(profile.get('signal', {}) or {})
            risk_over.update(profile.get('risk_control', {}) or {})

        sig_over.update(sub_cfg.get('signal_overrides', {}) or {})
        risk_over.update(sub_cfg.get('risk_overrides', {}) or {})

        if sig_over:
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

        if sig_over or risk_over:
            print(f"[param_profile] {sub_cfg.get('name')}: "
                  f"profile={profile_name or '-'}, "
                  f"signal_overrides={sig_over}, risk_overrides={risk_over}")

        return signal_cfg, risk_cfg

    def on_init(self, context: CtaContext):
        self._core.on_init(context, is_main_holder=True)
        context.stra_log_text(
            f"SpreadArbStrategy inited with core={self._core.name}")

    def on_calculate(self, context: CtaContext):
        self._cur_date = context.stra_get_date()
        cur_time = context.stra_get_time()
        context.stra_log_text(
            f"on_calculate triggered: date={self._cur_date}, time={cur_time}")
        self._core.process(context, self._cur_date)

    def on_tick(self, context: CtaContext, stdCode: str, newTick: dict):
        pass

    def on_bar(self, context: CtaContext, stdCode: str, period: str, newBar: dict):
        pass

    def on_session_begin(self, context: CtaContext, curTDate: int):
        self._core.on_session_begin(context)

    def on_session_end(self, context: CtaContext, curTDate: int):
        self._core.on_session_end(context)

    def on_backtest_end(self, context: CtaContext):
        """回测结束时暴露/清理未平仓，flush DiagLogger缓冲区"""
        core = self._core
        open_positions = {}
        for leg in core.legs:
            pos = context.stra_get_position(leg.code)
            if pos != 0:
                open_positions[leg.code] = pos

        if open_positions:
            context.stra_log_text(
                f"{core.name} backtest end open positions: {open_positions}; forcing close_all",
                level=3)
            core.eh.close_all(context, core._config, tag="BEC")

        core._diag.flush_all()
        context.stra_log_text("SpreadArbStrategy backtest ended, logs flushed")

    def register_risk_rule(self, name: str, rule_fn):
        """注册自定义风控规则"""
        self._core.rm.register_rule(name, rule_fn)
