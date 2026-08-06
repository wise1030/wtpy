"""
SpreadArbStrategy — CTA跨品种套利策略

v10架构: 去除多sub/PositionRouter概念。
- 单策略实例包含单个ArbCore(单个品种组合)
- 多品种组合用多策略实例(WT框架天然支持)
- 去除PositionRouter/PositionLedger/InstanceDecision等中间层
- 去除leg_uniqueness校验(单core不存在code冲突)

P4M-2 (2026-06): __init__ 与 from_experiment 共用 _build_core() 私有构造器,
消除 80% 重复代码。两入口仅负责"准备 resolved_cfg",后续 sub_cfg 解析
和 ArbCore 构建走同一路径。

S1 (2026-06): factor_overrides 支持 — profile 可覆盖因子启停/权重/参数,
生成临时 yaml 供 FactorEngine 加载, 向后兼容(无 factor_overrides 时原路径)。
"""
import os as _os
import tempfile as _tempfile
from copy import deepcopy as _deepcopy
from dataclasses import replace as _dc_replace

import yaml as _yaml
from wtpy import BaseCtaStrategy
from wtpy import CtaContext

from ConfigManager import ConfigManager
from ArbCore import ArbCore
from schemas import ArbCoreConfig


class SpreadArbStrategy(BaseCtaStrategy):

    def __init__(self, name: str, strategy_config_path: str,
                 factor_config_path: str, is_backtest: bool = False):
        BaseCtaStrategy.__init__(self, name)

        cfg_mgr = ConfigManager(strategy_config_path)

        # v7.4: param_profile 仅在 legacy 入口需要从原 yaml 重新读取
        # (ConfigManager 未暴露 param_profiles/auto_profile_cfg)
        with open(strategy_config_path, encoding='utf-8') as f:
            _raw_cfg = _yaml.safe_load(f)
        param_profiles = _raw_cfg.get('param_profiles', {}) or {}
        auto_profile_cfg = _raw_cfg.get('param_profile_auto', {}) or {}

        self._build_core(
            resolved_cfg=cfg_mgr,
            factor_config_path=factor_config_path,
            param_profiles=param_profiles,
            auto_profile_cfg=auto_profile_cfg,
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

        factor_path = _os.path.join(
            _os.path.abspath(config_root), 'base', 'factors.yaml')

        obj._build_core(
            resolved_cfg=rc,
            factor_config_path=factor_path,
            param_profiles=rc.param_profiles,
            auto_profile_cfg=rc.auto_profile_cfg,
            is_backtest=is_backtest,
        )
        return obj

    # ───────────────────── private builder ─────────────────────

    def _build_core(self, resolved_cfg, factor_config_path: str,
                    param_profiles: dict, auto_profile_cfg: dict,
                    is_backtest: bool):
        """统一构造路径: 接收 ConfigManager 或 ResolvedConfig (鸭子类型),
        完成 attr 注入 + sub_cfg 解析 + ArbCore 实例化。
        """
        self._is_backtest = is_backtest
        self._cfg_mgr = resolved_cfg
        self._factor_config_path = factor_config_path
        self._cur_date = 0

        # 结构化配置 (两入口都暴露 .signal_cfg/.risk_cfg/.regime_cfg/.exec_cfg)
        self._signal_cfg = resolved_cfg.signal_cfg
        self._risk_cfg = resolved_cfg.risk_cfg
        self._regime_cfg = resolved_cfg.regime_cfg
        self._exec_cfg = resolved_cfg.exec_cfg

        self._param_profiles = param_profiles
        self._auto_profile_cfg = auto_profile_cfg

        sub_cfgs = resolved_cfg.strategies
        if len(sub_cfgs) != 1:
            raise ValueError(
                f"v10单pipeline模式要求恰好1个策略配置, 当前{len(sub_cfgs)}个。"
                f"多品种组合请用多策略实例。")
        sub_cfg = sub_cfgs[0]
        self._maybe_apply_auto_profile(sub_cfg)
        core_config = ArbCoreConfig.from_yaml(sub_cfg)
        core_signal_cfg, core_risk_cfg, effective_factor_path = self._resolve_sub_cfg(
            sub_cfg, factor_config_path)

        self._core = ArbCore(
            core_config=core_config,
            risk_cfg=core_risk_cfg,
            signal_cfg=core_signal_cfg,
            adaptive_cfg=self._regime_cfg,
            factor_config_path=effective_factor_path,
            exec_cfg=self._exec_cfg,
            is_backtest=is_backtest,
            param_profile=sub_cfg.get('param_profile'),
            # A-5(2026-07): auto_profile 配置透传, 修复实盘重算路径死分支
            auto_profile_cfg=auto_profile_cfg,
        )

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

    def _resolve_sub_cfg(self, sub_cfg: dict, base_factor_path: str):
        """解析参数覆盖

        优先级(从低到高):
        1. 全局 signal/risk_control 配置（基线）
        2. param_profile 引用（顶层 param_profiles 字典中查找）
        3. strategy 内联 signal_overrides / risk_overrides（最高优先级）

        S1 新增: factor_overrides 合并 — profile 或 inline 可覆盖因子权重/启停/参数。
        """
        signal_cfg = self._signal_cfg
        risk_cfg = self._risk_cfg

        sig_over = {}
        risk_over = {}
        factor_over = []

        profile_name = sub_cfg.get('param_profile')
        if profile_name:
            profile = self._param_profiles.get(profile_name)
            if profile is None:
                raise ValueError(
                    f"strategy '{sub_cfg.get('name')}' 引用了未定义的 param_profile='{profile_name}'。"
                    f"可用 profile: {list(self._param_profiles.keys())}")
            sig_over.update(profile.get('signal', {}) or {})
            risk_over.update(profile.get('risk_control', {}) or {})
            # S1: 采集 profile 中的 factor_overrides
            pf_factors = profile.get('factors', []) or []
            factor_over.extend(pf_factors)

        sig_over.update(sub_cfg.get('signal_overrides', {}) or {})
        risk_over.update(sub_cfg.get('risk_overrides', {}) or {})
        # S1: 采集 inline factor_overrides (最高优先级, 后到覆盖)
        factor_over.extend(sub_cfg.get('factor_overrides', []) or [])

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

        # S1: 应用 factor_overrides → 生成临时 factor yaml
        effective_factor_path = base_factor_path
        if factor_over:
            effective_factor_path = self._apply_factor_overrides(
                base_factor_path, factor_over, sub_cfg.get('name', '?'))

        if sig_over or risk_over or factor_over:
            print(f"[param_profile] {sub_cfg.get('name')}: "
                  f"profile={profile_name or '-'}, "
                  f"signal_overrides={sig_over}, risk_overrides={risk_over}, "
                  f"factor_overrides={len(factor_over)} items")

        return signal_cfg, risk_cfg, effective_factor_path

    def _apply_factor_overrides(self, base_path: str, overrides: list,
                                sub_name: str) -> str:
        """将 factor_overrides 合并到 base factors.yaml, 输出临时文件

        合并逻辑: 以 override 的 name 匹配 base 中的因子:
        - 已存在: 更新 weight / enabled / params (按 key 合并)
        - 不存在: 追加到 factors 列表 (来自关停因子池)

        Returns: 临时 factor yaml 路径 (由 FactorEngine 加载)
        """
        with open(base_path, 'r', encoding='utf-8') as f:
            base_cfg = _yaml.safe_load(f)

        factors = base_cfg.get('factors', [])
        # 建立 name → index 映射 (包括注释/关停因子 — 它们不在 factors 列表中)
        name_idx = {}
        for i, f in enumerate(factors):
            fname = f.get('name')
            if fname:
                name_idx[fname] = i

        # 也扫描关停因子: 从原始 yaml 中已注释的因子无法被 yaml parser 解析,
        # 所以只处理 factors 列表中 enabled=false 的项
        # (新启用的因子需在 factors 列表中存在, 只是 enabled=false)

        for ov in overrides:
            ov_name = ov.get('name')
            if not ov_name:
                continue

            if ov_name in name_idx:
                # 合并到已有因子
                idx = name_idx[ov_name]
                existing = factors[idx]
                if 'weight' in ov:
                    existing['weight'] = ov['weight']
                if 'enabled' in ov:
                    existing['enabled'] = ov['enabled']
                if 'params' in ov:
                    # 按 key 合并 params, 不删除未覆盖的参数
                    if 'params' not in existing or existing['params'] is None:
                        existing['params'] = {}
                    existing['params'].update(ov['params'])
            else:
                # 不在 factors 列表中 → 追加 (从关停池启用)
                # 需要完整定义: class / name / enabled / weight / params
                new_factor = {
                    'class': ov.get('class', f'{ov_name.replace("_", " ").title().replace(" ", "")}Factor'),
                    'enabled': ov.get('enabled', True),
                    'name': ov_name,
                    'params': ov.get('params', {}),
                    'weight': ov.get('weight', 0),
                }
                factors.append(new_factor)
                name_idx[ov_name] = len(factors) - 1

        base_cfg['factors'] = factors

        # 写临时文件 (不自动删除 — debug 需要; 回测结束进程退出时自动清理)
        fd, tmp_path = _tempfile.mkstemp(
            suffix='.yaml', prefix=f'factors_{sub_name}_')
        with _os.fdopen(fd, 'w', encoding='utf-8') as f:
            _yaml.dump(base_cfg, f, allow_unicode=True, default_flow_style=False)

        print(f"[factor_overrides] {sub_name}: wrote merged factor config → {tmp_path}")
        return tmp_path

    def on_init(self, context: CtaContext):
        # 框架一致性校验: 自管理 is_backtest 必须与 engine.is_backtest 一致,
        # 否则 on_tick/write_indicator/recheck_profile 的门控会走错分支
        if context.is_backtest != self._is_backtest:
            raise RuntimeError(
                f"is_backtest mismatch: strategy={self._is_backtest} "
                f"engine={context.is_backtest}. 检查 runBT/runLive 构造参数")
        self._core.on_init(context, is_main_holder=True)
        context.stra_log_text(
            f"SpreadArbStrategy inited with core={self._core.name}")

    def on_calculate(self, context: CtaContext):
        # P-6(2026-07): 删除每bar的 stra_log_text(上万bar=上万行噪声日志)
        self._cur_date = context.stra_get_date()
        self._core.process(context, self._cur_date)

    def on_tick(self, context: CtaContext, stdCode: str, newTick: dict):
        # A-2/A-3(2026-07): 实盘恢复 tick 级状态机推进(回测内部自动跳过)
        self._core.on_tick(context)

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
