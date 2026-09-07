"""turtle_config.yaml → TurtleConfig 加载与校验"""
import yaml

from schemas import (TurtleConfig, TurtleBaseConfig, InstrumentConfig,
                     RiskConfig, FilterConfig, NotifyConfig,
                     VALID_DIRECTIONS, VALID_SYSTEMS, VALID_PERIODS,
                     VALID_SUB_PERIODS)
from filters.base import get_filter_class


class ConfigLoader:
    @staticmethod
    def load(path: str) -> TurtleConfig:
        with open(path, encoding='utf-8') as f:
            raw = yaml.safe_load(f) or {}

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
            filters = ConfigLoader._parse_filters(code, item.get('filters'))
            instruments.append(InstrumentConfig(
                code=code, period=period, system=system,
                units=int(item.get('units', 1)), direction=direction,
                group=group, filters=filters,
                intrabar_stop=bool(item.get('intrabar_stop', False))))

        if not instruments:
            raise ValueError("instruments 为空")
        return TurtleConfig(base=base, instruments=instruments, risk=risk,
                            notify=notify)

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
