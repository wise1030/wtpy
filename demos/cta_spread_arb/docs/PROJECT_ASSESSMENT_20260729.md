# cta_spread_arb 四维评估报告（2026-07-29）

> 上次评估：`PROJECT_ASSESSMENT_20260609.md`
> 本次评估视角：**项目架构（与 wtpy 对齐）/ 业务逻辑 / 代码优化 / 性能提升**
> 背景：2026-07 修复批（B2/B5/B6/B11/B12/C1/B4/D2/A8）已落地，188/188 测试全绿；
> 修复批复核纠正了 3 处问题（B5 语义、B7 回退、B11 setter 穿透）。
> 调查方法：两个独立探查代理全量走查 + 关键结论人工抽查（`ArbExecHelper.check_state_timeout`、`stat_factors.refit` backoff 已验证）。

---

## 0. 修复批复核结论（本轮新增）

| 项 | 复核结论 |
|---|---|
| B2/B6/B12/C1/B4/A8 | ✅ 正确落地 |
| B5 | ⚠️ 行为保留（浮亏降阈=等回归），注释已修正，5 测试锁定（`test_exit_judge_mr_loss.py`） |
| B7 | ❌ 报告前提不成立（`detect_regime` 不消费 coint_p）且引入权重滞后一 bar → **已回退** |
| B11 | ❌ 原修复是死代码（SignalGen 无该属性，实盘 profile 切换会 AttributeError）→ **已加 setter 穿透**，3 测试锁定（`test_b11_entry_threshold_propagation.py`） |
| D2 | ⚠️ 部分达成（RiskMgr.check 内部仍重复取持仓，见代码优化#3） |

---

## 1. 架构维度（与 wtpy 框架对齐）

### 1.1 对齐良好的部分

- 状态持久化走内建 `user_save/load_data`（`StateManager.py:24-56`），无自管文件 ✅
- 指标/图表 API 调用时机完全符合约定（on_init 注册、on_calculate 更新）✅
- `DataPipe._get_bars_cached` 正确规避 `stra_get_bars` 每次新建 WtNpKline 的成本 ✅
- usertag 31 字节约束处理正确（`ArbExecHelper.py:18-41`）✅
- `on_calculate` 单主 K 线驱动 + `on_bar` 空实现是合理简化 ✅

### 1.2 不对齐 / 未充分利用（按严重度排序）

| # | 问题 | 证据 | 建议 |
|---|---|---|---|
| A-1 | **run_matrix session 模式实质性 bug**：`WtBtEngine` 单 context 设计（`WtBtEngine.py:381-382,408-409`），`run_session` 循环 `set_cta_strategy` 导致所有回调只路由到最后一个策略，前面的实例永远收不到 on_init/on_calculate | `run_matrix.py:295-302` | **结果不可信，立即废弃 session 模式**（serial/subprocess 正常） |
| A-2 | **tick 订阅纯浪费**：订阅两腿 tick（`ArbCoreLifecycle.py:51-52`）但 `on_tick=pass`（`SpreadArbStrategy.py:307-308`），每 tick 白付 ctypes 解码 + ~60 字段 `to_dict`（`CtaContext.py:134`） | 实盘中两腿 tick 流量全是开销 | 二选一：(a) 删 `stra_sub_ticks`；(b) **实盘在 on_tick 恢复 `eh.on_tick_check`**（推荐，见 A-3） |
| A-3 | **执行确认退化为 bar 级**：状态机推进挪进 `process()` 后，成交确认/追单/紧急全平/反转开仓全部滞后最多 1 根主 K 线（m5=5 分钟），期间无腿超时保护 | `ArbCore.py:137-140` | 实盘恢复 tick 级 `on_tick_check`（套利腿暴露是秒级风险，bar 级检查太晚）；回测可保持 bar 级 |
| A-4 | **实盘多实例未落地**：框架支持多 `add_cta_strategy`（`WtEngine.py:388-394`），但 `runLive.py` 只挂一个策略且名字硬编码 `'spread_arb'` | `runLive.py:54,71,82` | runLive 支持遍历多 experiment 逐个 add（策略名需唯一），兑现"多组合=多实例"架构声明 |
| A-5 | `ArbCore._auto_profile_cfg` 从未赋值却用 hasattr 防御 | `ArbCore.py:151` | 真正传入或删除该分支 |

