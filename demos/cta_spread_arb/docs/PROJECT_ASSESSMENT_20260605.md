# cta_spread_arb 项目全面评估报告

> 日期: 2026-06-05 | HEAD: c1be9d8 | 分支: wtpy_dev

---

## 一、项目现状盘点

### 1.1 代码体量

| 模块 | 文件数 | 代码行 | 职责 |
|------|--------|--------|------|
| strategies | 4 | 758 | 策略门面(ArbCore) + WT适配层 |
| helpers | 15 | 3,720 | 核心业务: 配置/信号/风控/执行/诊断 |
| factors | 7 | 1,306 | 13个因子实现 + FactorEngine |
| scripts | 9 | 1,178 | 投研工具: IC/WF/分析 |
| tests | 16 | 2,755 | 143个测试用例,全覆盖 |
| tools | 2 | 398 | PairScreener + BacktestAnalyzer |
| **总计** | **53** | **9,717** | |

### 1.2 配置体系 (v9 分层)

```
config/
├── base/           # 5 基础层: signal/risk/execution/regime/factors
├── contracts/      # 合约模板: rb_hc.yaml (legs_template)
├── experiments/    # 实验定义: default/rb_hc_2601/profile_p3_6
└── profiles/       # 参数档案: mr_contract/trending_contract
```

### 1.3 数据覆盖

| 品种对 | 合约月 | 周期 | 日期范围 | bar数 |
|--------|--------|------|----------|-------|
| rb/hc | 2510 | d1+m5 | 2024/10→2025/10 | ~6K |
| rb/hc | 2601 | d1+m5 | 2025/01→2026/01 | ~16K |
| rb/hc | HOT | d1 | 2009→2026 | 4,173天 |

**数据缺口**: 无 hc HOT m5; 无 2022-2024 年具体合约月数据; 无第二品种对(如 cu/al)。

### 1.4 演进里程碑

| 阶段 | 内容 | commit |
|------|------|--------|
| P0 | 实盘安全必修(9项修复) | 7b125a6 |
| P1 | 单元测试+诊断+边界 | e3295cd |
| P2 | auto_profile/regime缓存 | 1311a93 |
| P3 | schemas收紧/grid搜索 | 28ed21d |
| P4 | 性能优化(FactorBase bisect等) | 1311a93 |
| 阶段1-2 | v10架构+Lifecycle拆分 | e3295cd→f0ec1a7 |
| 阶段3 | 风控配置化+ADF优化 | e8aedb4→28ed21d |
| 阶段4 | 卫生清理(DEPRECATED标记) | c1be9d8 |
| **阶段6** | **策略扩展(M1/M2/M3)** | **待commit** |

---

## 二、功能架构分析

### 2.1 架构优势

1. **v10 单 pipeline 模式** — 1策略=1ArbCore, 多品种用多实例, 架构清晰
2. **分层配置** — base/contracts/experiments/profiles 四层解耦, ConfigResolver 统一解析
3. **因子可插拔** — FactorEngine 自动发现, yaml 启停, rank 标准化
4. **auto_profile** — Hurst 自适应选 trending/mr profile
5. **诊断体系** — DiagLogger 四通道(signal/risk/regime/exec), 回测后 flush
6. **测试覆盖** — 143 测试, 涵盖状态机/风控/信号/边界/pre等

### 2.2 架构局限

1. **1策略=1ArbCore** — WT 框架要求 BaseCtaStrategy 子类, 无法在单策略内做组合风控
2. **regime 二态** — 只有 trend/mr 两档, 缺 range(窄幅震荡) 状态
3. **风控 pair 级** — 无组合级暴露控制, 多实例并行时各 pair 独立风控
4. **SignalPipeline 遗留** — DEPRECATED 标记但仍被引用, 未完全移除

---

## 三、业务流程分析

### 3.1 回测链路 (已验证 ✅)

```
runBT.py → WtBtEngine → SpreadArbStrategy.from_experiment()
  → ConfigResolver → ArbCore
    → DataPipe(bar缓存) → SpreadCalculator
    → FactorEngine(3因子) → SignalGen
    → RiskMgr(7规则) → ArbExecHelper → ArbIndexWriter
    → DiagLogger(四通道)
  → WtBtAnalyst → outputs_bt/
```

### 3.2 实盘链路 (未验证 ⚠️)

```
runLive.py → WtEngine + WtMonSvr → SpreadArbStrategy
  → preflight 自检 → 实盘交易...
```

**缺口**:
- runLive.py 仅支持单实例 (--experiment)
- 无多实例并行实盘入口
- preflight 只检查数据/配置存在性, 不验行情连接/账户状态
- 无 watchdog / 心跳 / 断线重连
- 无 paper trading 模式
- 无组合级风控(多 pair 同时持仓上限)

### 3.3 Walk-Forward 链路 (已验证 ✅)

```
walk_forward.py → _wf_worker.py(子进程)
  → 按 fold 切 train/test → 独立回测 → 汇总 IC/PnL
```

### 3.4 因子投研链路 (已验证 ✅)

```
factor_ic_analysis.py → CSV加载 → 逐bar calculate → IC/共线性 → 重启建议
```

---

## 四、阶段6 验证结论

### M1: 多实例并行 ✅
- rb_hc_2510 + rb_hc_2601 在同一 session 并行成功
- StateManager/RiskMgr/DiagLogger 按 name 天然隔离
- **交付**: run_multi.py

