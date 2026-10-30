# S1/S2 system 与 speeds 统一可行性分析

> 2026-10-30 分析并**已按 A 方案实施**（配置层统一，引擎/快照零改动；
> B 引擎去 system 化为可选后续）。本文档保留为设计论证存档。

---

## 1. 现状解剖：同一事物的三套词汇

| 层 | 词汇 | 实际语义 | 位置 |
|---|---|---|---|
| 配置层 | `system: S1\|S2\|S1+S2` | 速记符。S1/S2=单 profile 预设；S1+2=legacy 双系统 ceil/floor 拆 units | `InstrumentConfig.system` |
| 配置层 | `speeds[]` | 多 profile 组合（name/system/entry/exit/fs/share），**非空时忽略 system** | `InstrumentConfig.speeds` |
| 代码层 | `ProfileSpec(key, system, entry, exit, fs, units, share)` | **唯一中间态**，StraTurtle/组合回测/预检/日志/快照全部用它 | `core/profiles.py` |

关键观察：**合并已经进行了一半**。10-25 的 profiles 改造之后，代码里
到处是 `subs`/`profiles`，`system`/`speeds` 只剩两个角色：

1. `resolve_profiles()` 的**两个输入分支**（speeds 非空走 speeds 分支，
   否则走 system 分支展开成 1~2 个 ProfileSpec）；
2. 快照/日志里的**语义指纹**（`system` 字段是 ProfileSpec 的一部分）。

也就是说：分裂不在引擎、不在执行、不在风控，只在"用户写配置的入口"。
`speeds 非空时忽略 system` 这种优先级规则是教科书级的误配置温床
（10-25 就为此纠正过一次"system 与 speeds 无条件等价"的错误认知）。

## 2. 引擎层核验：system 到底是不是独立维度

枚举 `TurtleEngine` 中 `self.system` 的全部 10 处用途：

| 行 | 用途 | 实质 |
|---|---|---|
| 73/76 | 构造断言 + 存字段 | 身份 |
| 256 | fail-safe 武装门（仅 S1） | **filter 语义** |
| 273/923 | S1 过滤器门 `system=="S2" or not enable_s1_filter` | **filter 语义** |
| 345 | S1 入场评估分支 | **filter 语义** |
| 402 | S1 sim 单结算（仅 S1） | **filter 语义** |
| 1048/1072 | 快照指纹/恢复校验 | 身份 |

结论：**`system` 在引擎里 ≡ 一个布尔语义（S1 入场状态机：盈利跳过
+sim 单+fail-safe 一整套），外加一个预设名。窗口（entry/exit/fs）
早已是完全独立的参数**。而且 `enable_s1_filter`（"false=裸20/10突破"）
已经是一个半独立于 system 的开关——"S1 窗口 + 关过滤器"的现状组合
本身就证明 filter 与窗口正交。

所以语义上只存在三个正交维度：

```
窗口:   entry / exit / fs          —— 纯数值
语义:   filter (S1 状态机) on/off   —— 布尔
组合:   units (名义单位) / share (预算份额) —— 数值
```

`system` 和 `speeds` 都只是这三组维度的**预设/打包写法**，没有第四维。
"S2" 的完整定义 = `{entry:55, exit:20, fs:55, filter:off}` 的简写。

## 3. 目标模型：一个入口，preset 语法糖

```yaml
instruments:
  - code: SHFE.rb.HOT
    units: 2                     # 品种名义单位 (单层 VaR = units×unit_var)
    profiles:                    # 非空=多 profile; 缺省=单 profile 预设 S1
      - preset: S1               # 预设: 窗口+filter 语义, 可逐字段覆盖
        share: 0.5
      - name: slow               # 多 profile 时 name 必填 (快照身份)
        preset: S2
        entry: 40                # 覆盖预设窗口 → "S2 语义 + 40/20 窗口"
        filter: false            # 显式声明, 也可借 preset 缺省
        share: 0.5
```

配套规则：
- `preset: S1|S2` 只在**缺省字段**上填值（S1: 20/10/fs=base.s2_entry,
  filter=on；S2: 55/20/fs=55, filter=off）；显式字段永远赢。
- 单 profile 可写最简形态：`profiles: [{preset: S2}]` 或直接继承
  顶层速记（见 §5 兼容）。
- "speed" 这个名字退役：窗口组合 ≠ 速度，代码里早已叫 profile，
  配置改名消除最后一套词汇。
