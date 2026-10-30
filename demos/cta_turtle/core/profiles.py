# -*- coding: utf-8 -*-
"""信号子系统 (profile) 的唯一解析入口。

S1/S2 是 TurtleEngine 的**入场状态机语义**及 base 中的默认窗口预设；
speeds 是同一个执行/风控单元内的多 profile 组合配置。二者不是同一
维度，但 legacy system/speeds 都必须在这里展开成同一种 ProfileSpec，
避免 StraTurtle、组合回测、预检和日志各自解释一套。

一个 profile 只包含当前真正支持的独立维度：
- key: 稳定的快照/日志身份；
- system: S1|S2，决定盈利跳过/sim/fail-safe 语义；
- entry/exit/fs: Donchian 窗口（fs 仅 S1 fail-safe 使用）；
- units: 仓位反算的名义单位；
- share: 品种内风险/保证金预算份额。

ATR、period、entry_mode、止损/加仓口径和外部过滤链仍是 instrument
级共享参数，不在这里伪装成可独立配置。

兼容约束：legacy S1+S2 的 units 是 ceil/floor 整数拆分，不能机械
映射成 50/50 speeds；units=1 时 S2 的 units/share 都可以为 0。
"""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class ProfileSpec:
    key: str
    filter: bool                # S1 入场状态机语义 (唯一语义开关, B 方案);
                                # "S1"/"S2" 降级为派生标签 (system 属性)
    entry: int
    exit: int
    fs: int
    units: float                # PositionSizer 名义单位
    share: float                # 品种内预算份额（legacy 可为 0）

    @property
    def system(self) -> str:
        """派生标签: "S1" if filter else "S2"。仅供日志/旧键兼容,
        不再是独立维度 (docs/PROFILE_CONFIG_UNIFICATION.md B 方案)。"""
        return "S1" if self.filter else "S2"

    def snapshot_meta(self) -> dict:
        """写入快照的语义指纹 (v5)。任何变化都要求显式迁移，禁止同名
        静默继承旧 s1_last/armed/channels 或混用新旧 sizing。
        (v4 快照的 system 字段由 validate_snapshot_meta 迁移为 filter。)"""
        return {
            "filter": bool(self.filter),
            "entry": int(self.entry),
            "exit": int(self.exit),
            "fs": int(self.fs),
            "units": float(self.units),
            "share": float(self.share),
        }


# 预设缺省 (docs/PROFILE_CONFIG_UNIFICATION.md): preset 只填缺省字段,
# 显式字段永远赢。S1 的 filter 缺省跟随 base.enable_s1_filter (全局
# "裸20/10" 开关的语义不变); S2 恒无过滤器语义。
_PRESET_DEFAULTS = {
    "S1": lambda base: {
        "filter": bool(base.enable_s1_filter),
        "entry": int(base.s1_entry), "exit": int(base.s1_exit),
        "fs": int(base.s2_entry),
    },
    "S2": lambda base: {
        "filter": False,
        "entry": int(base.s2_entry), "exit": int(base.s2_exit),
        "fs": int(base.s2_entry),
    },
}


