# C PositionRouter + 账本化下单实施计划

> **[已作废 — 2026-06-02]**
> 本计划已被 v10 架构简化 (commit 88ba1c5) 完全反向：
> - SubStrategy → ArbCore（单实例，非 facade）
> - PositionRouter / PositionLedger / InstanceDecision / TargetIntent 全部删除
> - SpreadArbStrategy 从多 sub 列表改为单 ArbCore 实例
> - 仓位读取统一走 context.stra_get_position
> 保留本文件仅供历史参考，不要按此文档开发。

**Goal:** 在保留现有回测行为的前提下，将 `SubStrategy` 降级为轻量 facade/instance，由外层统一调度，并引入 `PositionLedger` + `PositionRouter`，支持多 sub 共享同一 stdCode 的净额化下单与 sub 级仓位视图。

**Architecture:** 第一阶段保留 `SubStrategy.py` 文件名，不重命名，降低 diff。将 `SubStrategy.process()` 拆成“状态同步/计算决策/持久化”等阶段；`SpreadArbStrategy` 负责全局 reconcile → compute all → route/flush → persist。`ArbExecHelper` 逐步从“直接下单者”降级为执行状态机，所有 WT `stra_set_position` 由 `PositionRouter` 统一发出。

**Tech Stack:** Python, WonderTrader CTA (`CtaContext`), v9 layered config, pytest/py_compile, existing `runBT.py --experiment --config-root` backtest entry.

---

## 已拍板约束

1. 采用 B 路线：`SubStrategy` 降级 + 外层统一调度。
2. 允许把 `SubStrategy` 拆分/降级作为 C0/C1 前置任务。
3. 第一阶段保留 `SubStrategy.py` 文件名，不做文件重命名。
4. `execution.position_router.enabled` 默认应为 false，旧路径完全不变。
5. 默认 no-overlap 回测必须与当前 baseline 一致；账本化允许 ±1e-6 浮点漂移，但核心收益指标必须一致。

---

## 核心接口设计

### 1. `TargetIntent`

位置：`helpers/PositionRouter.py` 或 `schemas.py`。

字段：

```python
@dataclass
class TargetIntent:
    sub_name: str
    code: str
    target: int
    tag: str
    reason: str = ""
```

语义：单个 sub 对单个 code 的目标仓位意图。它不是 WT 净仓目标。

### 2. `InstanceDecision`

位置：先放 `helpers/PositionRouter.py` 或 `schemas.py`，后续可独立。

字段：

```python
@dataclass
class InstanceDecision:
    sub_name: str
    intents: list[TargetIntent]
    marks: list[dict] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
```

语义：一次 `compute_decision()` 的输出。第一版可只用 `intents`。

### 3. `PositionLedger`

位置：`helpers/PositionLedger.py`。

职责：维护 sub 级目标仓位与虚拟仓位。

第一版字段：

```python
class PositionLedger:
    desired: dict[str, dict[str, int]]       # sub -> code -> desired target
    virtual: dict[str, dict[str, int]]       # sub -> code -> filled/confirmed virtual position
    pending: dict[str, dict[str, int]]       # sub -> code -> pending desired target
```

第一版方法：

```python
set_desired(sub_name, code, target)
get_desired(sub_name, code) -> int
get_position(sub_name, code) -> int
mark_sub_filled(sub_name, code, target)
aggregate_desired() -> dict[str, int]
positions_for_sub(sub_name, codes) -> dict[str, int]
```

第一版不强求精确成交价归因，PnL 可继续在 no-overlap 中走 WT，overlap PnL 后续 C3 精化。

### 4. `PositionView`

位置：`helpers/PositionLedger.py`。

职责：给单个 sub 提供仓位查询视图，替代直接 `context.stra_get_position(code)`。

接口：

```python
class PositionView:
    def get_position(self, code: str) -> int: ...
    def get_positions(self, codes: list[str]) -> dict[str, int]: ...
    def has_position(self, codes: list[str]) -> bool: ...
```

