# 反向 net/gross 订单级归因：问题语义、userTag 的本义、与零 C++ 改动替代方案

> 2026-10-29 分析。结论先行：**C++ userTag 不是唯一解，也不是当前最优解**。
> 生产环境已有的三条零侵入数据链（outbox 意图落盘 + EventNotifier 订单
> 事件侧车 + trades.csv 权威落盘）拼起来就是一条完整的
> "意图 → localid → 逐笔成交"归因链。反向 net/gross 的订单级归属可以在
> 纯 Python 层实现，C++ userTag 降级为"实证错误率不可接受时的加固项"。

---

## 1. 问题语义：什么叫"反向 net/gross 订单级归属"

### 1.1 状态空间：两套账，一个标量

| 层 | 形态 | 内容 |
|---|---|---|
| 引擎/profile 层（策略内存） | **向量** | S1 持仓 10 多、S2 持仓 4 空（gross），各带层价/冻结 N/止损位 |
| 框架信号簿（CtaStraBaseCtx） | **标量** | `stra_set_position` 只认每 code 一个净目标；`_pos_map` 存净仓 |
| 券商柜台（rtdata.json / 真实） | **两本** | 真实多头簿 + 真实空头簿（CL/CNL/CS/CNS 动作类型） |

执行单元（WtLocalExecuter → WtMinImpactExeUnit）每次只收到一个**净目标
变化**，把它翻译成一张或多张订单。归因问题 = 把柜台回来的逐笔成交
（只有 code/action/volume/price，**没有 profile 标签**）分配回引擎层的
profile 向量分量，使得：

1. 每个 profile 的层数/层价/止损位按真实成交价修正（re-anchor）；
2. 出场确认按 profile 精确关闭（confirm_exit 的价格与风险账）；
3. 幽灵判定（ghost_fill）知道**哪一层**没到账。

### 1.2 为什么"净标量证据"在数学上不可解

现有 `fill_confirmed` 的账户真值证据是**净仓增量**（rebuild 模式下
`truth_net(code) - truth0`）。净增量是向量变化的投影，投影多对一：

```
场景 A（同向双 profile，部分出场 —— 当前用时序分配硬扛）
  S1 多 10 + S2 多 4，净 +14。S1 出场 → 净目标 4。
  柜台: CL 10 手。净增量 -10 ✓ 守恒。
  但"CL 10 全部是 S1 的"是记账约定，不是观测事实；
  成交价回写按 S1/S2 的确认顺序逐笔分配（layer_price 的 skip 口径），
  一旦同一窗口内 S2 也有加仓成交，顺序假设就承载了两个未知量。

场景 B（反向并存，单边出场 —— action 类型可救一半）
  S1 多 10 + S2 空 4，净 +6。S1 止损 → 净目标 -4。
  柜台: CL 10 手（卖平多）。islong/isopen 动作类型能区分
  "平多"≠"平空"，所以这一半靠 trades.csv 的 action 列可解。

场景 C（跨零反向 —— 当前直接 UNKNOWN，`_process_dual_exit` 的 mixed 分支）
  S1 多 10 + S2 空 4，净 +6。同批 S1 出场 + 新开 S2 反向 → 净目标 -10。
  净变化 -16 的 profile 分解不唯一：
    分解 1: S1 -10（平多）, S2 0, 另有 -6 新开空（谁的？）
    分解 2: S1 -10, S2 -4（平空）, 新开 -10 空（谁的？）
  两种分解柜台订单流不同（CL10+OS6 vs CL10+CS4+OS10），
  但策略在提交时只发了一个净目标变化——**它的含义本来就依赖
  执行单元内部的多空簿簿记规则**。没有订单级观测，恢复路径无法
  判性，只能 UNKNOWN 封锁。

场景 D（共享账户串扰 —— alert 模式的弃用原因）
  同 code 若有第三方策略成交，净增量可能恰好等于本层手数。
  2026-09-15 实测 TA 账户 112 vs 本策略 64 → 绝对量证据已被判废。
```

核心论断：**归因 = 把标量/类型化的成交流映射回向量意图。任何不经过
"订单身份"的映射都依赖时序假设，而时序假设在崩溃恢复、分批成交、
同批多 profile 三个场景下同时受压。** 这就是"订单级归属"的意义：
`localid` 是唯一把"柜台事实"和"策略意图"钉在同一根钉子上的键。

## 2. C++ userTag 方案的本义与真实成本

### 2.1 本义（A5-2）

