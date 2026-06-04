# C4 诊断报告 — PositionView 接入状态机/风控

**Status**: 待用户拍板 | **Author**: 奶酪 → 用户  
**Prereq**: C3 已 ✅ PASS（router off/on 数值完全等价）

---

## 1. 盘点现状

C 路线下，router on 时 PositionRouter 是仓位真理源（aggregates 所有 sub 的 desired，netting 后下单），但目前业务代码 16 处仍直接调 `context.stra_get_position*` 读 WT actual。当 sub_a 想多 1 手 rb、sub_b 想空 1 手 rb 时，WT 实际净仓位 = 0，但 sub_a/sub_b 各自的"持仓"语义应该 = ±1。继续读 WT actual 会让状态机/风控完全错位。

### 16 处调用清单（按调用方分组）

| 文件 | 行号 | 调用 | 用途 | router on 需切 view? |
|---|---|---|---|---|
| `helpers/ArbExecHelper.py` | 131 | `stra_get_position(code)` | _execute 判断当前 vs target 是否需下单 | **是** — sub 视角 |
| 同上 | 153 | 同上 | _close_position 当前持仓判断 | **是** |
| 同上 | 179 | 同上 | close_all 当前持仓判断 | **是** |
| 同上 | 238 | 同上 | on_tick_check 撮合确认 | **是** |
| 同上 | 361 | 同上 | get_status snapshot | **是** |
| `helpers/RiskMgr.py` | 128 | `stra_get_position(code)` | trigger_check 仓位 | **是** |
| 同上 | 129 | `stra_get_position_profit(code)` | trigger_check 浮盈 | ❓ view 无 pnl 接口 |
| `strategies/SubStrategy.py` | 155 | 注入 ArbExecHelper 的 position_getter | callback | **是** |
| 同上 | 210 | `stra_get_position_profit(leg.code)` | _compute_unrealized | ❓ |
| 同上 | 273 | `stra_get_position(leg.code)` | reconcile/sync 用 | **是** |
| 同上 | 274 | `stra_get_position_profit(leg.code)` | 同上 | ❓ |
| 同上 | 310 | `stra_get_position_profit` | 风控触发判定 | ❓ |
| 同上 | 380 | `stra_get_position_profit` | persist 持久化 | ❓ |
| `strategies/SpreadArbStrategy.py` | 379 | `stra_get_position(leg.code)` | on_backtest_end 检查 | **是** |
| `helpers/PositionRouter.py` | 61 | `stra_get_position(code)` | reconcile 判定 net 已达 | router 自身 — 不动 |
| 同上 | 71 | 同上 | flush 判定是否需下单 | router 自身 — 不动 |

### 量化差异点

- **position 类**（10 处业务侧）：router on 必须走 `view.get_position(code)`（即 `ledger.desired[sub][code]` 或已 `mark_sub_filled` 后的值）
- **profit 类**（5 处）：PositionView **当前无 pnl 接口**，需要扩展或保留走 context（router on 时按 net pos × 当前价 估算可能失真）

---

## 2. 诊断根因

- **C 路线 B 方案**核心：SubStrategy 降级为"目标输出器"，PositionRouter 外层 netting 后统一下单。如果 sub 仍读 WT actual 仓位，会出现：
  1. sub_a 提交 desired=+1, sub_b 提交 desired=-1
  2. PositionRouter netting → WT actual = 0
  3. sub_a 的 ArbExecHelper.on_tick_check 看到 `stra_get_position=0` → 误判未成交 → 重试 → desired 累加 → 雪崩
- **profit 类问题更复杂**：sub-level pnl 严格意义应基于"sub 自己的成交流水"算（FIFO/LIFO），不是 WT 的 net 仓位 mark-to-market。这需要 PositionLedger 扩展记账，是 C4+ 的工作量。

---

## 3. 候选方案

### 方案 A：最小切换（推荐）— 只切 position 类，pnl 类保留 context
- **范围**：10 处 `stra_get_position` 改为 router on 时走 view、off 时走 context
- **实现**：在 SubStrategy/ArbExecHelper/RiskMgr 加一个 helper `_get_position(code)`，根据 `router_enabled` flag 路由
- **保留**：5 处 `stra_get_position_profit` 不动（router on 时 sub-level pnl ≈ net-level pnl，业务接受度待评估）
- **工作量**：~3 小时（含测试）
- **解锁**：router on 时 sub 状态机自洽，不会因 netting 后 actual=0 误判
- **风险**：sub-level pnl 略有失真（netting 时多 sub 共享 WT pnl）；风控判定可能不精确但不会爆仓（仍有总仓位风控兜底）

### 方案 B：完整方案 — position + pnl 全部走 view
- **范围**：16 处全切，PositionLedger 加 `realized_pnl/unrealized_pnl_per_sub` 字段，需要记录每 sub 成交流水
- **工作量**：~2 天（含 ledger 记账逻辑 + 完整测试）
- **解锁**：彻底解决 sub-level 风控精度，C 路线设计完整闭环
- **风险**：ledger 复杂度上升一档，回测↔实盘 ledger 一致性需额外验证

### 方案 C：跳过 C4，先 C5/C6
- **理由**：C3 已证明 router on/off 数值完全等价 — 当前 default 实验单 sub_strategy 场景下 netting 退化为透传，PositionView 接入 **不影响 default 实验**
- **解锁**：多 sub 场景 / overlap 场景才需要 view
- **风险**：C5 放开 overlap 后必然踩到 sub_a/sub_b 互冲的雷，C4 是 C5 的必要前置

---

## 4. 推荐路径

**A → C5 → C6 → B（如有必要）**

1. **C4-α**: 方案 A（只切 position） — 解锁 C5 必要前置，pnl 类延后
2. **C5**: router enabled 时 bypass `_check_leg_uniqueness`
3. **C6**: 完整回归矩阵，验证多 sub overlap 场景正确性
4. **C4-β（可选）**: 若 C6 矩阵暴露 pnl 精度问题再上方案 B

### 阶段决策点
- C4-α 完成后跑一遍 c3_verify（router on/off 仍应等价）
- C6 矩阵跑完看 sub-level 风控触发是否符合预期，决定是否启 C4-β

---

## 5. 待用户拍板

1. **选 A / B / C 哪条路径？** （我倾向 A → C5 → C6）
2. **PositionView 路由方式**：在 SubStrategy 加 `self._pos_getter = view.get_position if router_on else context.stra_get_position`，还是在每个调用点 inline 三元判断？（推荐前者）
3. **pnl 类是否动**：方案 A 保留 context.stra_get_position_profit，是否接受 sub-level pnl 失真？
4. **是否需要 RED test 先写**：C4-α 切换前是否需要写一个"router on 时 sub_a 的 view.get 应等于 desired 而非 WT actual"的 RED 测试？

---

**Pending → 待奶酪拍板 → 拍板后落盘 `docs/c4_design.md` 并开工**