第一版 PnL：
- router disabled：仍用 WT context。
- router enabled + no-overlap：可直接转发 WT PnL，保证等价。
- router enabled + overlap：先返回 0 或 mark-price 近似，C3 再精化；但 C2 验收时必须单测仓位语义。

### 5. `PositionRouter`

位置：`helpers/PositionRouter.py`。

职责：聚合所有 sub desired target，统一向 WT 发出 code 级净目标。

接口：

```python
class PositionRouter:
    def __init__(self, ledger: PositionLedger, enabled: bool = False): ...
    def view(self, sub_name: str) -> PositionView: ...
    def submit_intent(self, intent: TargetIntent): ...
    def submit_decision(self, decision: InstanceDecision): ...
    def aggregate_targets(self) -> dict[str, int]: ...
    def reconcile(self, context): ...
    def flush(self, context): ...
```

`flush()` 语义：
- 计算 `net_target[code] = sum(ledger.desired[sub][code])`
- 读取 WT 当前真实净仓 `context.stra_get_position(code)`
- 若不同，调用一次 `context.stra_set_position(code, net_target, tag)`

---

## Task C0：配置开关 + 纯账本/路由单元测试

**目标：** 新增 ledger/router 基础文件，但默认不影响任何回测行为。

**Files:**
- Create: `helpers/PositionLedger.py`
- Create: `helpers/PositionRouter.py`
- Modify: `schemas.py` 或 `helpers/PositionRouter.py` 添加 dataclass
- Modify: `config/base/execution.yaml` 或当前 v9 execution 配置源，增加 `position_router.enabled: false`
- Test: 新建或补充 `tests/test_position_router.py`（若项目无 tests 目录，则放 `tests/`）

**步骤：**

1. 写 RED 测试：两个 sub 对同一 code 的 desired 聚合。
   - subA rb +1
   - subB rb -1
   - aggregate rb = 0

2. 写 RED 测试：同向叠加。
   - subA rb +1
   - subB rb +2
   - aggregate rb = +3

3. 写 RED 测试：PositionView 只返回当前 sub 的 virtual position。
   - subA rb +1
   - subB rb -1
   - view(subA).get_position(rb) == +1
   - view(subB).get_position(rb) == -1

4. 实现 `PositionLedger` / `PositionRouter` 最小逻辑。

5. 验证：

```bash
python3 -m py_compile helpers/PositionLedger.py helpers/PositionRouter.py
pytest -q tests/test_position_router.py
```

6. 回测验证 router 默认关闭不影响旧路径：

```bash
python3 runBT.py --experiment default
```

验收：
- 单测通过。
- default 回测能跑通。
- `position_router.enabled=false` 时，`ArbExecHelper.py` 仍走旧路径。

---

## Task C1：SubStrategy 降级第一步，拆 compute 与 execute 边界

**目标：** 保留 `SubStrategy.py` 文件名，但把“计算决策”和“执行下单”分开，为外层统一调度做准备。

**Files:**
- Modify: `strategies/SubStrategy.py`
- Modify: `strategies/SpreadArbStrategy.py`
- Test: default backtest result comparison script or existing summary comparison script

**重构原则：**

将当前 `SubStrategy.process(context, cur_date)` 拆成内部方法：

```python
def sync_state(self, context, position_view=None): ...
def compute_decision(self, context, cur_date, position_view=None) -> InstanceDecision: ...
def apply_legacy_execution(self, context, decision): ...
def persist_state(self, context): ...
```

第一阶段保持 `process()` 作为兼容入口：

```python
def process(self, context, cur_date):
    self.sync_state(context)
    decision = self.compute_decision(context, cur_date)
    self.apply_legacy_execution(context, decision)
    self.persist_state(context)
```

这样外部行为不变，但为 C2 的 orchestrator 调用铺路。

**步骤：**

1. 写 characterization 回归：当前 default 回测 summary 核心字段快照。
   - total_return
   - annual_return
   - max_falldown
   - calmar_ratio
   - win_rate
   - trades 数