### M2: Walk-Forward ✅
- 4折验证: 趋势段(Fold1: +870)盈利, 震荡段(Fold4: -410)亏损
- 策略强依赖趋势行情, mr_contract profile 参数不够激进
- **交付**: scripts/walk_forward.py + scripts/_wf_worker.py

### M3: 因子IC ✅
- IC排名: RSI(0.33) > ChannelBreak(0.29) > ZScore(0.27) > TrendStrength(0.26)
- 共线性集群: RSI+ChannelBreak+ZScore (互相关>0.8)
- **重启实验**: RSI+ADX+TrendStrength → 2601 mr PnL 从720→890(+23.6%), 但2510 trending 亏损加剧
- **交付**: scripts/factor_ic_analysis.py

**核心发现**: 6因子在MR行情有增益, 但在trending行情恶化。需要 regime-aware 的因子权重切换(非固定权重)。

---

## 五、未完成项分级

### P0 — 阻塞实盘 (必须做)

| 编号 | 项目 | 状态 | 说明 |
|------|------|------|------|
| P0-1 | runLive 多实例 | 未做 | 无并行实盘入口,需 run_live_multi.py |
| P0-2 | preflight 增强 | 部分 | 缺行情连接/账户/合约状态检查 |
| P0-3 | paper trading | 未做 | 无模拟盘验证通道 |
| P0-4 | 组合级风控 | 未做 | 多pair同时持仓上限/暴露控制 |

### P1 — 业务缺口 (应该做)

| 编号 | 项目 | 影响 | 方案 |
|------|------|------|------|
| B1 | regime-aware 因子权重 | 6因子在trending恶化 | FactorEngine 按 regime 动态调权重 |
| B2 | 数据覆盖扩展 | 仅9个月数据 | ks2wtpy 拉 2022-2024 多合约月 + hc HOT m5 |
| B3 | 关停因子重启决策 | IC已有但未落地 | RSI+ADX+TrendStrength 仅在mr_contract profile启用 |
| B4 | SignalPipeline 移除 | DEPRECATED但仍引用 | SignalGen.py 改为直接用 FactorEngine, 删除 pipeline 引用 |

### P2 — 优化空间 (可以做)

| 编号 | 项目 | 收益 | 工作量 |
|------|------|------|--------|
| O1 | regime 三态(trend/mr/range) | 避免range误判 | 中 |
| O2 | CointResidualFactor IC脚本适配 | 投研完整 | 小 |
| O3 | walk-forward 年度化 | 样本外更可靠 | 需多年数据 |
| O4 | factor_ic 多品种验证 | IC泛化判断 | 需多品种数据 |

### P3 — 战略候选 (YAGNI, 默认不做)

| 编号 | 项目 | 判定 |
|------|------|------|
| S1 | BaseSpreadStrategy 抽象基类 | YAGNI |
| S2 | RL 执行优化 | YAGNI |
| S3 | ArbCore 拆分 | 不拆(386行可维护) |
| S4 | SQLite 持久化 | YAGNI |

---

## 六、优化改进方案建议

### 方案1: Regime-Aware 因子权重 (推荐优先)

**问题**: 6因子在mr行情有效(+23.6%), 但trending行情恶化(-91%)。原因是固定权重无法适应regime切换。

**方案**:
1. profiles/mr_contract.yaml 增加 `factor_overrides`, 启用 RSI+ADX+TrendStrength
2. profiles/trending_contract.yaml 保持原有3因子权重(或微调)
3. auto_profile 切换时同步切换因子权重

**工作量**: ~2h, 只需改 yaml + FactorEngine 一处读取逻辑

**风险**: 低, 向后兼容(不设 factor_overrides 则用默认权重)

### 方案2: 多年数据扩展 (推荐做)

**问题**: 仅9个月数据, walk-forward 2折已不够充分, 且无法验证跨年表现。

**方案**:
1. ks2wtpy 拉 2022-2025 rb/hc 具体4个合约月(每年2个月份)的 m5 数据
2. 生成 hc HOT m5 (当前只有 d1)
3. 写批量实验 yaml (experiments/rb_hc_2401.yaml 等)

**工作量**: ~1h (ks2wtpy S3 拉取) + 数据验证

**风险**: 中, S3凭证依赖, 数据质量需验证

### 方案3: 实盘准入路线图 (待启动)

**问题**: runLive.py 从未端到端验证。

**方案** (分3步):
1. **Step 1**: run_live_multi.py — 多实例并行实盘入口
2. **Step 2**: paper trading 模式 — WtEngine + 模拟账户
3. **Step 3**: preflight 增强 — 行情连接/账户/合约状态检查

**工作量**: Step1 ~3h, Step2 ~1天(需WT框架支持), Step3 ~4h

---

## 七、推荐路径

```
当前 ──→ 方案1(regime-aware因子) ──→ 方案2(多年数据) ──→ 方案3(实盘准入)
  │          ~2h                        ~2h                  ~2天
  │          立即可做                    需S3连接              需实盘环境
```

**拍板问题**:
1. 方案1 是否现在做? (改 profile yaml + FactorEngine, 向后兼容)
2. 方案2 何时拉数据? (需确认 S3 凭证可用)
3. 方案3 Step1 是否启动? (run_live_multi.py)