def _expand_profiles(inst_cfg, base_cfg) -> list:
    """profiles 入口展开 (2026-10-30 配置统一的唯一新分支)。

    校验规则 (fail-loud, 与旧 speeds 分支同严格度):
    - 多档必须显式 name 且唯一 (快照身份); 单档 name 可缺省, 键=预设名
      (无预设时必须显式 name, 否则无法寻址);
    - share 各自 (0,1] 且合计 1; 单档缺省 1.0;
    - preset 仅 S1|S2; 无 preset 时 entry/exit 必须显式 (fs 缺省
      base.s2_entry, filter 缺省 base.enable_s1_filter);
    - preset S2 上显式 filter=true 非法 (S2 无过滤器语义);
    - 窗口整数 >= 2。
    """
    code = str(inst_cfg.code)
    items = list(getattr(inst_cfg, "profiles", None) or [])
    if not items:
        raise ValueError(f"{code}: profiles 需为非空列表")
    multi = len(items) > 1
    names = [str(getattr(p, "name", "") or "") for p in items]
    if multi and any(not n for n in names):
        raise ValueError(f"{code}: 多档 profiles 必须显式 name (快照身份)")
    if len(set(names)) != len(names):
        raise ValueError(f"{code}: profiles 名称重复")

    units_total = float(getattr(inst_cfg, "units", 1))
    out = []
    total_share = 0.0
    for j, p in enumerate(items):
        preset = str(getattr(p, "preset", "") or "").strip().upper()
        if preset and preset not in _PRESET_DEFAULTS:
            raise ValueError(
                f"{code}: profiles[{j}].preset 非法 '{preset}', 仅支持 S1|S2")
        if preset:
            eff = _PRESET_DEFAULTS[preset](base_cfg)
        else:
            eff = {"entry": None, "exit": None,
                   "fs": None, "filter": None}
        # 显式字段覆盖预设缺省
        for field in ("entry", "exit", "fs", "filter"):
            v = getattr(p, field, None)
            if v is not None:
                eff[field] = v
        if eff["entry"] is None or eff["exit"] is None:
            raise ValueError(
                f"{code}: profiles[{j}] 无 preset 时必须显式 entry/exit")
        flt = bool(eff["filter"]) if eff["filter"] is not None \
            else bool(base_cfg.enable_s1_filter)
        if preset == "S2" and flt:
            raise ValueError(
                f"{code}: profiles[{j}] preset S2 无过滤器语义, "
                "filter 只能为 false/缺省")
        for field in ("entry", "exit"):
            v = eff[field]
            if not isinstance(v, int) or isinstance(v, bool) or v < 2:
                raise ValueError(
                    f"{code}: profiles[{j}].{field} 需为 >=2 的整数")
        fs = eff["fs"] if eff["fs"] is not None else int(base_cfg.s2_entry)
        if not isinstance(fs, int) or isinstance(fs, bool) or fs < 2:
            raise ValueError(f"{code}: profiles[{j}].fs 需为 >=2 的整数")
        share = getattr(p, "share", None)
        share = 1.0 if share is None else float(share)
        if not 0 < share <= 1:
            raise ValueError(f"{code}: profiles[{j}].share 需在 (0,1]")
        total_share += share
        name = names[j] or preset
        if not name:
            raise ValueError(
                f"{code}: profiles[{j}] 无 preset 时必须显式 name")
        out.append(ProfileSpec(
            key=name, filter=flt, entry=int(eff["entry"]),
            exit=int(eff["exit"]), fs=int(fs),
            units=max(0.0, units_total * share),
            share=share))
    if not math.isclose(total_share, 1.0, abs_tol=1e-6):
        raise ValueError(
            f"{code}: profiles share 之和需为 1 (当前 {total_share:g})")
    return out


def resolve_profiles(inst_cfg, base_cfg) -> list:
    """把 profiles/system/speeds 三种输入统一展开为 ProfileSpec 列表。

    纯函数、零框架依赖。profiles 是唯一推荐入口 (2026-10-30 配置
    统一); system/speeds 是 deprecated 别名, 展开结果与旧实现逐字
    等价 (legacy 的 name、顺序、units 和 share 全部保留，以保证现有
    快照键和执行优先级不变)。
    """
    profiles = getattr(inst_cfg, "profiles", None) or []
    if profiles:
        return _expand_profiles(inst_cfg, base_cfg)

    speeds = getattr(inst_cfg, "speeds", None) or []
    if speeds:
        keys = [str(sp.name) for sp in speeds]
        if len(set(keys)) != len(keys):
            raise ValueError(f"{inst_cfg.code}: speeds 名称重复")
        if any(str(sp.system) not in ("S1", "S2") for sp in speeds):
            raise ValueError(f"{inst_cfg.code}: speeds.system 仅支持 S1|S2")
        shares = [float(sp.share) for sp in speeds]
        if any(not 0 < s <= 1 for s in shares) or \
                not math.isclose(sum(shares), 1.0, abs_tol=1e-6):
            raise ValueError(
                f"{inst_cfg.code}: speeds share 需各自在 (0,1] 且合计为 1")
        return [
            ProfileSpec(
                key=str(sp.name),
                filter=str(sp.system) == "S1"
                and bool(base_cfg.enable_s1_filter),
                entry=int(sp.entry), exit=int(sp.exit),
                # ConfigLoader 已按 base.s2_entry 填缺省；直接实例化
                # SpeedConfig 时 fs=None 也走同一缺省口径。
                fs=int(sp.fs if sp.fs is not None else base_cfg.s2_entry),
                units=max(0.0, float(inst_cfg.units) * float(sp.share)),
                share=float(sp.share))
            for sp in speeds
        ]

    if inst_cfg.system == "S1+S2":
        u = int(inst_cfg.units)
        u1, u2 = u - u // 2, u // 2
        denom = max(u, 1)
        return [
            ProfileSpec("S1", bool(base_cfg.enable_s1_filter),
                        int(base_cfg.s1_entry),
                        int(base_cfg.s1_exit), int(base_cfg.s2_entry),
                        float(u1), u1 / denom),
            ProfileSpec("S2", False,
                        int(base_cfg.s2_entry),
                        int(base_cfg.s2_exit), int(base_cfg.s2_entry),
                        float(u2), u2 / denom),
        ]

    system = str(inst_cfg.system)
    if system not in ("S1", "S2"):
        raise ValueError(
            f"{inst_cfg.code}: system 非法 {system!r}，仅支持 S1|S2|S1+S2")
    if system == "S2":
        entry, exit_ = base_cfg.s2_entry, base_cfg.s2_exit
    else:
        entry, exit_ = base_cfg.s1_entry, base_cfg.s1_exit
    return [ProfileSpec(system, system == "S1"
                        and bool(base_cfg.enable_s1_filter),
                        int(entry), int(exit_),
                        int(base_cfg.s2_entry), float(inst_cfg.units), 1.0)]