让 CTA 信号携带的 tag 穿透到订单：`append_signal` 的 tag →
`WtLocalExecuter` 建单时填入 `OrderStruct.userTag` → CTP 回报带回 →
TRD_TRADE / trades.csv 逐笔成交都带策略自定义标签 → profile 归因
零推断。

### 2.2 三个常被忽略的前提

1. **userTag 只在"每 profile 独立订单"时才有区分度**。若维持现状
   "一个净目标变化一张订单"，一张订单同时服务两个 profile，订单上
   贴什么标签都无法拆分它的成交。所以 userTag 方案**强制配套**：
   策略必须按 profile 拆分提交（跨零/mixed 场景拆成两笔净目标变化，
   顺序提交）。
2. **它解决的是"标签随订单走"，不解决"订单与意图的对应"**——后者
   由提交顺序天然保证（见 §3.2）。换言之 userTag 替换掉的只是
   §3 算法里的"签名匹配"一步，而不是整条链。
3. **成本是硬的**：补丁 + 重编 libWtCore.so、与上游 wtpy 0.9.9.3
   二进制分叉、生产部署回滚面扩大；而 §3 的全部数据链**今天已经在
   生产运行**（A5-1 侧车 10-21 已部署，trades.csv 一直落盘，outbox
   10-29 刚落地）。

## 3. 零 C++ 改动替代：localid 归因链

### 3.1 已存在的数据链（无一需要改 C++）

| # | 数据源 | 字段 | 角色 | 状态 |
|---|---|---|---|---|
| 1 | outbox `_open_intents` / `_exit_orders`（v4 快照） | profile, kind, net before→after, decision(price/n), lots, ts, seq, cursor | **意图侧**：我按什么顺序提交了什么净变化 | 10-29 已落地 |
| 2 | EventNotifier 侧车 `exec_YYYYMMDD.jsonl` | TRD_ORDER: trader,time,**localid**,code,**islong,isopen,istoday**,total,left,traded,price,state; TRD_TRADE: 同键+volume | **订单侧**：柜台每笔订单的身份/方向/性质/部成 | 10-21 已部署生产 |
| 3 | `generated/traders/<id>/trades.csv` | localid,date,time,code,**action(OL/OS/CL/CNL/CS/CNS)**,volume,price,tradeid,orderid | **权威兜底**：TraderAdapter 同步落盘，进程内不丢 | 一直可用 |
| 4 | rtdata.json | 多空两簿 + 资金 | 批末守恒校验 | 一直可用 |

关键洞察：**订单事件自带 islong/isopen/istoday**——动作类型不需要
Python 复算，事件自己告诉我们。需要复算的只剩"这个净变化对应几手
什么方向的订单"，即执行单元簿记规则的纯函数镜像。

### 3.2 为什么"提交顺序 == 建单顺序"成立

- 信号在 `on_tick` 内被 **FIFO 消费**（CtaStraBaseCtx:811 口径，
  2026-09 源码复核已确认）；
- 每个策略实例持有**独立执行器**（12 品种预热实测：每策略每 code
  各建各的单元），同策略同 code 的订单创建严格按信号顺序；
- 同一 net 目标变化 → 执行单元**一张订单**（分批是部成不是拆单；
  10-09 MA 平仓事故的形态是"一张订单的 fill 分批到达"）。

所以：**同一 (trader, code) 内，第 k 个未匹配订单事件 == 第 k 个
未匹配意图**。不需要时间窗猜测，不需要 userTag。

### 3.3 归因算法（纯逻辑，可单测）

```
状态: 每 (trader, code) 一条 FIFO 队列，元素 = 待归因意图
      {iid, profile, kind, delta_net, predicted_sig, ts}

on_intent(意图):
    sig = predict_sig(unit_book, delta_net)   # 纯函数镜像簿记规则:
    #   新多目标>旧多簿 → 开多 buy vol=差;  空簿同构;
    #   目标穿越两簿 → 拆成 (平多 x, 开空 y) 两个期望签名
    #   （mixed 场景在意图侧就拆开，与执行单元实际拆单同构）
    unit_book 镜像随 predict 同步推进
    入队

on_order_event(ev):                     # 侧车/TRD_ORDER
    if ev.trader/code 不在队列头匹配:
        若签名与队列头 predicted_sig 相容(islong/isopen/vol/±时间窗):
            绑定 localid→iid; ev.traded 进展即更新意图进度
        否则: 跳过 (其他策略/手工单) —— 队列头保持, 等下一事件
    撤单/重挂事件 → 旧 localid 解绑, 新 localid 重绑队首

on_trade(fill):                          # TRD_TRADE 或 trades.csv
    按 localid 找到意图 → 该 profile 的确认进度 += volume
    进度 ≥ 意图手数 → 走现有 confirm 链 (confirm_enter/add/exit
    + 精确 vwap 回写), 出队

批末守恒校验:
    Σ 已归因成交手数(按方向) == 账户真值多空簿变化
    不等 → 全部保留 UNKNOWN + `attribution_mismatch` 告警
    （宁可封锁, 不假确认 —— 与现有 UNKNOWN 哲学一致）
```