- 语义收益（现在表达不了、统一后自然表达）：`55/20+filter on`、
  `20/10+filter off`、`40/15 自定义窗口+filter on` 等任意组合；
  walk-forward 的网格也从"system 二选一"变成窗口×filter 的连续空间。

## 4. 引擎指纹怎么办（快照兼容）

v4 快照的 profile 指纹含 `system` 字段，`TurtleEngine.restore` 校验它。
两条路：

- **A（推荐，第一步）**：配置层统一，引擎与快照**完全不动**。
  `resolve_profiles` 输出仍带 `system` 字段（由 filter 位推导：
  filter=on→"S1"，off→"S2"），快照/恢复/日志零漂移。
  代价：`system` 成为导出字段而非输入字段，概念上仍在。
- **B（可选，第二步）**：引擎去 system 化——构造参数改
  `filter: bool`（替换 system+enable_s1_filter 二元组），快照指纹
  `system` 改 `filter`。需要快照版本 v5 + 迁移规则（v4 按
  system→filter 换算）。收益是概念彻底干净；成本是动引擎核心 +
  ~40 处测试口径。

A 做到位后 B 是纯机械重构，可以随时补；不补也不影响任何业务正确性。

## 5. 迁移与兼容矩阵

| 现状配置 | 统一后等价写法 | 处理 |
|---|---|---|
| `system: S1`（生产 12 品种全部如此） | `profiles: []`（缺省单 S1）或显式 `[{preset: S1}]` | 保留旧键作 deprecated 别名，ConfigLoader 警告并展开为同一 ProfileSpec 序列 |
| `system: S2` | `[{preset: S2}]` | 同上 |
| `system: S1+S2, units: u` | `[{preset: S1, share: ceil}, {preset: S2, share: floor}]`，units 按 share 折算 | 同上（ceil/floor 语义在别名展开层保留） |
| `speeds: [...]` | `profiles: [...]`（name/system 字段不变） | 键名迁移 + 警告 |
| 旧 v4 快照（含 system 指纹） | 不需要迁移 | A 方案下指纹不变，恢复零影响 |

`required_bars`/预检/组合回测/walkforward 网格全部只依赖
`resolve_profiles` 输出，入口统一后自动一致。

## 6. 成本与风险清单

需要动的面（全部机械、有测试网）：
- `schemas.py`：`InstrumentConfig.profiles` 新字段 + system/speeds
  deprecated 别名 + 未知键拒绝已就位；
- `core/profiles.py`：resolve 重写为"preset 展开 + 别名适配"单分支；
- `ConfigLoader` 校验、README/GUIDE/配置模板、walkforward 网格生成
  （system 参数→filter 布尔或保留二值）；
- 测试：test_profiles/test_config_safety/test_dual_system 等改输入
  写法，断言不变（ProfileSpec 序列不变 ⇒ 行为零漂移）。

风险：
1. **别名层必须薄**：deprecated 展开只做一次且打警告，禁止配置层
   再出现第二套优先级规则（本次合并要消灭的正是它）；
2. 快照身份键（profile key）集合不能变——S1+S2 别名展开必须仍产出
   key=["S1","S2"]，否则存量部署快照恢复 fail-loud（这是特性不是
   bug，但要写进迁移说明）；
3. 不要借合并之机改任何引擎语义（窗口/filter 的行为定义全部不动，
   黄金回测逐位一致是验收线）。

## 7. 明确回答

**可以合并，值得合并**。理由按权重排序：
1. 消除双入口优先级规则（`speeds 非空忽略 system`）这一结构性
   误配置源；
2. 语义正交性已被引擎实现证明（system ≡ filter + 预设名），配置
   模型如实反映它；
3. 词汇统一（配置层 "profiles" 对齐代码层，退役 "speed"/"system
   三选一"）；
4. 打开 filter×窗口的完整参数空间，walk-forward 与研究工具直接受益。

**边界**（合并不解决的事）：
- 10-30 双系统 A/B 负结果（+6.2% vs +75.3%）与本次无关——合并是
  概念完整性工作，不改变"生产维持单 profile S1"的结论；
- 合并本身不带来收益数字，收益是更低的理解/维护成本和更安全的
  配置演化；若团队认为 deprecated 别名层不值得维护，也可以更激进：
  直接删除 system/speeds 旧键并一次性迁移全部配置与文档（开发阶段
  允许，存量只有生产 12 行 S1 配置 + 测试夹具）。