### 1.3 结论

框架能力利用率约 70%：持久化/指标/K线缓存用对了，但 **tick 通道"付成本不得时效"两头不占**，run_matrix session 模式是不可信的实验数据源头（若历史实验用过 session 模式需重跑）。

---

## 2. 业务逻辑维度

| # | 问题 | 证据 | 建议 |
|---|---|---|---|
| B-1 | **双重追踪止盈并存**：`ExitJudgeStep._pnl_trailing_check`（pnl_trailing_*，threshold=100，不触发冷却）与 `RiskMgr._builtin_profit_take_rule`（profit_*，threshold=500，触发冷却）~90% 同构，两套独立 peak 跟踪器 + 两套平行配置，同一持仓同时活跃 | `exit_judge.py:195-218` vs `RiskMgr.py:248-288` | 合并为单一 peak 跟踪器 + 单一配置族，"是否触发冷却"降为参数 |
| B-2 | **协整恶化无法阻止新开仓**：`coint_quality` 的 block-entry 分支是死代码（`check()` 空仓提前 return + ArbCore 仅持仓时调用） | `RiskMgr.py:136,316` | entry gating 前移到 `compute_decision`（coint_p ≥ block_entry_p 直接跳过 SignalGen），或删死分支 |
| B-3 | **反转开仓绕过入场校验**：`should_reverse` 由 ExitJudge 判定后直接 execute，不经过 EntryJudge 的 persist filter / blocked_hours 时段过滤 | `exit_judge.py:105-106` → `ArbCore.py:389-406` | 反转的新开仓侧补走 entry 校验（persist + 时段） |
| B-4 | **refit 间隔日期算术失真**：`cur_date` 是 YYYYMMDD 整数，跨月差 70、跨年差 8870 → 月末/年末后必然提前 refit | `stat_factors.py:118` | 改用交易日 ordinal 或 bar 计数 |
| B-5 | **rb/hc 结构性亏损**（C2 结论）未转化为准入规则 | `PROJECT_ASSESSMENT_20260609.md` §2 | PairScreener 加 `fee/avg_win < 0.8%` 准入门槛，preflight 拦截 |
| B-6 | **理论 vs 实际 PnL 无对账**：`SpreadCalculator._pnl_coeffs` 可算理论 PnL，与 `stra_get_position_profit` 的差=滑点+手续费，从未监控 | — | session_end 输出偏差，监控执行质量漂移 |
| B-7 | B5 修复批行为已确认：浮亏降阈等回归（MR 哲学），硬止损由 RiskMgr 兜底 | `exit_judge.py:142-165` | 已锁定，无需动作 |

---

## 3. 代码优化维度

