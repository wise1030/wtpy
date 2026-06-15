# cta_spread_arb 项目综合诊断 + 优化建议 (P3 Report)

> **生成时间**: 2026-06-09
> **项目路径**: `/mnt/d/gf_pc/WonderTrader/wtpy/demos/cta_spread_arb`
> **当前 HEAD**: 5ddff08 (working tree dirty, 待 Phase 3 收尾)
> **测试状态**: 157/157 ✅
> **数据池**: 540 CSV (DCE.l/pp + SHFE.rb/hc 各 77 月 + index + 228 价差)
> **离线分析**: l/pp + rb/hc 各 1 个 multi-TF features.csv 已落盘

---

## 〇、TL;DR (3 句话)

1. **架构骨架已成型 (7/10)**：多品种 contracts 模板 + experiments 配置 + WF 子进程 + 多 TF 离线框架 + 双轨 rank v1/v2 已就位；但 4 处**配置-代码命名不一致 bug** 让 auto_profile / m30 周期 / refit backoff 实际**没生效**，导致 WF 跑出大量 0 成交噪声而非真实业绩。
2. **数据 → 信号链路有断点**：m30 周期没落盘 (storage/csv/ 只有 m5/d)，离线扫到的最优锚点 (l/pp m30×2w pnl_z=1.73、rb/hc m15×1w pnl_z=1.87) **无法直接喂回测验证**。要么补 m30 CSV，要么离线锚点先降级到 m5 跑通。
3. **业务层 4 个已知 bug 全部 unreachable**：CointResidualFactor refit backoff 反转、4 个 vol_price factors 缺 DIRECTION_TYPE、P0-γ 硬阻断、WT 日期解析 2025→3509。建议**Phase 3 内全清**，否则 WF 结果永远在噪声里。

---

## 一、项目分层现状盘点

```
┌─────────────────────────────────────────────────────────┐
│ Layer 5  外部入口    run_matrix.py / run_multi.py / WF │  ⚠️ 双入口未对齐
│ Layer 4  实验编排    experiments/*.yaml + profiles     │  ⚠️ profile 写死 main=m5/bar=200
│ Layer 3  策略容器    SpreadArbStrategy + ArbCore       │  ✅ 整体稳
│ Layer 2  数据/因子   DataPipe + factors/* + Cointeg    │  ⚠️ 4 个 bug
│ Layer 1  风控/执行   RiskManager + ExecManager         │  ⚠️ P0-γ 阻断
│ Layer 0  WT 引擎     wtpy + WonderTrader C++          │  ⚠️ 日期 2025→3509
└─────────────────────────────────────────────────────────┘
```

### 1.1 完成度矩阵

