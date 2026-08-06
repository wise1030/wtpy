"""turtle_config.yaml → TurtleConfig 加载与校验"""
import yaml

from schemas import (TurtleConfig, TurtleBaseConfig, InstrumentConfig,
                     VALID_DIRECTIONS, VALID_SYSTEMS, VALID_PERIODS)


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
            instruments.append(InstrumentConfig(
                code=code, period=period, system=system,
                units=int(item.get('units', 1)), direction=direction))

        if not instruments:
            raise ValueError("instruments 为空")
        return TurtleConfig(base=base, instruments=instruments)