2. 拆 `process()`，但保持内部仍调用 `self.eh.execute()` 和 `self.eh.close_all()`。

3. 确保 `process()` 老入口仍可用。

4. 验证：

```bash
python3 -m py_compile strategies/SubStrategy.py strategies/SpreadArbStrategy.py
python3 runBT.py --experiment default
```

5. 比较 summary：与 C1 前快照一致。

验收：
- 不引入 router 时行为完全不变。
- `SubStrategy.py` 文件名保留。
- `process()` 兼容入口保留。

---

## Task C2：SpreadArbStrategy 外层统一调度骨架

**目标：** 在 router disabled 时，外层调度仍等价；为 router enabled 的 compute-all/flush-all 准备架构。

**Files:**
- Modify: `strategies/SpreadArbStrategy.py`
- Modify: `strategies/SubStrategy.py`

新增外层调度伪代码：

```python
def on_calculate(self, context):
    self._cur_date = context.stra_get_date()

    if not self._position_router_enabled:
        for sub in self._subs:
            sub.process(context, self._cur_date)
        return

    self._position_router.reconcile(context)
    decisions = []
    for sub in self._subs:
        view = self._position_router.view(sub.name)
        sub.sync_state(context, view)
        decisions.append(sub.compute_decision(context, self._cur_date, view))
    for decision in decisions:
        self._position_router.submit_decision(decision)
    self._position_router.flush(context)
    for sub in self._subs:
        sub.persist_state(context)
```

**步骤：**

1. 增加 `self._position_router_enabled = False` 默认字段。
2. router disabled 继续走旧 `sub.process()`。
3. router enabled 分支先存在，但可暂时只支持 no-op 或 no-overlap smoke。
4. 验证 default 回测一致。

验收：
- router disabled 行为完全不变。
- 代码结构已支持 router enabled 分支。

---

## Task C3：ArbExecHelper 接入可选 router，但 no-overlap 必须等价

**目标：** router enabled 时，所有 `stra_set_position` 从 `ArbExecHelper` 移到 `PositionRouter.flush()`。

**Files:**
- Modify: `helpers/ArbExecHelper.py`
- Modify: `strategies/SubStrategy.py`
- Modify: `strategies/SpreadArbStrategy.py`
- Modify: `helpers/PositionRouter.py`

**改造点：**

`ArbExecHelper` 新增可选行为：

```python
if self._router is None:
    context.stra_set_position(code, target, tag)
else:
    return TargetIntent(sub_name=sub_config.name, code=code, target=target, tag=tag)
```

但更推荐把方法改为产出 intents：

- `_open_position(...) -> list[TargetIntent]`
- `_close_position(...) -> list[TargetIntent]`
- `close_all(...) -> list[TargetIntent]`
- `execute(...) -> list[TargetIntent]`

第一阶段可兼容：如果没有 router，内部直接下单；如果有 router，返回 intents。

**关键替换点：**

- `_open_position` lines 109-125
- `_close_position` lines 127-141
- `close_all` lines 143-172
- `on_tick_check` retry lines 225-235
- emergency close lines 242-245

**验收：**

新增实验配置：

```yaml
execution:
  position_router:
    enabled: true
```

跑 no-overlap：

```bash
python3 runBT.py --experiment default_router_enabled
```

比较 default router off：
- 核心收益指标一致，允许 ±1e-6。

---

## Task C4：PositionView 替换 SubStrategy / RiskMgr 的真实仓查询

**目标：** router enabled 时，状态机和风控使用 sub 级仓位视图，不再直接使用 WT code 级净仓。

**Files:**
- Modify: `strategies/SubStrategy.py`
- Modify: `helpers/RiskMgr.py`
- Modify: `helpers/ArbExecHelper.py`
- Modify: `helpers/PositionLedger.py`

**替换点：**

1. `SubStrategy.process/sync_state`：
   - `actual_has_pos = any(context.stra_get_position(...))`
   - 改为 `position_view.has_position(...)`