| # | 问题 | 证据 | 建议 |
|---|---|---|---|
| C-1 | **`check_state_timeout` 回测失效且不可复现**：无条件 wall-clock（`ArbExecHelper.py:351`），`timeout_bars` 参数声明从未使用（line 336），docstring 声称"回测用 bar 计数"是谎言注释；高速回测 30s 覆盖数千 bar，慢机器/断点会幽灵强平 | `ArbExecHelper.py:334-364` | 回测走 bar 计数（启用 `timeout_bars`），修正 `test_p1_exec_state_machine.py:87,93` 的 wall-clock 语义 |
| C-2 | **配置体系 5 处静默丢弃/漂移**：① `signal_mode` 写在 YAML 但 SignalConfig 无此字段，改了不生效（`FactorEngine.py:134` 永远走默认）；② experiment 级 signal/risk/regime 覆盖被静默忽略（`ConfigResolver.py:305-311`）；③ `trend_aligned_boost` 等 3 个死配置字段（`schemas.py:108-110`）；④ dataclass 默认值与 from_yaml 默认值双写（改默认值要改两处）；⑤ RegimeConfig 缺 adf_maxlag/hurst_cache_every 等字段无法调 | schemas.py / ConfigResolver.py | 配置单源化：退役 legacy strategy_config.yaml；from_yaml 默认值改从 `dataclasses.fields` 读；静默丢弃改为显式报错 |
| C-3 | **持仓/盈亏同 bar 重复取数 3 处**：`ArbCore.py:249`（unrealized）、`ArbCore.py:305-306`（indicators）、`RiskMgr.py:131-134`（check），持仓 bar 上 get_position/get_position_profit 各 ~6 次 | 见左 | positions/pnl dict 在 compute_decision 算一次，传入 RiskContext 和 indicators |
| C-4 | regime 状态存 3 处（RegimeDetector/SignalGen/RegimeDispatchStep 各一份 `_last_regime`） | `SignalGen.py:28`、`pipeline.py:69-81` | 归一到 SignalGen 单源 |
| C-5 | 冗余接口：`SignalGen.generate` 生产零调用；`SignalPipeline.generate_with_regime` 是 generate 的逐参透传 | `SignalGen.py:92-105`、`pipeline.py:52-67` | 标 deprecated 或删除 |
| C-6 | `FactorEngine` 权重表双份同构（MODE_DIRECTION_MAP vs REGIME_WEIGHT_MAP，循环体重建逻辑相同） | `FactorEngine.py:52-100,229-271` | 合一 |
| C-7 | README 与代码漂移：#3 rank 死代码（实为 use_rank_v2 守护的休眠代码）、#5 backoff 反转（已修）、#6 DIRECTION_TYPE（已修）、#10 run_multi/run_batch（文件已删除）均过时 | README.md:446-449,457 | 同步 README |

---

## 4. 性能提升维度

每 bar 现状（2 腿、m5、3 因子、持仓中）：~35 次 ctypes 穿越 + ~200 次 numpy 小调用 + ~3000 次 Python 操作 + ~15 个 200 长数组分配。对 m5 CTA **量级可接受**，真正的悬崖在趋势族因子启用和实盘指标写盘两处。

| # | 优化点 | 位置 | 预估收益 |
|---|---|---|---|
| P-1 | **`_align_series` 向量化 + coint 模式去重**：当前 timestamps 路径每 bar 2 次完整 set/dict join（~3000 次 Python 操作），coint 模式第二次 join 输入完全相同仅 beta 不同 | `SpreadCalculator.py:154-180`、`DataPipe.py:189-209` | 纯 Python 侧提速 20-40%；`np.intersect1d(return_indices=True)` 一次完成，对齐索引复用给 coint 计算 |
| P-2 | **趋势族因子是纯 Python O(n) 循环**（启用即悬崖）：MACD ~600 次迭代/bar、ADX ~800-1200 次、RSI Wilder ~200 次（**mr profile 当前真实承担**） | `trend_factors.py:60-66,118-192`、`momentum_factors.py:51-61` | 改增量状态化（保留上一 EMA/Wilder 值）→ O(1)/bar |
| P-3 | **回测 write_indicator 白做**：runBT 无 set_writer → no-op，但每 bar 仍构造数据（4 次 ctypes + 大 dict） | `ArbCore.py:285-318`、`WtBtEngine.py:98-99` | 无 writer 时提前 return |
| P-4 | **实盘 ArbIndexWriter 每 bar 原子写盘**（makedirs + NamedTemporaryFile + json.dump + os.replace） | `ArbIndexWriter.py:19-38` | 改缓冲批量写（如每 N bar 或 N 秒 flush） |
| P-5 | `persist_state` 每 bar 4 次 user_save_data + 2 次 json.dumps，无论状态是否变化 | `ArbCore.py:278-283` | 加 dirty-flag，状态变化才写 |
| P-6 | 删除/降频 on_calculate 每 bar `stra_log_text`（上万 bar = 上万行噪声） | `SpreadArbStrategy.py:303-304` | 每 bar 省 1 次日志写 |
| P-7 | Hurst 每 bar 全量重算（~48 次 np.std Python 循环 + polyfit），hurst_cache_every 未透出配置 | `regime_detector.py:95,202-214` | 透出配置后设 cache_every=4~6 |
| P-8 | DiagLogger 纯计数 flush（实盘崩溃最多丢 49 条/类）；`_bar_cache` 当前零命中（每 bar 白付 4 次 stra_get_time） | `DiagLogger.py:40`、`DataPipe.py:50-79` | 加时间维度兜底 flush；bar cache 保留（防御性） |

