"""turtle_config.yaml → TurtleConfig 加载与校验"""
import dataclasses
import sys

import yaml

from schemas import (TurtleConfig, TurtleBaseConfig, InstrumentConfig,
                     RiskConfig, FilterConfig, NotifyConfig, SpeedConfig,
                     ProfileConfig,
                     VALID_DIRECTIONS, VALID_SYSTEMS, VALID_PERIODS,
                     VALID_SUB_PERIODS)
from filters.base import get_filter_class
from core.profiles import required_bars, describe_profiles, resolve_profiles

# 顶层段与 instruments[] 项的合法键 (P1-6 加固: 此前拼错的键如
# untis/drection 会被静默忽略回默认值, 与 base/risk/notify 的不对称
# 防护是配置面最危险的洞)
_TOP_KEYS = {"base", "risk", "notify", "instruments", "config_version"}
_INST_KEYS = {"code", "period", "system", "units", "direction", "group",
              "filters", "intrabar_stop", "speeds", "profiles"}


def allocated_capital(risk, code: str, codes: list) -> float:
    """资金分配启用时 (risk.portfolio_capital>0) 该 code 的权益基数 (元);
    未启用返回 fallback_capital (2026-10-22 S1)。"""
    if float(risk.portfolio_capital) <= 0:
        return float(risk.fallback_capital)
    w = risk.alloc_weights or {}
    tot = sum(float(w.get(c, 1.0)) for c in codes)
    if tot <= 0:
        return float(risk.fallback_capital)
    return float(risk.portfolio_capital) * float(w.get(code, 1.0)) / tot


def register_allocated_capital(risk_mgr, cfg) -> dict:
    """runLive/deploy 共用的 sid 资金分配注册；返回 {code: (sid, 金额)}。

    portfolio_capital=0 时仅返回空表，不改变旧 fallback 行为。集中此
    函数是为了防止生产模板漏注册而静默回退到每策略 fallback_capital。
    """
    out = {}
    if cfg.risk.portfolio_capital <= 0:
        return out
    codes = [i.code for i in cfg.instruments]
    for inst in cfg.instruments:
        name = f"turtle_{inst.code.replace('.', '_')}"
        alloc = allocated_capital(cfg.risk, inst.code, codes)
        risk_mgr.set_sid_capital(name, alloc)
        out[inst.code] = (name, alloc)
    return out


def risk_coherence_warning(base, risk, capital: float = None) -> str:
    """单位风险 vs 单笔风险上限的一致性提示 (2026-10-20)。

    capital: 有效权益基数 (资金分配启用时应传 allocated_capital,
    否则按 fallback_capital 评估会与实际闸门不一致)。"""
    if capital is not None:
        risk = dataclasses.replace(risk, fallback_capital=float(capital))
    unit_2n = float(base.unit_var) * float(base.stop_atr_mult)
    order_cap = float(risk.fallback_capital) * float(risk.max_risk_per_mkt)
    if unit_2n > order_cap > 0:
        return (
            f"[ConfigLoader] WARN: unit_var×stop_atr_mult="
            f"{unit_2n:,.0f} > fallback_capital×max_risk_per_mkt="
            f"{order_cap:,.0f} → 每笔最多只能下 "
            f"{order_cap / unit_2n:.1%} 个名义单位 "
            f"(建议 unit_var={order_cap / float(base.stop_atr_mult):,.0f} "
            f"或 max_risk_per_mkt={unit_2n / float(risk.fallback_capital):.4f})")
    return ""