2. `SubStrategy` 未实现盈亏：
   - no-overlap 可继续转发 context PnL。
   - overlap 第一版可 mark-price 近似或暂时 0，但必须显式标注。

3. `ArbExecHelper.infer_direction(context)`：
   - 增加 `position_view` 参数。
   - router enabled 时用 view。

4. `RiskMgr.check(...)`：
   - 增加可选 `position_view` 或直接传入 positions/pnl。
   - 不再在 router enabled 时读 WT 净仓。

验收：
- no-overlap router enabled 仍一致。
- 单测验证正反抵消时：subA/subB 的 direction 不被净仓 0 误判。

---

## Task C5：放开 leg overlap，但只在 router enabled 时允许

**目标：** `_check_leg_uniqueness()` 从硬拦截改为条件拦截。

**Files:**
- Modify: `strategies/SpreadArbStrategy.py`
- Create: overlap test experiment yaml, e.g. `config/experiments/overlap_smoke.yaml`

规则：

- router disabled：保持当前 ValueError。
- router enabled：允许 overlap，并打印 overlap map。
- 如果第一版选择只允许同向叠加，则检测正反冲突并报错。
- 如果第一版允许正反抵消，则不报错，但标记 experimental。

验收：
- router off + overlap config：必须启动失败。
- router on + overlap config：必须启动成功。
- subA close 不清掉 subB 的 ledger desired/virtual position。

---

## Task C6：完整回归矩阵

**目标：** 证明 C 不破坏默认回测，并证明 overlap 场景下不再互相覆盖。

**Commands:**

1. 编译：

```bash
python3 -m py_compile \
  runBT.py \
  schemas.py \
  helpers/PositionLedger.py \
  helpers/PositionRouter.py \
  helpers/ArbExecHelper.py \
  helpers/RiskMgr.py \
  strategies/SubStrategy.py \
  strategies/SpreadArbStrategy.py
```

2. 单测：

```bash
pytest -q tests/test_position_router.py
```

3. default router off：

```bash
python3 runBT.py --experiment default
```

4. default router on no-overlap：

```bash
python3 runBT.py --experiment default_router_enabled
```

5. overlap smoke：

```bash
python3 runBT.py --experiment overlap_smoke
```

**验收标准：**

- py_compile exit=0。
- router 单测通过。
- default router off 指标保持当前 baseline。
- default router on no-overlap 与 router off 核心指标一致，允许 ±1e-6。
- overlap smoke 能启动、能产生净额目标，不出现后 sub 覆盖前 sub 的问题。
- 风控 close_all 只影响触发风控的 sub ledger target。

---

## 风险与防线

1. 风险：一次性把 SubStrategy 拆太大，baseline 变动难定位。
   - 防线：C1 只拆边界，保留 `process()` 老入口，先做等价回归。

2. 风险：router flush 时机改变同 bar 下单顺序。
   - 防线：router disabled 不动；router enabled no-overlap 先做严格指标对比。

3. 风险：sub 级虚拟 PnL 与 WT 总 PnL 不一致。
   - 防线：C2 第一版只把仓位语义做正确；C3 再精化 PnL 归因。

4. 风险：`stra_set_position` pending 判定仍看 WT 净仓。
   - 防线：C4 必须把 `on_tick_check` 的成交确认改为 router/ledger 视图。

5. 风险：v8.1 老入口被破坏。
   - 防线：`SpreadArbStrategy.__init__(strategy_config_path, factor_config_path)` 保留；router 默认 false。

---

## 推荐执行顺序

1. C0：纯 ledger/router + 单测。
2. C1：SubStrategy 降级边界，保持行为一致。
3. C2：SpreadArbStrategy 外层统一调度骨架，router off 等价。
4. C3：ArbExecHelper router 化，no-overlap router on 等价。
5. C4：PositionView 接入状态机/风控。
6. C5：router enabled 时放开 leg overlap。
7. C6：完整回归矩阵。

每阶段通过后再进入下一阶段；若任一阶段 default 回测指标不一致，先停下定位，不继续叠加改动。