def required_bars(inst_cfg, base_cfg) -> int:
    """策略评估所需的最少信号周期 bar 数（含 prev 通道余量）。

    保留旧 _need_bars 对 base 全局窗口的覆盖，同时纳入所有 speed 窗口；
    ConfigLoader 据此 fail-fast，避免长窗口 speed 永久 NaN 空转。
    """
    longest = max(int(base_cfg.s2_entry), int(base_cfg.s1_entry),
                  int(base_cfg.s2_exit), int(base_cfg.atr_period))
    for p in resolve_profiles(inst_cfg, base_cfg):
        longest = max(longest, p.entry, p.exit, p.fs)
    return longest + 2


def snapshot_meta(profiles) -> dict:
    return {p.key: p.snapshot_meta() for p in profiles}


def validate_snapshot_meta(saved, profiles) -> None:
    """校验快照携带的 profile 指纹；旧快照无此段时由调用方做
    name 兼容检查。任何不匹配都抛 ValueError（fail-loud）。

    v4→v5 迁移 (B 方案 2026-10-30): v4 指纹的 system 字段迁移为
    filter = system=="S1" (v4 从不持久化 enable_s1_filter, filter 语义
    每启由配置重建, 该迁移与启动构造口径一致)。saved 多出的 system
    键容忍; 缺 filter 且无 system → fail-loud。
    """
    if saved is None:
        return
    if not isinstance(saved, dict):
        raise ValueError("snapshot profiles 段格式非法")
    expected = snapshot_meta(profiles)
    if set(saved) != set(expected):
        raise ValueError(
            "snapshot profile 集合不匹配: "
            f"saved={sorted(saved)} current={sorted(expected)}; "
            "请显式迁移快照，禁止静默漏恢复")
    for key, want in expected.items():
        got = saved[key]
        if not isinstance(got, dict):
            raise ValueError(f"snapshot profile {key} 元数据格式非法")
        # v4 迁移: system → filter
        got = dict(got)
        if "filter" not in got:
            if "system" not in got:
                raise ValueError(
                    f"snapshot profile {key} 缺 filter (v4 迁移亦需要 "
                    f"system 字段)")
            got["filter"] = got["system"] == "S1"
        for field, wv in want.items():
            if field not in got:
                raise ValueError(f"snapshot profile {key} 缺 {field}")
            gv = got[field]
            try:
                same = math.isclose(float(gv), float(wv), rel_tol=0.0,
                                    abs_tol=1e-12) \
                    if field in ("units", "share") else gv == wv
            except (TypeError, ValueError):
                same = False
            if not same:
                raise ValueError(
                    f"snapshot profile {key}.{field} 不匹配: "
                    f"saved={gv!r} current={wv!r}; 请显式迁移快照")


def describe_profiles(profiles) -> str:
    """启动日志用的完整有效配置，避免只打印原始 inst.system。"""
    return ", ".join(
        f"{p.key}[{p.system} {p.entry}/{p.exit}/fs{p.fs} "
        f"units={p.units:g} share={p.share:g}]" for p in profiles)


def validate_snapshot(data: dict, profiles) -> None:
    """校验 v3/v4 外层快照的 profile 完整性与语义。

    - 子系统 name 集合必须一致：旧实现找不到 name 直接跳过，可能把
      多 profile 净仓错误收养成首 profile 单层；
    - 新快照的 profiles 指纹必须一致；
    - 每个引擎快照的 system 必须与当前 profile 一致（TurtleEngine.
      restore 另有同口径防线）。
    旧快照没有 profiles 指纹段时仍允许恢复，但 name/system 必须匹配。
    """
    if not isinstance(data, dict):
        raise ValueError("snapshot 外层格式非法")
    saved_subs = data.get("subs")
    if saved_subs is None:
        saved_subs = {}
    if not isinstance(saved_subs, dict):
        raise ValueError("snapshot subs 段格式非法")
    expected = {p.key: p for p in profiles}
    if set(saved_subs) != set(expected):
        raise ValueError(
            "snapshot 子系统集合不匹配: "
            f"saved={sorted(saved_subs)} current={sorted(expected)}; "
            "请显式迁移快照，禁止静默漏恢复")
    validate_snapshot_meta(data.get("profiles"), profiles)
    for key, snap in saved_subs.items():
        if not isinstance(snap, dict):
            continue
        # v5 指纹 filter; v4 仅有 system → 迁移 filter=system=="S1"
        saved_filter = snap.get("filter")
        if saved_filter is None and "system" in snap:
            saved_filter = snap.get("system") == "S1"
        if saved_filter is not None \
                and bool(saved_filter) != expected[key].filter:
            raise ValueError(
                f"snapshot profile {key}.filter 不匹配: "
                f"saved={saved_filter} current={expected[key].filter}; "
                "请显式迁移快照")