---

## 5. 优先级路线图（按 ROI）

> **2026-07-29 执行状态**：立即/短期/中期全部完成，224/224 测试全绿。

### 立即（正确性，本周）—— ✅ 全部完成

| 项 | 状态 |
|---|---|
| A-1 废弃 run_matrix session 模式 | ✅ 调用即报错并指引 serial/subprocess |
| C-1 修 check_state_timeout 回测 bar 计数 | ✅ + 2 测试锁定 |
| B-4 refit 日期算术 | ✅ 日序数转换 + 跨月测试锁定 |

### 短期（业务正确性 + 配置可信，1-2 周）—— ✅ 全部完成

| 项 | 状态 |
|---|---|
| B-1 合并双重追踪止盈 | ✅ 删 RiskMgr profit_take（裁决：保留 ExitJudge 软止盈） |
| B-2 协整 entry gating 前移 | ✅ compute_decision 空仓硬阻断 + 2 测试 |
| C-2 配置单源化 | ✅ signal_mode 生效/from_yaml 单源/死字段删除/experiment 覆盖显式报错/RegimeConfig 补 7 参数 |
| B-3 反转开仓补 entry 校验 | ✅ persist+时段 veto，降级普通出场 + 4 测试 |
| A-2/A-3 tick 通道 | ✅ 实盘恢复 on_tick 状态机（回测保持 bar 级） |

### 中期（性能 + 整洁，按需）

| 项 | 状态 |
|---|---|
| P-1 align 向量化 + coint 去重 join | ✅ intersect1d+searchsorted + 5 测试 |
| P-3 回测跳过 write_indicator 构造 | ✅ |
| P-4 ArbIndexWriter 缓冲批量写 | ✅ flush_every=10 + close flush |
| P-5 persist dirty-flag | ✅ StateManager 序列化值比对 |
| P-6 删除每 bar on_calculate 日志 | ✅ |
| P-7 hurst_cache_every 透出配置 | ✅ 随 C-2 完成 |
| P-8 DiagLogger 时间兜底 flush | ✅ 60s |
| A-5 _auto_profile_cfg 传入 | ✅ |
| C-3 持仓/盈亏取数去重 | ✅ positions/pnl dict 全链路复用 |
| P-2 因子增量状态化 | ✅ lfilter/sliding_window_view 向量化(MACD EMA 位级一致, RSI/ADX Wilder 数值等价<1e-8, 5 测试锁定)。注意: ATR/DM 是和尺度递推(b=[1.0]), 与均值尺度(alpha=1/n)形式不同, 首次实现曾踩坑 |
| C-4 regime 三处缓存归一 | ✅ 删 RegimeDispatchStep 镜像 + Pipeline 冗余 properties, 单一来源 SignalGen |
| C-5 冗余接口 | ✅ SignalGen.generate / Pipeline.generate_with_regime 标注 deprecated |
| C-6 权重表双份 | ✅ 提取 _rebuild_weights 公共方法 |
| B-5 PairScreener fee 准入 | ✅ check_fee_economics(fee/avg_win<0.8%) + screen() 附 fee_ratio/fee_ok/exclude_fee_fail |
| B-6 理论 vs 实际 PnL 对账 | ✅ ArbCore._update_pnl_recon(持仓不变bar累计) + session_end 输出 gap |
| A-4 runLive 多实例 | ✅ --experiment 逗号分隔多实例 + write_arb_config_multi 合并 marker(兼容 ArbCore 对象, 原 sub['name'] 对对象会 TypeError) |

### 明确不做

- 纯 Python 微优化（当前 3 因子 m5 量级足够）
- A7 `generate_with_regime` 删除（破坏性变更，低收益）
