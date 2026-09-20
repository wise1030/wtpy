"""turtle_config.yaml → TurtleConfig 加载与校验"""
import sys

import yaml

from schemas import (TurtleConfig, TurtleBaseConfig, InstrumentConfig,
                     RiskConfig, FilterConfig, NotifyConfig, SpeedConfig,
                     VALID_DIRECTIONS, VALID_SYSTEMS, VALID_PERIODS,
                     VALID_SUB_PERIODS)
from filters.base import get_filter_class

# 顶层段与 instruments[] 项的合法键 (P1-6 加固: 此前拼错的键如
# untis/drection 会被静默忽略回默认值, 与 base/risk/notify 的不对称
# 防护是配置面最危险的洞)
_TOP_KEYS = {"base", "risk", "notify", "instruments", "config_version"}
_INST_KEYS = {"code", "period", "system", "units", "direction", "group",
              "filters", "intrabar_stop", "speeds"}


class ConfigLoader:
    @staticmethod
    def load(path: str) -> TurtleConfig:
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

        risk_raw = raw.get('risk', {}) or {}
        known_risk = {k for k in RiskConfig.__dataclass_fields__}
        unknown = set(risk_raw) - known_risk
        if unknown:
            raise ValueError(f"risk 配置含未知字段: {unknown}")
        risk = RiskConfig(**risk_raw)
        risk.validate()

        notify_raw = raw.get('notify', {}) or {}
        known_notify = {k for k in NotifyConfig.__dataclass_fields__}
        unknown = set(notify_raw) - known_notify
        if unknown:
            raise ValueError(f"notify 配置含未知字段: {unknown}")
        notify = NotifyConfig(**notify_raw)
        if notify.type not in ("wechat", "dingtalk", "generic"):
            raise ValueError(f"notify.type 非法 '{notify.type}'")

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
            filters = ConfigLoader._parse_filters(code, item.get('filters'))
            speeds = ConfigLoader._parse_speeds(code, item.get('speeds'))
            instruments.append(InstrumentConfig(
                code=code, period=period, system=system,
                units=units, direction=direction,
                group=group, filters=filters,
                speeds=speeds,
                intrabar_stop=bool(item.get('intrabar_stop', False))))

        if not instruments:
            raise ValueError("instruments 为空")
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
    def _parse_speeds(code: str, raw_list) -> list:
        """解析 instruments[].speeds (多速度); 份额之和须为 1"""
        if raw_list is None:
            return []
        if not isinstance(raw_list, list) or not raw_list:
            raise ValueError(f"{code}: speeds 需为非空列表")
        out, names, total = [], set(), 0.0
        for j, sp in enumerate(raw_list):
            if not isinstance(sp, dict):
                raise ValueError(f"{code}: speeds[{j}] 需为映射")
            name = str(sp.get("name") or f"V{j}")
            if name in names:
                raise ValueError(f"{code}: speeds 名称重复 {name}")
            names.add(name)
            try:
                entry, exit_ = int(sp["entry"]), int(sp["exit"])
            except (KeyError, TypeError, ValueError):
                raise ValueError(
                    f"{code}: speeds[{j}] 缺 entry/exit 或非整数") from None
            if entry < 2 or exit_ < 2:
                raise ValueError(f"{code}: speeds[{j}] entry/exit 需 >= 2")
            share = float(sp.get("share", 1.0 / len(raw_list)))
            if not 0 < share <= 1:
                raise ValueError(f"{code}: speeds[{j}] share 需在 (0,1]")
            fsp = int(sp.get("fs", 55))
            if fsp < 2:
                raise ValueError(f"{code}: speeds[{j}] fs 需 >= 2")
            total += share
            out.append(SpeedConfig(name=name, entry=entry, exit=exit_,
                                   share=share, fs=fsp))
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"{code}: speeds share 之和需为 1 (当前 {total})")
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