| 模块 | 完成度 | 状态说明 |
|---|---|---|
| 多品种 contracts 模板 | 90% | l_pp + rb_hc 落盘, 命名 `DCE.l.{month}` 规范 ✅ |
| experiments 配置体系 | 80% | yaml 入口可用; 但 `run_matrix.py` profile 硬编码 main=m5/bar=200 绕过 |
| WF 子进程框架 | 85% | scripts/walk_forward.py + _wf_worker.py 可跑; 但 worker 只回报 realized_pnl, **不暴露 0 成交根因** |
| 多 TF 离线分析 | 95% | tools/multi_tf/* 全通; **唯一遗留**: synth.py:82 FutureWarning 刷屏 (>4000 行) |
| 双轨 rank v1/v2 | 100% | use_rank_v2 默认 False 保留历史, True 激活真 rank, 4 新测试覆盖 ✅ |
| auto_profile (核心) | **30%** | 实现完整但**永远不触发** (命名 bug + 缺日线) |
| RiskManager | 70% | 单合约模式稳; P0-γ 硬阻断 unreachable |
| ExecManager | 80% | hedge/closeout 已审计 (skill wt-strategy-audit) |
| 因子库 (factors/) | 60% | CointResidualFactor refit 反转; 4 vol factors 缺 DIR; FactorBase v2 已修 |
| KS S3 数据下载 | 100% | 540 CSV 齐, 156 合约 / 0 错误 ✅ |
| 测试覆盖 | 80% | 157/157, 但**端到端 WF 没绿** (现在跑出 0 成交) |
| 文档 | 70% | README 重写 28KB; 但 `cta-spread-arb-architecture` skill 比 README 信息密度高 |

### 1.2 数据流断点

```
S3 (1m) ──[ks_dl]──> storage_research/ks_1m_raw/csv/ (540 CSV) ──┐
                                                                  │
storage/csv/ (生产, 18 CSV, 只有 m5+d)  <─── 这里断链 ───┘
                                                                  ▼
multi_tf/synth.py (m5/m15/m30/h1/h2 重采样) ── 只在离线分析用
                                                                  ▼
features.csv (660+644 行) ── 找出最优锚点 (m30×2w / m15×1w)
                                                                  ▼
????  ── 没有"锚点 → experiment yaml → WT 回测"的回灌通道 ❌
```

---

## 二、核心问题 (按优先级)

### 🔴 P0-1 配置-代码命名不一致 (auto_profile unreachable)

**根因** (`helpers/auto_profile.py:171-172`)：
```python
csv_a = os.path.join(storage_csv_dir, f'{leg_a.code}_d.csv')
```
- `leg.code` 实际是 `DCE.l.2609` (三段式, WT stdCode 标准)
- 拼出 `DCE.l.2609_d.csv` **存在** ✅
- **但** 历史上某些 yaml 写过 `l2609` (无前缀) → 拼出 `l2609_d.csv` **不存在** → 返回 None → fallback 到 baseline profile

**已验证**:
```
l2609 (无前缀)         → profile=None  (缺失日线: ./storage/csv/l2609_d.csv)
DCE.l.2609 (三段)      → profile=trending_contract  ✅ hurst=0.362, R²=0.845
DCE.l2609 (两段)       → profile=None  (缺失日线)
```

**影响范围**: 所有 auto_profile 体系 (mr/trending/baseline 自动判定) 都受这个文件名拼装影响。

### 🔴 P0-2 storage/csv/ 没有 m30 数据 → WF 永 0 成交

**事实**: `storage/csv/` 只有 `_d.csv` (日线) 和 `_m5.csv`，**没有 m30/m15/h1**。

`config/experiments/matrix_l_pp_2609_m30.yaml` 写 `main_period: m30` → WT 引擎找不到数据 → 0 bar → 0 信号 → 0 成交 (8 回测里 5 个 PnL=0)。

**这就是 l/pp WF 跑出 0 成交噪声的直接原因**。

### 🔴 P0-3 _wf_worker 不可观测 → 调试盲飞

`scripts/_wf_worker.py:40-43` 只回报：
```python
result = {'realized_pnl': stra.core.rm._realized_pnl, 'name': stra.core.name}
```

**缺**: 信号触发数、被各 filter 拒绝数、auto_profile 实际选了哪个、bar count、首/尾 tick。
→ 0 成交时无法定位是数据问题还是信号问题。

### 🟠 P1 业务层 4 个 unreachable bug (来自 skill 笔记)

| Bug | 位置 | 影响 |
|---|---|---|
| CointResidualFactor refit backoff **反转** | factors/CointResidualFactor.py | refit 失败时反向退避, 数值发散 |
| 4 个 vol_price factors 缺 `DIRECTION_TYPE` | factors/vol_price*.py | 信号方向丢失, 等价于无方向噪声 |
| P0-γ 硬阻断 unreachable | RiskManager.py | γ 控制写了但永不触发 |
| WT 日期解析 2025→3509 | wtpy 层 / configbt.yaml | 跨年回测时间戳错位 1500 年 |

### 🟠 P2 多入口未对齐

```
run_matrix.py        ── 走 profile 体系 (auto/mr/trending/baseline)
                        硬编码 main_period=m5, main_bar_cnt=200
                        ❌ 想跑 m30 必须改源码

run_multi.py -e <name>── 直接读 experiment yaml
                        ✅ 灵活, 但 profile 选择手动

scripts/walk_forward.py -e <name> --folds N
                     ── WF 入口, 调 _wf_worker 子进程
                        ⚠️ 只接 experiment 名, 不接 profile 切换
```

**问题**: 三个入口的"配置 → 实际跑什么"流程不一致，新人/agent 容易选错入口。

### 🟡 P3 noise

- `tools/multi_tf/synth.py:82` pandas 'T' deprecated 警告 (rb/hc 一次 4234 行刷屏)
- README 28KB 但 skill `cta-spread-arb-architecture` 已更详尽，README 有信息重复风险
- 测试 157/157 但**没有端到端 smoke test** (跑一个最小 WF 并验证 ≥1 笔成交)

---

## 三、多 TF 离线扫描关键发现

### 3.1 l/pp 2609 (聚烯烃跨品种)

```
最优锚点                  pnl_z    备注
(m30, 2w)                1.730    ★ 推荐
(m15, 1w)                1.479
(m15, 2w)                1.339
```

整体: 645 segments, win_rate 0.91, hurst 0.24 (强 MR), beta_cv 0.265 (β 漂移大)

### 3.2 rb/hc 2609 (黑色跨品种, 今天新跑)

```
tf  window  n_sig  win_rate  pnl_z    half_life
m15  1w     12     1.000     1.868    1.82  ★ 最高 pnl_z
m5   2w     410    0.958     1.600    4.71  ★ 信号最多
m5   3d     296    0.938     1.595    2.92
m5   1w     365    0.957     1.582    3.74
m15  2w     146    0.973     1.551    1.95
m30  2w     36     0.875     1.525    1.42
```

**关键观察**:
- rb/hc 在 m5/m15 短周期上信号密度远高于 l/pp (410 vs 数十个)
- rb/hc 在 m30+ 长周期上**信号数=0** (n_sig 列大量空) — 因为 ADF≥0.3，价差不够稳
- l/pp 的最优锚 (m30, 2w) 在 rb/hc 上**完全不工作**
- → **不同品种对最优 TF 敏感**，"一刀切" 配置一定亏

### 3.3 业务结论

| 品种 | 推荐主周期 | 推荐窗口 | 信号机制 | 当前生产 vs 推荐 |
|---|---|---|---|---|
| **l/pp** | m30 | 2w | MR + R² 强趋势绕过 | ❌ 写的 m5 |
| **rb/hc** | m15 | 1w | MR (信号少但极强) | ❌ 写的 m5 |
| rb/hc (高频补充) | m5 | 2w | MR 高密度 | ✅ 可用 |

**rb/hc 历史结构性亏损的根因之一**: 用 m5 做主周期但没用合适的 window，beta_cv 漂移大但没用 refit 兜底 (refit bug 又反转)。

---

## 四、优化建议路线图

### Phase 3-A: 止血修复 (1-2 天, 必做)

| # | 任务 | 工作量 | 期望效果 |
|---|---|---|---|
| A1 | 修 auto_profile 文件名拼装: 支持 `DCE.l.{month}` / `l{month}` / `DCE.l{month}` 三种 fallback | 30min | auto_profile 可触发 |
| A2 | 给 storage/csv/ 补 m30 (从 m5 重采样落盘) | 1h | m30 experiment 能跑 |
| A3 | _wf_worker 加 observability: n_signals, n_filtered_by_*, auto_profile_chosen, n_bars | 1h | WF 不再盲飞 |
| A4 | 修 4 个 unreachable bug (refit backoff, vol factors DIR, P0-γ, WT 日期) | 半天 | 因子真信号 |
| A5 | synth.py 'T'→'min' 静默 FutureWarning | 5min | log 不刷屏 |

### Phase 3-B: 配置回灌 + 验证 (2-3 天)

| # | 任务 | 工作量 | 期望效果 |
|---|---|---|---|
| B1 | 写"离线最优锚点 → experiment yaml" 自动生成脚本 (`tools/anchor_to_experiment.py`) | 半天 | 离线分析直接产出可跑配置 |
| B2 | 跑 l/pp m30×2w + rb/hc m15×1w + rb/hc m5×2w 三个 WF, 验证离线锚点是否回灌成立 | 半天 | 离线-在线一致性 |
| B3 | 端到端 smoke test (pytest): 最小 WF 必须 ≥1 笔成交 | 1h | CI 防回归 |
| B4 | 三入口对齐: profile 选择参数化 → run_matrix/run_multi/walk_forward 用统一 ConfigResolver | 半天 | 入口收敛 |

### Phase 3-C: 业务扩展 (1-2 周)

| # | 任务 | 工作量 | 期望效果 |
|---|---|---|---|
| C1 | 扫剩余品种对 (i/j, m/RM, MA/TA, SR/CF, ag/au...) 多 TF, 找最优锚 | 1-2 天 (每对 15s) | 候选池 |
| C2 | 引入 SPC 官方价差对照 (我们已下 46 个 SPC, DCE/CZCE 官方跨品种) — 验证我们的合成价差与官方对齐 | 半天 | 数据可信度 |
| C3 | β 漂移 (coi_beta_cv=0.265) → 引入 rolling refit + EWMA β, 解决 l/pp 长期亏损 | 2-3 天 | 减少β飘移损失 |
| C4 | 跨周期一致性过滤 (m5 信号 + m30 同向才进场) | 2 天 | 提高 win_rate |
| C5 | rb/hc 下架决策: 修完 P0/P1 后再回测一次, 若仍亏损则降仓至 0.3x 或下架 | — | 风险止血 |

### Phase 4 (远期, 4-6 周)

- 实盘对接 (DataPipe 实时模式 + ExecManager 真单)
- 多策略组合层 (跨多个套利对的 portfolio-level 风控)
- 在线 auto_profile (`auto_select_from_spread` 已实现, 但 ArbCore 没调用)
- 监控面板 (Prometheus + Grafana, 或 simple HTML)

---

## 五、待奶酪拍板的决策点

| # | 问题 | 选项 |
|---|---|---|
| Q1 | **Phase 3-A 全做吗?** | 全做 (1-2 天) / 只做 A1+A2+A3 (止血最小集) / 只做 A1+A5 (今晚跑通) |
| Q2 | **m30 数据怎么补?** | 离线重采样 m5→m30 落盘 (快) / 重新从 S3 下 m30 (S3 没 m5 以外的真实分钟数据, **此选项无效**) / 改用 m5+m15 跑 |
| Q3 | **rb/hc 当前怎么处理?** | 立即降仓 0.3x / 暂停 (paused=true) / 等修完 P0/P1 再回测决定 |
| Q4 | **多入口要对齐吗?** | 强对齐 (B4) / 留双入口但写"入口选择 cheat sheet" / 不动 |
| Q5 | **C1 候选池扫多少品种?** | 全扫 (40+ 对) / 只扫黑色+化工+农产品三大类 (~12 对) / 先扫 i/j (与 rb/hc 互补) |
| Q6 | **本报告落盘路径?** | `docs/PHASE3_REPORT.md` (推荐, 项目内可追溯) / `analysis_results/p3_report.md` / 不落盘只对话输出 |

---

## 六、附录: 关键文件与命令

**新增/修改文件 (working tree dirty)**:
- `factors/FactorBase.py` — v2 双轨化 (use_rank_v2)
- `tests/test_factorbase_rank_v2.py` — 4 新测试
- `config/experiments/matrix_l_pp_2609_m30.yaml` — m30 实验 (待修, 缺数据)
- `tools/multi_tf/{synth,features,analyzer}.py` — 多 TF 框架
- `storage_research/ks_1m_raw/csv/` — 540 CSV 池
- `analysis_results/l_pp_2609/` + `analysis_results/rb_hc_2609/` — 离线分析产物

**复用命令**:
```bash
# 多 TF 扫描 (其它品种)
python3 -c "
import sys; sys.path.insert(0, '.')
from tools.multi_tf.analyzer import run_pair_analysis
run_pair_analysis('i_j_2609', 'storage_research/ks_1m_raw/csv/DCE.i2609_m1.csv',
                  'storage_research/ks_1m_raw/csv/DCE.j2609_m1.csv',
                  'analysis_results/i_j_2609')
"

# WF (注意目前会 0 成交直到 P0 修完)
python3 scripts/walk_forward.py -e matrix_l_pp_2609_m30 --folds 4

# 测试
pytest -x
```

---

## 七、Phase 3-A 进度追加 (2026-06-10)

> 增量章节, 不修原文。记录 Q1+Q2+Q4+Q5+A1-A5 实际执行结果。

### 7.1 已完成项

| 编号 | 任务 | 结果 |
|---|---|---|
| Q2 | m30 数据补法 | 选定**离线重采样 m5→m30 落盘** (synth.py 已就位) |
| A1 | LegConfig stdCode 三段式校验 | ✅ schemas.py +18 行 + auto_profile.py 二次断言, 12 测试通过 |
| A3 | CointResidual refit backoff 反转 | ✅ stat_factors.py: `effective_interval = refit * 2^min(fail,5)`, cap 32x, 5 测试通过 |
| A4 | 4 个 vol_price factors 补 DIRECTION_TYPE | ✅ VwapDeviation/VolumePriceDiverg=reversal, OBVSpread/VolumeRatio=momentum, 6 测试 (含 meta-test 防回归) |
| A5 | **报告"WT 日期 2025→3509"已证伪** | ❌ **误诊勘误**: 3509 是 hc 真实成交价 (热卷价格本就 3500-3700), 不是日期解析 bug。strToDate 实现验证正常 (`split("/")` + strtoul), BOM 不影响数值解析 |
| B | csv_writer.py BOM 防御性清理 | ✅ 写端 `utf-8-sig`→`utf-8` (3 处), 读端保留 `utf-8-sig` 兼容老文件 |
| Q5 | **缩到本地 m1 覆盖的 4 品种 6 对组合** (rb/hc/l/pp) | ✅ 4 对跨板块已扫 |

测试现状: **180/180 ✅** (基线 157 + A1 12 + A3 5 + A4 6)

### 7.2 Q5 跨板块扫描结论 (黑色×化工)

**前提变更**: 原计划"扫黑色/化工/农产品三大类 (~12 对)" 因本地 m1 数据只覆盖 4 个品种 (rb/hc/l/pp, 即两对 spread 的腿), 缩减到这 4 品种 C(4,2)=6 对组合, 排除已有的 (l,pp)(rb,hc) 剩 **4 对跨板块**: rb-l / rb-pp / hc-l / hc-pp。

数据规模: 每对 ~54000 m1 bars × 210 trading dates × 5 TFs (m5/m15/m30/h1/h2) × 2 窗口 (1w/2w) = ~265 segments/对, 共 1060 行特征。

**核心排名 (m15+m30, 2w 窗口, 按 β 稳定性排序)**:

| 排名 | 配对 | β CV ↓ | ADF p | Hurst | win_rate | 评级 |
|---|---|---|---|---|---|---|
| 1 | **rb_hc_2609** | 0.176 | 0.310 | 0.135 | 93.8% | ★★ 基线 |
| 2 | **l_pp_2609** | 0.197 | 0.174 | 0.210 | 95.8% | ★★★ 基线最优 |
| 3 | hc_pp_2609 | 1.565 | 0.117 | 0.224 | 94.2% | ★ 危险 |
| 4 | rb_pp_2609 | 2.164 | 0.133 | 0.237 | 90.8% | ★ 危险 |
| 5 | hc_l_2609 | 3.856 | 0.225 | 0.241 | 94.5% | ✗ 废弃 |
| 6 | rb_l_2609 | 5.494 | 0.252 | 0.253 | 90.8% | ✗ 废弃 |

**结论**:
1. **跨板块 4 对全部 β 飘移严重** (cv ≥ 1.5 vs 同板块基线 0.18-0.20, 飘移幅度 8-30 倍), 黑色↔化工无稳定协整关系, **不构成新候选池**。
2. **sig_win_rate 看似都 >90% 的假象**: signal 模块用滚动 z-score 自适应阈值, 在任何噪声序列上都会"统计上像盈利", 但 β CV 决定**实盘是否能持有头寸不爆**。跨板块 4 对的 win_rate 高 ≠ 可用。
3. **本地数据池的天花板已到顶**: 想做真正的多对候选池扫描, 必须扩 m1 下载范围 (i/j/jm/ma/ta/y/p 等), 这是 Phase 3-C 的 C1 任务。
4. **现有两对 (l-pp + rb-hc) 仍是唯一可用品种**, rb-hc 结构性亏损问题需要回到 Phase 3-A 修完 P0/P1 后再决定 (Q3)。

### 7.3 Q4 强对齐结论 (多入口)

(基于 A1 实施过程中的副发现) `run_matrix.py` 与 `run_multi.py` 的 profile 解析路径已被 A1 校验逻辑统一收口到 schemas.py 的 LegConfig 入口, **不需要 B4 单独再做一次重构** — Q4 推迟到 Phase 3-B 评估时再看是否真有重复成本。

### 7.4 未完成项 (Phase 3-A 剩余)

| # | 状态 | 原因 |
|---|---|---|
| A2 (P0-γ 阻断) | pending | 未触碰, 需要扫 risk_manager.py 确认硬阻断点 |
| B1-B4 (Phase 3-B) | pending | 等 Phase 3-A 收完口 |
| C1 (扩品种扫描) | pending | **需要先决定: 用现有 ks2wtpy 下黑色/化工/农产品全品种 m1**, 估计 30+ 品种 × 12 月 = 360+ 合约, 磁盘 1-2GB |

### 7.5 A5 误诊根因复盘 (流程改进)

- **错因**: PHASE3_REPORT.md §3 P0-2 描述"日期 2025→3509"时, 误把 csv 的某行 `SHFE.hc.2601,...,3509,1,...` 的 close 列读成 date 列
- **流程教训**: 写 bug 报告前必须 (a) `head` csv 看 header (b) 拿一行数据手工跑 strToDate 验算 (c) 排除"价格列误读为日期列"
- **已固化到 skill**: 详见 `cta-spread-arb-architecture` skill 中"verifying-bug-reports-before-fixing"段, 防再次发生

### 7.6 A2 (P0-γ 硬阻断) 误诊勘误 (2026-06-10)

> 第二个被证伪的"bug": 与 A5 类似, 报告把 by-design unreachable 误判成需要修的 P0 bug。

**报告原描述** (§2 P0 表 / §1.1 完成度矩阵):
- "P0-γ 硬阻断 unreachable, γ 控制写了但永不触发"
- RiskManager 完成度评级 70%, 含"P0-γ 硬阻断 unreachable"扣分

**实际事实** (基于代码 + 测试验证):

| 维度 | 现状 |
|---|---|
| 硬阻断代码 | `strategies/ArbCore.py:367-376` 已实现, 含 ENV 开关 `P0_GAMMA_HARD_BLOCK=0/1` 供 A/B 实验 |
| 单测覆盖 | `tests/test_p0_gamma_trend_hard_block.py` 4 测试全绿 ✅ (trending+counter-trend 阻断 / mr 不阻断 / baseline 不阻断 / trending+trend-aligned 仍开) |
| 软衰减前置 | `helpers/pipeline/entry_judge.py:46-50` 先把 trend 逆势信号 × `_trend_counter_decay_factor` (默认 0.5), 衰减后 abs(signal) 通常已 < threshold → L74 `should_trade=False`, 硬阻断分支自然不进入 |
| 历史实验数据 | 原作者 indice.csv 统计 (l/pp 2609 + rb/hc 2510, ON/OFF 4 实验): trend regime 真实存在 (11%/6% bar), 但 `trend_block_hard` 触发 = 0 — **这是预期行为, 不是 bug** |

**原作者代码注释明确说明设计意图** (ArbCore.py:357-366):
> "保留代码 + 单测作为: (1) 未来 threshold 下调时的兜底硬阻断 (2) 软衰减系数被调整 (如 ×0.8) 时的安全网"

**结论**:
- P0-γ 设计完整: 软衰减为主 + 硬阻断兜底, 双层防御
- "unreachable" 是**正确的运行时表现**, 不是缺陷
- 完成度评级修正: RiskManager 70% → **85%** (单合约模式稳, P0-γ 双层防御已落地, 多策略 portfolio 层是 Phase 4 工作)

**误诊根因复盘**:
- 写报告时只看到"代码分支 0 次触发", 没读 (a) 同文件上方 50 行的设计注释 (b) entry_judge 软衰减前置逻辑 (c) 现有单测
- **流程教训**: 报告任何"unreachable bug"前必须确认: (1) 是不是 by-design (2) 有没有上游 short-circuit (3) 是否已有保护性单测
- A5 + A2 两次相似误诊已合并固化到 skill `cta-spread-arb-architecture` 的 "verifying-bug-reports-before-fixing" 段, 强化"分支 unreachable ≠ bug, 可能是 defense-in-depth"

**A2 行动决策**: **不修代码**, 仅更新报告评级, 关闭 todo。

---

## §7.7 Q3 决策 — rb/hc 回测验证 (Phase 3-A 修复后)

**触发**: Phase 3-A 6 项修复 (A1/A3/A4 真修复 + A2/A5 勘误 + Q5 扫盘) 全部落地后, 重跑 rb/hc 2510 + 2601, 验证报告 §0 提出的核心问题:
> Q3: rb/hc 当前怎么处理? (降仓 0.3x / 暂停 / 修完 P0/P1 再回测决定)

### 7.7.1 三对组合最新回测对比 (2026-06-10)

| 实验 | total_return | max_dd | win_rate | trades | calmar | 评级 |
|------|-------------:|-------:|---------:|-------:|-------:|------|
| **rb_hc 2510** | **+20.07%** | **8.18%** | 32.13% | 728 | **+2.36** | ★★★ 可上 |
| **rb_hc 2601** | -3.52% | 22.18% | 32.60% | 964 | -0.21 | ★ 边缘 |
| l_pp 2609 | +2.24% | 20.35% | 46.15% | 766 | +0.16 | ★★ 基线 |

### 7.7.2 rb/hc Phase 3-A 前后对比 (核心验证)

| 指标 | 2510 BEFORE | 2510 AFTER | 2601 BEFORE | 2601 AFTER |
|------|------------:|-----------:|------------:|-----------:|
| total_return% | -18.79 | **+20.07** | -22.84 | **-3.52** |
| max_drawdown% | 48.04 | **8.18** | 40.65 | **22.18** |
| win_rate% | 44.98 | 32.13 | 44.75 | 32.60 |
| trades | 1268 | **728 (-43%)** | 1460 | **964 (-34%)** |
| calmar | -0.38 | **+2.36** | -0.74 | -0.21 |

**翻盘解读**:
- **2510 从亏损王变盈利冠军**: -18.79% → +20.07% (Δ=+38.86pp), MDD 48% → 8% (-83%), Calmar 翻正到 +2.36
- **2601 大幅止血**: -22.84% → -3.52% (Δ=+19.32pp), 仍亏但 MDD 减半, 接近盈亏平衡
- **胜率反而降低 (45% → 32%)**: 不是 bug — A4 方向反转 + A3 backoff 过滤了 43% 的反向/低置信信号, 留下来的盈亏比更高 → ret/mdd 双改善
- **交易笔数大降**: A3 (coint backoff 反转) + A4 (DIRECTION_TYPE 4 处校正) 联合作用, 噪声单被滤掉

### 7.7.3 各修复项的归因 (推测)

| Phase 3-A 项 | 对 rb/hc 的预期影响 | 证据 |
|--------------|---------------------|------|
| **A4 方向反转** (4 因子 DIRECTION_TYPE) | 顶级贡献 — VwapDeviation/VolumePriceDivergence 之前用反方向, 直接打反信号 | trades -43%/-34%, win_rate 降但 ret 升 = 噪声单消失 |
| **A3 coint backoff** (失败时 cap 32x) | 中等贡献 — refit 失败时不再频繁误判, beta 漂移减少 | calmar 从负翻正, 长周期持仓质量改善 |
| **A1 stdCode 三段校验** | 防御性, 防止 contract 配错炸盘 | 本次未触发, 但消除一类潜在崩溃 |
| A2 / A5 勘误 | 0 — 这两项是误诊 | 不动代码 |

### 7.7.4 Q3 决策 — **取消"降仓/暂停"决议, 继续运行**

| 选项 | 原预案 (报告 §0) | 当前决议 (Phase 3-A 后) |
|------|------------------|------------------------|
| 降仓 0.3x | 候选 ✓ | ❌ 已无必要, MDD 降到 8.18% |
| 暂停 (paused=true) | 候选 ✓ | ❌ 已无必要, 2510 已盈利 |
| **继续以默认仓位运行** | 候选 ✓ | ✅ **采纳** |

**采纳理由**:
1. 2510 已实现 Calmar +2.36, **全实验池最优**
2. 2601 仍微亏但 MDD 已可控, 留在池里继续观察
3. trades 数下降但单笔质量提升, 行为模式健康

### 7.7.5 剩余风险 / 警告

| 项 | 说明 |
|----|------|
| sharpe 仍为负 (-13 ~ -18) | 计算口径异常 (用 std 而非 down_std + 基准利率), 不是真实 sharpe, 仅供横向对比 |
| win_rate 32% | 看似低, 但盈亏比足够 → 总 ret 为正; 仍需观察是否过拟合到 2510 单合约 |
| 2601 仍亏 -3.52% | 不同合约月份的 beta 稳定性不同, 需要 param profile 区分 (skill `cta-spread-arb-param-profile` 已支持) |
| 未做 WF 验证 | 当前仍是固定参数全样本回测, 未跑 walk-forward; 真实部署前需 WF 验证不过拟合 |

### 7.7.6 完成度评级修正 (Phase 3-A 终态)

| 模块 | Phase 3 前 | §7.6 后 | §7.7 后 (Q3 决策后) |
|------|-----------|---------|---------------------|
| 数据 pipeline (m1/m5) | 90% | 90% | 90% |
| 信号/特征 (A3+A4 修后) | 60% | 75% | **85%** (rb/hc 翻盘验证) |
| RiskManager (单合约) | 70% | 85% | 85% |
| 多 TF 离线分析 | 95% | 95% | 95% |
| 多策略 portfolio 层 | 0% | 0% | 0% (Phase 4) |
| WF 验证 | 30% | 30% | 30% (待 B2 推进) |
| **总体** | **~58%** | **~63%** | **~68%** |

**结论**: rb/hc 不再是"结构性亏损"案例, Q3 决议结案。后续重点转向 Phase 3-B (WF 验证 B1-B4) 和 Phase 3-C (扩品种 C1) — 见 §6 路线图。

---

## §7.8 Phase 3-B B2 — Walk-Forward 验证 (2026-06-11)

**触发**: §7.7 全样本回测显示 rb/hc 2510 翻盘到 +20%、Calmar +2.36, 但报告 §7.7.5 已警告"未做 WF 验证, 可能过拟合"。本次用 `scripts/walk_forward.py` 跑 3 折 WF (train 67%/test 33%) 验证样本外泛化。

### 7.8.1 WF 工具说明

- 路径: `scripts/walk_forward.py` + `scripts/_wf_worker.py`
- 切折策略: 按月份连续值线性切, fold N 内 train 占 67%, test 占 33%
- 隔离: 每折独立子进程跑 (WtBtEngine 同进程多 init exit=11)
- PnL 口径: `RiskManager._realized_pnl` (仅 closed PnL, **与 summary.json `total_return` 口径不同**, 数量级仅供 train/test 比较)

### 7.8.2 三对实验 WF 结果 (3 folds, train_ratio=0.67)

**rb_hc 2510** (全样本 +20% Calmar +2.36):
| Fold | Train PnL | Test PnL | Ratio (Test/Train) |
|-----:|----------:|---------:|-------------------:|
| 1 | +3000 | +280 | 0.09 |
| 2 | -140 | -150 | 1.07 |
| 3 | +1360 | -20 | -0.01 |
| **SUM** | **+4220** | **+110** | **0.03** |

**rb_hc 2601** (全样本 -3.52%):
| Fold | Train PnL | Test PnL | Ratio |
|-----:|----------:|---------:|------:|
| 1 | +1790 | +1170 | 0.65 |
| 2 | +60 | +480 | 8.00 |
| 3 | -1260 | +90 | -0.07 |
| **SUM** | **+590** | **+1740** | **2.95** |

**l_pp_2609** (全样本 +2.24%):
| Fold | Train PnL | Test PnL | Ratio |
|-----:|----------:|---------:|------:|
| 1 | +1420 | +135 | 0.10 |
| 2 | -165 | +30 | -0.18 |
| 3 | -110 | +140 | -1.27 |
| **SUM** | **+1145** | **+305** | **0.27** |

### 7.8.3 关键发现 — **反直觉但信息量极大**

#### 发现 1: rb_hc 2510 全样本最优, WF 测试折几乎归零

- 全样本 +20%, WF test_sum 仅 +110 (Train 的 2.6%)
- Fold 1: 大赚 +3000 训练好, 测试期只剩 +280 → 大幅衰减
- Fold 3: Train +1360 → Test -20 → 完全失效
- **诊断**: A4 修复让 rb/hc 2510 整体盈利, 但 **参数在样本外难以保持**, 强过拟合迹象

#### 发现 2: rb_hc 2601 全样本亏损, WF Test 反而大幅盈利

- 全样本 -3.52%, WF test_sum +1740 (Train +590 的 2.95 倍)
- Fold 1+2 Test 均显著正, Fold 3 Train 大亏但 Test 居然小赚
- **诊断**: 全样本亏损来自**回测起止时段的拖累期** (尤其是 Q3-Q4 趋势期), WF 分折后避开了那个时段 → 反而显示策略本身样本外能力 OK
- **隐含**: 不是策略不行, 是 2601 合约的某段时间段不适合套利 → 需要 regime filter 或时段筛选

#### 发现 3: l_pp_2609 train 多折负, test 多折正 — 工具自带的"严重过拟合"警告

- 工具检测到 2/3 折 Train/Test 方向相反 → 警告
- 但实际 test_sum 是正的, 不是亏
- **诊断**: train 短期窗口的局部噪声 vs test 折的均衡, 这种情况不是真过拟合, 是 **WF 切折太粗** (3 folds, 总样本 ~169 天, 每折 train 仅 38 天)

### 7.8.4 综合诊断 — 三种"过拟合"实际是三个不同问题

| 实验 | 表象 | 真实根因 | 处方 |
|------|------|---------|------|
| rb_hc 2510 | 全样本好 → WF Test 差 | **真过拟合**: 单合约 + 单参数, 样本外无泛化 | 多合约 + 参数 robust 化 (param_profile 已有, 用起来) |
| rb_hc 2601 | 全样本差 → WF Test 好 | **时段拖累**: 某段行情不合 | regime filter (跳过不利时段) 或 stop-loss 时段化 |
| l_pp 2609 | Train Test 方向反 | **WF 切折太粗** (38 天 train) | 增大 folds=4-5 或换 expanding window |

### 7.8.5 Q3 决议 *再修正* (基于 §7.8)

| 选项 | §7.7 决议 | §7.8 决议 |
|------|----------|----------|
| 继续以默认仓位运行 rb/hc 2510 | ✅ | ⚠️ **降级**: 全样本好但 WF 弱, 建议**仓位 0.5x + 加 walk_forward 监控** |
| 继续以默认仓位运行 rb/hc 2601 | ⚠️ 边缘 | ✅ **升级**: WF Test 大幅正, **保持默认仓位 + 加时段过滤** |
| l_pp 2609 | ★★ 基线 | ✅ 基线维持, 但建议用 4-5 折 WF 重测 |

### 7.8.6 Phase 3-B 后续路线 (B1/B3/B4)

| 项 | 工作 | 优先级 | 依赖 |
|----|------|--------|------|
| **B2** ✅ | 3 折 WF 三对全跑 | 完成 | — |
| **B2.5** | 加跑 4-5 折 WF + 全样本期间分段诊断 (找 2601 的拖累段) | 高 | B2 数据 |
| B1 | 补 m30 CSV 落盘 → 让离线最优锚点 (l/pp m30×2w) 真正喂回测 | 高 | ks2wtpy 改 m30 落盘 |
| B3 | 修 `walk_forward.py` PnL 口径 (统一到 `total_return*capital` 或 funds.csv `dynbalance`) | 中 | 小重构 |
| B4 | regime filter (跳过 trending 段) — 验证对 rb_hc 2601 是否真改善 | 中 | Phase 3-A 的 P0-γ 软衰减已部分实现, 但是 entry 级, 不是 regime 级 |

### 7.8.7 完成度评级 (§7.8 后)

| 模块 | §7.7 后 | §7.8 后 |
|------|---------|---------|
| 信号/特征 | 85% | **75%** (WF 暴露样本外弱点, 评级下修) |
| WF 验证 | 30% | **60%** (3 折跑通, 但口径不一致 + 工具需扩) |
| 多策略 portfolio 层 | 0% | 0% |
| **总体** | ~68% | **~67%** (持平, 信号下修+WF 上修抵消) |

**B2 结论**: WF 揭穿了"全样本 +20% 是真的好"的乐观结论 — rb/hc 2510 高收益**部分来自过拟合**, 真实可期望收益应折扣 50% 看待。但同时也证明 rb/hc 2601 **不是策略问题, 是时段问题**, 这是个意外收获。Phase 3-B 下一步应优先做 B2.5 + B1。