### 3.4 与现有证据链的兼容关系

- 不是替换，是**升级第三证据级**：现有 `fill_confirmed` 的
  truth → trades(时序+skip) → fees 三级链保留为降级路径；
  localid 归因成功时给出 `src="orders"` 的精确归因（profile 级
  vwap、部成进度、订单状态），失败时回落现有时序口径并标注
  置信度。
- `dual_exit_mixed` 的 UNKNOWN 分支自然消解：mixed 意图在
  `on_intent` 阶段就按执行单元簿记规则拆成两个期望签名，订单
  流回来逐笔认领，不再需要"跨零一律 UNKNOWN"。

## 4. 残余风险登记（诚实边界）

| # | 风险 | 只有 userTag 能根除? | Python 侧缓解 |
|---|---|---|---|
| R1 | 订单改价/重挂产生新 localid，序列错位 | 部分（标签跟随新订单） | 撤单事件解绑重绑 + 批末多空簿守恒校验 |
| R2 | MiniImpact 单元实际拆单行为未源码核验（二进制发行） | 否 | 侧车观测窗实证：连续 N 日比对"每意图订单数"分布，异常即告警降级 |
| R3 | 总线事件丢包（at-least-once 之外的真丢失） | 否 | trades.csv（进程内同步写）做成交侧权威兜底；订单事件缺失时用 action+volume+守恒闭包重建 localid↔成交映射 |
| R4 | 同 tick 两个意图签名完全相同 | 是（标签区分） | 队首顺序假设 + 概率极低（同 code 同向同手数同毫秒）；发生时 UNKNOWN |
| R5 | 同 code 多策略共享执行器 | 否 | 部署不变量：一策略一 code（12 品种现状成立）；共享的是账户不是执行器 |

结论：**只有 R4 是 userTag 的独占价值**，而它发生的必要条件是
同 code 同向同手数的同批双意图——在当前 ceil/floor 半量配置下
S1/S2 手数恰好相等且同向时成立，需要正视。缓解 = 提交侧人为错开
（批内第二个意图延迟一个事件循环提交），或接受 UNKNOWN 封锁。
因此 userTag 的正确定位是：**等 Python 方案跑出实证错误率后，再
决定是否需要**。

## 5. 实施与验证路线（均不改 C++）

1. **core/OrderAttributor.py**（纯逻辑）：§3.3 算法 + 簿记镜像 +
   守恒校验；单测覆盖场景 A~D + R1/R3 降级路径。
2. ** StraTurtle 接线**：`_begin_position_intent` / `begin_exit_order`
   同步喂 attributor（快照持久化，恢复后队列可重建）；成交确认
   优先消费 attributor 结果，`src="orders"`。
3. **侧车已够用**：exec_journal.py 落盘 JSONL 即数据源，仅需
   attributor 离线/在线两种读取模式。
4. **实证门禁（替代故障注入的一部分）**：生产观测窗 2 周，逐笔比对
   attributor 归因 vs 现有"时序分配"归因，偏差 >0.5 tick 的层占比
   即触发告警并维持现状口径；无偏差才放开 mixed 自动确认。
5. 多 profile 生产激活的最后清单随之收敛为：本归因链实证通过 +
   S1+S2 vs S1 组合 A/B + 真实 webhook/OOS 复核。

## 6. 一页结论

- **意义**：net 标量证据无法分解 profile 向量变化（§1.2 四场景）；
  没有订单级归属，反向/跨零场景只能 UNKNOWN 封锁或多层回写欠定。
- **必要性**：订单级归属是必要的；但"订单级"≠"必须 userTag"——
  localid 归因链用现有三条零侵入数据源即可达到同一粒度。
- **C++ userTag**：从"阻断项"降级为"加固项/备选项"。它消除的是
  签名匹配的推断（R4 及簿记镜像的实证风险），代价是二进制分叉与
  部署面；建议先跑 Python 方案 + 观测窗实证，错误率不达标再启动
  A5-2。