class ConfigLoader:
    @staticmethod
    def load(path: str, warn: bool = True) -> TurtleConfig:
        """加载配置; warn=False 时抑制风险口径 WARN (调用方自行覆盖后打印)。"""
        with open(path, encoding='utf-8') as f:
            raw = yaml.safe_load(f) or {}
        if not isinstance(raw, dict):
            raise ValueError("turtle_config.yaml 顶层需为映射")

        unknown_top = set(raw) - _TOP_KEYS
        if unknown_top:
            raise ValueError(
                f"turtle_config.yaml 含未知顶层段: {sorted(unknown_top)} "
                f"(合法: {sorted(_TOP_KEYS)})")
        ver = raw.get('config_version', 1)
        if not isinstance(ver, int) or ver < 1:
            raise ValueError(f"config_version 需为正整数, 当前 {ver!r}")
        if ver > 1:
            print(f"[ConfigLoader] WARN: config_version={ver} > 当前支持(1), "
                  f"请确认代码已升级, 未知字段将报错", file=sys.stderr)

        base_raw = raw.get('base', {}) or {}
        known_base = {k for k in TurtleBaseConfig.__dataclass_fields__}
        unknown = set(base_raw) - known_base
        if unknown:
            raise ValueError(f"base 配置含未知字段: {unknown}")
        base = TurtleBaseConfig(**base_raw)

        if base.bar_cnt < base.s2_entry + base.atr_period:
            raise ValueError(
                f"bar_cnt({base.bar_cnt}) 过小: 需 >= s2_entry + atr_period "
                f"({base.s2_entry + base.atr_period})")
        if not isinstance(base.enable_s1_filter, bool):
            raise ValueError("base.enable_s1_filter 需为 bool")
        if int(base.recon_patience) < 1:
            raise ValueError("base.recon_patience 需 >= 1")
        if base.intrabar_period not in VALID_SUB_PERIODS:
            raise ValueError(
                f"base.intrabar_period 非法 '{base.intrabar_period}', "
                f"仅支持 {VALID_SUB_PERIODS}")
        if int(base.intrabar_cnt) < 60:
            raise ValueError("base.intrabar_cnt 需 >= 60")
        if base.entry_mode not in ("close", "trigger"):
            raise ValueError(
                f"base.entry_mode 非法 '{base.entry_mode}', "
                "仅支持 close|trigger")
        if base.stop_n_mode not in ("entry", "current", "ratchet"):
            raise ValueError(
                f"base.stop_n_mode 非法 '{base.stop_n_mode}', "
                "仅支持 entry(冻结入场 N)|current(当根 N)|"
                "ratchet(当根 N 但只收紧不放宽)")
        if not isinstance(base.truth_json, str):
            raise ValueError("base.truth_json 需为字符串路径 (或 '' / off)")
        if float(base.truth_max_age) <= 0:
            raise ValueError("base.truth_max_age 需为正数(秒)")
        if base.truth_rebuild_mode not in ("alert", "rebuild"):
            raise ValueError(
                f"base.truth_rebuild_mode 非法 '{base.truth_rebuild_mode}', "
                "仅支持 alert(只观测/告警)|rebuild(允许按真值重建)")
        if not isinstance(base.ghost_auto_trim, bool):
            raise ValueError("base.ghost_auto_trim 需为 bool")
        if int(base.limit_lock_secs) < 0:
            raise ValueError("base.limit_lock_secs 不能为负 (0=关闭)")
        # S4 信号口径开关 (2026-10-24): 均为 bool
        for name in ("trigger_envelope", "allow_same_bar_reversal",
                     "gap_stop", "arm_exclude_signal"):
            if not isinstance(getattr(base, name), bool):
                raise ValueError(f"base.{name} 需为 bool")

        risk_raw = raw.get('risk', {}) or {}
        known_risk = {k for k in RiskConfig.__dataclass_fields__}
        unknown = set(risk_raw) - known_risk
        if unknown:
            raise ValueError(f"risk 配置含未知字段: {unknown}")
        risk = RiskConfig(**risk_raw)
        risk.validate()
        # 单位风险 vs 单笔风险上限一致性 (2026-10-20):
        # 1 个名义单位 2N 风险 = unit_var * stop_atr_mult;
        # 单笔上限 = fallback_capital * max_risk_per_mkt。
        # 前者更大时 cap_entry 会把每一笔都缩到 <1 单位 (当前配置
        # 400k vs 300k → 75%), 文档里的 "1 unit = unit_var" 不成立。
        # 这里只 WARN 不阻断, 避免旧配置无法启动; 新部署应调整
        # unit_var 或 max_risk_per_mkt 使比例=1。
        msg = risk_coherence_warning(base, risk)
        if warn and msg:
            print(msg, file=sys.stderr)
        if risk.portfolio_capital > 0:
            print(f"[ConfigLoader] 资金分配启用: portfolio_capital="
                  f"{risk.portfolio_capital:,.0f}", file=sys.stderr)

        notify_raw = raw.get('notify', {}) or {}
        known_notify = {k for k in NotifyConfig.__dataclass_fields__}
        unknown = set(notify_raw) - known_notify
        if unknown:
            raise ValueError(f"notify 配置含未知字段: {unknown}")
        notify = NotifyConfig(**notify_raw)
        if not isinstance(notify.enabled, bool):
            raise ValueError("notify.enabled 需为 bool")
        if notify.type not in ("wechat", "dingtalk", "generic"):
            raise ValueError(f"notify.type 非法 '{notify.type}'")
        if not isinstance(notify.urls, list) or \
                any(not isinstance(u, str) or not u.strip()
                    for u in notify.urls):
            raise ValueError("notify.urls 需为非空字符串 URL 列表")
        if notify.enabled and not notify.urls:
            raise ValueError("notify.enabled=true 时 urls 不能为空")

        instruments = []
        seen_codes = set()
        for i, item in enumerate(raw.get('instruments', []) or []):
            code = item.get('code')
            if not code:
                raise ValueError(f"instruments[{i}] 缺 code")
            if code in seen_codes:
                raise ValueError(f"instruments[{i}] code 重复: {code}")
            seen_codes.add(code)
            unknown_inst = set(item) - _INST_KEYS
            if unknown_inst:
                raise ValueError(
                    f"{code}: instruments 项含未知字段 "
                    f"{sorted(unknown_inst)} (合法: {sorted(_INST_KEYS)})")
            period = item.get('period', 'd1')
            system = item.get('system', 'S1')
            direction = item.get('direction', 'both')
            if period not in VALID_PERIODS:
                raise ValueError(f"{code}: period 非法 '{period}', 仅支持 {VALID_PERIODS}")
            # h1 一期 (2026-10-21): 策略层 m5→h1 聚合 (core/aggregate),
            # 主K=m5; 数据前提=m5 存在, 由 runBT 预检/runLive 暖机兜底
            if False:
                pass
            if system not in VALID_SYSTEMS:
                raise ValueError(f"{code}: system 非法 '{system}'")
            if direction not in VALID_DIRECTIONS:
                raise ValueError(f"{code}: direction 非法 '{direction}'")
            group = str(item.get('group', '') or '')
            units = item.get('units', 1)
            if not isinstance(units, int) or isinstance(units, bool) \
                    or units < 1:
                raise ValueError(
                    f"{code}: units 需为正整数, 当前 {units!r} "
                    f"(1.5 之类的值会被静默截断, 故拒绝)")
            intrabar_stop = item.get('intrabar_stop', False)
            if not isinstance(intrabar_stop, bool):
                raise ValueError(f"{code}: intrabar_stop 需为 bool")
            filters = ConfigLoader._parse_filters(code, item.get('filters'))
            speeds = ConfigLoader._parse_speeds(
                code, item.get('speeds'), default_fs=base.s2_entry)
            profiles = ConfigLoader._parse_profiles(
                code, item.get('profiles'))
            if profiles and (speeds or 'system' in item):
                raise ValueError(
                    f"{code}: profiles 与 system/speeds 互斥 —— profiles "
                    f"非空时是唯一入口, 禁止再叠加旧键造成优先级歧义")
            deprecated = []
            if speeds:
                deprecated.append("speeds")
            if not profiles and 'system' in item:
                deprecated.append("system")
            if deprecated:
                print(f"[ConfigLoader] WARN {code}: "
                      f"{'/'.join(deprecated)} 已弃用, 请迁移到 profiles "
                      f"(docs/PROFILE_CONFIG_UNIFICATION.md)")
            instruments.append(InstrumentConfig(
                code=code, period=period, system=system,
                units=units, direction=direction,
                group=group, filters=filters,
                speeds=speeds, profiles=profiles,
                intrabar_stop=intrabar_stop))

        if not instruments:
            raise ValueError("instruments 为空")
        # 暖机深度必须按**实际 profiles**校验：此前只看 base 窗口，
        # 120/60、250/125 等 speed 会在 bar_cnt=120 下永久 NaN 空转。
        for inst in instruments:
            need = required_bars(inst, base)
            if base.bar_cnt < need:
                raise ValueError(
                    f"{inst.code}: bar_cnt({base.bar_cnt}) 小于实际 profiles "
                    f"所需 {need} 根 (含全部 entry/exit/fs 与 prev 余量); "
                    f"请增大 bar_cnt 或缩短窗口")
            if inst.profiles or inst.speeds or inst.system == "S1+S2":
                print(f"[ConfigLoader] {inst.code} profiles: "
                      f"{describe_profiles(resolve_profiles(inst, base))}")
        if risk.alloc_weights:
            codes = {i.code for i in instruments}
            unknown_w = set(risk.alloc_weights) - codes
            if unknown_w:
                raise ValueError(
                    f"risk.alloc_weights 含未知 code: {sorted(unknown_w)} "
                    f"(不在 instruments 中)")
        cfg = TurtleConfig(base=base, instruments=instruments, risk=risk,
                           notify=notify)
        # 敏感行为口径显式打印 (P1-6): 默认值变更会静默改变风险行为
        # (2026-09-15 stop_n_mode current→ratchet 先例), 加载时留痕
        print(f"[ConfigLoader] {path}: instruments={len(instruments)} "
              f"entry_mode={base.entry_mode} stop_n_mode={base.stop_n_mode} "
              f"ghost_auto_trim={base.ghost_auto_trim} "
              f"truth_mode={base.truth_rebuild_mode} "
              f"gate={risk.account_gate_mode} "
              f"notify={'on' if notify.enabled else 'OFF'}")
        return cfg

    @staticmethod
    def _parse_profiles(code: str, raw_list) -> list:
        """解析 instruments[].profiles (统一入口, 2026-10-30)。

        preset 是窗口+filter 的预设简写 (S1/S2), 显式字段覆盖预设缺省;
        语义校验集中在 core.profiles._expand_profiles (单一口径,
        直测 dataclass 实例化同样 fail-loud)。这里只做 YAML 形状/类型
        校验: 未知键拒绝、整数窗口、share/filter 类型。
        """
        if raw_list is None:
            return []
        if not isinstance(raw_list, list) or not raw_list:
            raise ValueError(f"{code}: profiles 需为非空列表")
        allowed = {"name", "preset", "entry", "exit", "fs",
                   "filter", "share"}
        out = []
        for j, sp in enumerate(raw_list):
            if not isinstance(sp, dict):
                raise ValueError(f"{code}: profiles[{j}] 需为映射")
            unknown = set(sp) - allowed
            if unknown:
                raise ValueError(
                    f"{code}: profiles[{j}] 含未知字段 {sorted(unknown)} "
                    f"(合法: {sorted(allowed)})")
            for field in ("entry", "exit", "fs"):
                value = sp.get(field)
                if value is not None and (
                        not isinstance(value, int) or isinstance(value, bool)):
                    raise ValueError(
                        f"{code}: profiles[{j}].{field} 需为整数，"
                        "禁止浮点/字符串静默截断")
            share = sp.get("share")
            if share is not None:
                share = float(share)
                if not 0 < share <= 1:
                    raise ValueError(f"{code}: profiles[{j}].share 需在 (0,1]")
            flt = sp.get("filter")
            if flt is not None and not isinstance(flt, bool):
                raise ValueError(f"{code}: profiles[{j}].filter 需为 bool")
            out.append(ProfileConfig(
                name=str(sp.get("name") or ""),
                preset=str(sp.get("preset") or ""),
                entry=sp.get("entry"), exit=sp.get("exit"),
                fs=sp.get("fs"), filter=flt, share=share))
        return out

    @staticmethod
    def _parse_speeds(code: str, raw_list, default_fs: int = 55) -> list:
        """解析 instruments[].speeds (多速度); 份额之和须为 1。

        多档必须显式 name/system：name 是快照身份，system 决定 S1/S2
        语义；仅靠 55/20 或 name=S2 不能推断语义。fs 缺省跟随
        base.s2_entry，而不是写死 55。
        """
        if raw_list is None:
            return []
        if not isinstance(raw_list, list) or not raw_list:
            raise ValueError(f"{code}: speeds 需为非空列表")
        allowed = {"name", "entry", "exit", "share", "fs", "system"}
        out, names, total = [], set(), 0.0
        for j, sp in enumerate(raw_list):
            if not isinstance(sp, dict):
                raise ValueError(f"{code}: speeds[{j}] 需为映射")
            unknown = set(sp) - allowed
            if unknown:
                raise ValueError(
                    f"{code}: speeds[{j}] 含未知字段 {sorted(unknown)} "
                    f"(合法: {sorted(allowed)})")
            name = str(sp.get("name") or f"V{j}")
            if name in names:
                raise ValueError(f"{code}: speeds 名称重复 {name}")
            names.add(name)
            for field in ("entry", "exit", "fs"):
                value = sp.get(field, default_fs if field == "fs" else None)
                if not isinstance(value, int) or isinstance(value, bool):
                    raise ValueError(
                        f"{code}: speeds[{j}].{field} 需为整数，"
                        "禁止浮点/字符串静默截断")
            entry, exit_ = sp["entry"], sp["exit"]
            if entry < 2 or exit_ < 2:
                raise ValueError(f"{code}: speeds[{j}] entry/exit 需 >= 2")
            share = float(sp.get("share", 1.0 / len(raw_list)))
            if not 0 < share <= 1:
                raise ValueError(f"{code}: speeds[{j}] share 需在 (0,1]")
            fsp = sp.get("fs", default_fs)
            if fsp < 2:
                raise ValueError(f"{code}: speeds[{j}] fs 需 >= 2")
            # 引擎语义与展示名解耦 (2026-10-20): name 可为 fast/slow,
            # system 必须是 S1/S2 (TurtleEngine 的过滤器语义)。
            system = str(sp.get("system", "S1"))
            if system not in ("S1", "S2"):
                raise ValueError(
                    f"{code}: speeds[{j}].system 非法 '{system}', "
                    f"仅支持 S1|S2")
            total += share
            out.append(SpeedConfig(name=name, entry=entry, exit=exit_,
                                   share=share, fs=fsp, system=system))
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"{code}: speeds share 之和需为 1 (当前 {total})")
        if len(raw_list) > 1:
            for j, sp in enumerate(raw_list):
                for field in ("name", "system"):
                    if not sp.get(field):
                        raise ValueError(
                            f"{code}: 多档 speeds[{j}] 必须显式配置 {field}; "
                            "name 是快照身份, system 决定 S1/S2 语义")
        return out

    @staticmethod
    def _parse_filters(code: str, raw_list) -> list:
        """解析 instruments[].filters; 校验类型已注册/params 为 dict"""
        if raw_list is None:
            return []
        if not isinstance(raw_list, list):
            raise ValueError(f"{code}: filters 需为列表")
        out = []
        for j, f in enumerate(raw_list):
            if not isinstance(f, dict) or not f.get('type'):
                raise ValueError(
                    f"{code}: filters[{j}] 缺 type")
            ftype = str(f['type'])
            params = f.get('params') or {}
            if not isinstance(params, dict):
                raise ValueError(f"{code}: filters[{j}] params 需为映射")
            get_filter_class(ftype)   # 未注册类型在此报错 (fail-fast)
            out.append(FilterConfig(type=ftype, params=dict(params)))
        return out
