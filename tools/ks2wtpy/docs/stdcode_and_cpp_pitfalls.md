# stdCode 三段规范 & WT C++ Loader 陷阱清单

> 适用范围：`tools/ks2wtpy` 转换出的 CSV / DSB / LMDB / basefiles 在 WonderTrader
> C++ 引擎（WtCore / WtUftCore / WtBtCore / WtDataStorage / WtDtServo / WtDtHelper）
> 中的读取与索引。
>
> 权威源：`wondertrader/src/Share/CodeHelper.hpp`、
> `wondertrader/src/WtDataStorage/WtBtDtReader.cpp`、
> `wondertrader/src/WtBtCore/HisDataReplayer.cpp`。
>
> 本文档**只描述事实**，不发明新约定；所有规则都能在上述 C++ 源文件中
> 直接定位，并已在 `tools/ks2wtpy/tests/test_wt_readback.py` /
> `tests/test_ks2wtpy_regression.py` 中以测试形式锁定。

---

## 1. 三层代码空间（最重要：先分清在哪一层）

WT 中**同一根合约**在不同层级有完全不同的写法。混层是导致
"WtDtServo.get_bars 永远返回 None"、basefiles 加载失败、回测引擎 segfault
的头号根因。

| 层级       | 形态              | 用在哪里                          | 例子                       |
| ---------- | ----------------- | --------------------------------- | -------------------------- |
| 策略层     | **三段** stdCode  | Python 策略、订阅、订单、信号     | `SHFE.rb.HOT`、`CFFEX.IF.2509`、`CZCE.AP.610` |
| 物理文件   | **不带点** 文件名 | `his/<period>/<EXCHG>/<code>.dsb` | `rb_main.dsb`、`IF2509.dsb`、`AP610.dsb` |
| basefiles  | **品种 ID**       | `commodities.json` / `contracts.json` 键 | `SHFE.rb`（commodities），`rb2509`（contracts） |

记忆口诀：
- 策略只看到三段；
- DSB 只看到文件名；
- basefiles 拿品种+合约号做哈希键。

三段与 DSB 的桥梁是 `CodeHelper::extractStdCode`（C++ 端唯一权威解析器），
经它把三段拆成 `_exchg / _product / _code / _ruletag`，然后由
`WtBtDtReader::read_raw_bars` 用 `_code` 拼出物理路径。

---

## 2. 策略层 stdCode 三段规则（C++ 权威）

来源：`Share/CodeHelper.hpp` 第 586-658 行 `extractStdCode`。

形态：`EXCHG.product.contract`（点号分隔 3 段，期权除外）。

**`extractStdCode` 解析顺序**（按 C++ 注释逐条复现）：

1. **期权先判**：若 `isStdChnFutOptCode(stdCode)` 命中，走
   `extractStdChnFutOptCode` 单独解析。期权命名见 §5。
2. **复权后缀**：若最后一段以 `-`（前复权）或 `+`（后复权）结尾，
   先剥离，置 `_exright = 1/2`，再继续判合约号。
   - 例：`SSE.STK.600000-` → `_exright=1, _code="600000"`
3. **分月合约**：最后一段长度 = 4 且末位是数字，认定为分月合约。
   - 此时 `_code = product + 月号`，CZCE 例外，**去掉首位**。
   - `CFFEX.IF.2509` → `_code = "IF2509"`
   - `CZCE.AP.2610` → `_code = "AP610"` ← 去首位
4. **HOT / 2ND / 自定义规则**：若最后一段不是分月格式，
   交给 `hotMgr->getRuleTag(ext)` 查规则。命中规则 →
   `_code = product, _ruletag = ruleTag`；否则原样作为 `_code`。
   - `SHFE.rb.HOT` → `_code = "rb", _ruletag = "HOT"`
   - `SHFE.rb.2ND` → `_code = "rb", _ruletag = "2ND"`
   - `BINANCE.DC.BTCUSDT` → `_code = "BTCUSDT"`（普通三段）

关键事实：
- **product 段必须是纯字母**。`_is_wt_std_code`（acceptance/wt_readback.py:67）
  以此为 gate；`SHFE.rb2510`（两段合并形式）是非法策略 stdCode，
  会让 C++ loader 在某些路径下崩溃。
- **HOT/2ND 是规则标签，不是合约号**。运行时由 `hotMgr` 通过
  `hots.json` 在 `extractStdCode` 之后的 `getCustomRawCode(ruleTag, stdCommID, date)`
  动态映射到当日的 raw 合约（见 `HisDataReplayer.cpp:3238-3245`）。
- **两段形态保留给非分月币对**：如 `OKEX.BTC-USDT`（`extractStdCode` 第 608-616 行）。
  期货品种不允许两段。

---

## 3. 物理文件层（DSB 路径）规则

来源：`WtDataStorage/WtBtDtReader.cpp:70`：

```cpp
ss << _base_dir << "his/" << PERIOD_NAME[period] << "/"
   << exchg << "/" << code << ".dsb";
```

- 根目录：`{output_dir}/his/`
- period 名：`day` / `min1` / `min5`（对应 `kline_1d / kline_1m / kline_5m`）
- exchg 子目录：`SHFE / DCE / CZCE / CFFEX / INE / GFEX`
- 文件名：`{code}.dsb`，**`code` = `CodeInfo._code`，不带 EXCHG 前缀，不带点**

ks2wtpy 端对应实现：`dsb_writer.py:_dsb_path()`（第 219-227 行），并已在注释
明确"若加 EXCHG 前缀（如 `SHFE.rb0909.dsb`），`WtDtServo.get_bars` 永远返回 None"。

主力线 / 指数线的 raw code：
- 主力（HOT 展开后的 raw）：`rb_main.dsb`、`AP_main.dsb`（ks2wtpy 现状是
  KS 主力序列直接落 `{product}_main` raw）
- 指数（_index）：`rb_index.dsb`、`AP_index.dsb`
- 具体分月：直接用 `extractStdCode` 拆出来的 `_code` 落文件名
  （`rb2509.dsb` / `AP610.dsb` ←CZCE 已去首位）

---

## 4. basefiles 层规则

- **commodities.json**：键是 `EXCHG.product`，值含 `category(int 1)`、`session`、
  `holiday="CHINA"` 等。`session` 必须是 WT 8 个官方 ID 之一：
  `FD0900 / FD0915 / SD0930 / FN0100 / FN0230 / FN2300 / FN2330 / TRADING`。
  - WT 现场命名按"夜盘收盘时刻"，不是按开盘。`FN0230` = 夜盘收 02:30。
- **contracts.json**：键是合约号 raw（与 DSB 文件名一致：`rb2509` / `AP610`），
  下挂 `name / exchg / product / maxlimitqty / maxmarketqty / pricetick` 等。
- **hots.json / seconds.json**：嵌套结构
  `{EXCHG: {product: [{date, from, to, oldclose, newclose}, ...]}}`。
  日期是切换那天的整数，`from / to` 写的是 raw 合约号（不带 EXCHG）。
- **holidays.json**：顶层必须有 `"CHINA"` 数组，WT 原版自带数据只到 2022；
  2023-2026 由 ks2wtpy `references/extra_holidays_2023_2026.json` 维护（手维国务院发文）。
- **fees.json**：WT 标准字段 `{open, close, closetoday, byvolume:bool}`，
  键同 commodities 用 `EXCHG.product`。

加载入口（C++ 端）：`HisDataReplayer.cpp:266-283`，按
`commodities / contracts / holiday / hot / second / rules` 顺序读，
任何一个文件路径配错都直接 fail-fast。

---

## 5. 期权命名

- DCE / SHFE：`m2609-P-2950`、`au2510-C-820`（带 `-P-` / `-C-`）
- CZCE：`SR109P16800`、`CF2111C22000`、`AP610C7000`（中段没有连字符）
- CZCE 策略组合：`{product}{月号3位}[CPM]SP{strike}`，如 `SR605MSP5400`
  （`MSP` = 月度 Spread / `CSP` = Call Spread / `PSP` = Put Spread）。
  acceptance 白名单正则：
  `^[A-Z]+\.[a-zA-Z]+\d{3,4}[CPM]SP\d+_[dm]\d*\.csv$`
- 期权检测：`is_option_code` 正则 `\d+[CP]\d{3,}`
  （`ks_reader.py:551` / `s3_reader.py:377`）

`extractStdCode` 对期权走单独分支 `extractStdChnFutOptCode`，与上面
四步法的常规期货分支互斥。

---

## 6. CZCE 去首位（最易踩）

C++ 源 `Share/CodeHelper.hpp:638-641`：

```cpp
auto i = wt_strcpy(codeInfo._code, codeInfo._product);
if (memcmp(codeInfo._exchg, "CZCE", 4) == 0)
    wt_strcpy(codeInfo._code + i, ext + 1, extlen - 1);  // 跳过首位
else
    wt_strcpy(codeInfo._code + i, ext, extlen);
```

策略侧传 `CZCE.AP.2610` 是合法的——`extractStdCode` 会自动把 `_code` 解析成
`AP610`（去掉 `2`）。但**反过来 ks2wtpy 在生成 CSV/DSB/contracts 时**，
必须主动把月号写成 3 位：

- DSB 文件名：`AP610.dsb`（不是 `AP2610.dsb`）
- CSV 文件名：`CZCE.AP610_d.csv`
- contracts.json 键：`AP610`
- hots.json `from/to` 字段：`AP610` / `AP611`

ks2wtpy 已在 `config_generator.py:163-206 parse_contract_code` 中处理
新旧两种命名（4 位 / 3 位）并归一为产出 3 位月号；测试 `test_wt_readback.py:103`
锁定这一约定。

> 易踩点：从外部 future_code_list（GBK 编码！）拿到的 CZCE 合约通常已是
> 3 位月号；不要二次"补 4 位"，否则 C++ 端二次去首位会丢真正的月份信息。

---

## 7. HOT 展开机制（不是字符串替换）

- ks2wtpy 端：CSV 写主力是 `{EXCHG}.{product}.HOT_{period}.csv`
  （`csv_writer.py:17`），DSB 端直接落 `{product}_main.dsb` raw 文件。
- 策略端：写 `SHFE.rb.HOT` 是给 `hotMgr` 看的"模板"，不是文件名。
- 运行时：`HisDataReplayer.cpp:3238-3245`

  ```cpp
  CodeHelper::CodeInfo cInfo = CodeHelper::extractStdCode(stdCode, &_hot_mgr);
  std::string rawCode = cInfo._code;
  if (strlen(cInfo._ruletag) > 0)
      rawCode = _hot_mgr.getCustomRawCode(cInfo._ruletag, cInfo.stdCommID(), uDate);
  ```

  也就是说 `SHFE.rb.HOT` 在给定 `uDate` 下被换成当日的真实 raw 合约（如 `rb2509`），
  再去找 `his/day/SHFE/rb2509.dsb`。

- 必备前提：`hots.json` 中必须有该 (EXCHG, product) 在 `uDate` 区间内的记录，
  否则 `getCustomRawCode` 返回空、loader 拿到空字符串去拼 `.dsb`、读取失败。
- ks2wtpy 现状：DSB 端**不依赖 hots.json**，直接把 KS 上游打包好的
  `{product}_main` 序列落成一个独立 DSB（采用方案 A：纯 OI 最大启发）。
  这与 WT 原生 hots.json + raw 合约的玩法**并行**，策略侧若想用 `rb.HOT`
  必须同时具备 hots.json 和对应 raw DSB。

---

## 8. `WtDtServo.setStorage` 路径

`WtDtServo.setStorage(path)` 必须传**含 `his/` 子树的根目录**，不是
`his/day/<EXCHG>/`。`WtBtDtReader::read_raw_bars` 内部自己拼
`{base_dir}/his/{period}/{exchg}/{code}.dsb`。

singleton 行为：一个 Python 进程内 `WtDtServo` 只能 `setStorage` 一次。
扫描 / 切换不同根目录必须用 subprocess 隔离，否则 silently 用第一次的路径。
acceptance `_wt_readback_worker.py` 就是为此设计——每次扫描 fork 一个干净进程。

---

## 9. ks2wtpy 现状速查

| 关注点 | ks2wtpy 输出 | C++ 看到 |
| -- | -- | -- |
| 策略代码（CSV 文件名）| `SHFE.rb2509_d.csv` `SHFE.rb.HOT_d.csv` `CZCE.AP610_d.csv` | CSV 是 ks2wtpy 中间产物，C++ loader 不直接读 |
| DSB 路径 | `his/day/SHFE/rb2509.dsb` `his/day/SHFE/rb_main.dsb` `his/day/CZCE/AP610.dsb` | `WtBtDtReader` 直接命中 |
| commodities | `SHFE.rb`（key） `category=1, session="FN0100", holiday="CHINA"` | basefiles 加载成功 |
| contracts | `rb2509`（key） | 与 DSB 文件名一致 |
| hots.json | 默认不生产（DSB 已落 `_main`） | 策略写 `rb.HOT` 时需要补全 |
| sessions.json | WT 8 ID 全集 + GBK 名称 | 全部命中 |
| holidays.json | 顶层 `CHINA`，含 2023-2026 国务院数据 | `_bd_mgr.loadHolidays` 通过 |

---

## 10. 高频坑点 & 自检 cheat-sheet

| 症状 | 真正原因 | 自检 |
| -- | -- | -- |
| `WtDtServo.get_bars` 返回 None / 空 | DSB 文件名带 EXCHG 前缀，或不在 `his/<period>/<EXCHG>/` 下 | `ls his/day/SHFE/ \| head` 看是不是 `rb2509.dsb` 而不是 `SHFE.rb2509.dsb` |
| basefiles 加载报 "session not found" | commodities.session 用了非 WT 8 ID | `python -m acceptance --wt-readback` 跑 fail 看 details |
| `rb.HOT` 跑不出数据但具体 `rb2509` 可以 | hots.json 缺该 (EXCHG, product) 当日映射 | 看 `hots.json["SHFE"]["rb"]` 是否覆盖测试日期 |
| CZCE 老合约写 4 位月 → loader segfault | 没有去首位，C++ 端会二次去首位 | grep `AP[0-9]\{4\}.dsb` `his/day/CZCE/`，应该一个都没有 |
| holiday 命中失败 / `loadHolidays` warn | 顶层 key 不是 `CHINA`，或 2023-2026 缺 | `jq '.CHINA \| length' holidays.json` 应 > 0；ks2wtpy 自带 extra |
| 同一进程切两次根目录后旧数据 | `WtDtServo` singleton 缓存 | 一定要 subprocess fork（`_wt_readback_worker.py` 范例）|
| 期权合约被当普通分月解析 | 名字命中 `\d+[CP]\d{3,}` 没走期权分支 | `_is_wt_std_code` 不应放行；CSV 端走 `is_option_code` |
| commodities.json `category` 写成字符串 `"1"` | basefile 校验失败 | 必须 `int(1)`，已在 generator 中固定 |
| `extractStdCode` 返回 `_code=""` | 传了两段合约（`SHFE.rb2510`）非分月币对 | 策略代码必须三段，acceptance `_is_wt_std_code` 已 gate |

---

## 11. 引用文件 & 测试锚点

权威 C++ 源：
- `wondertrader/src/Share/CodeHelper.hpp:586-658`（`extractStdCode`）
- `wondertrader/src/Share/CodeHelper.hpp:21-25`（`SUFFIX_HOT / SUFFIX_2ND`）
- `wondertrader/src/Share/CodeHelper.hpp:638-641`（CZCE 去首位）
- `wondertrader/src/WtDataStorage/WtBtDtReader.cpp:68-82`（DSB 路径拼接）
- `wondertrader/src/WtBtCore/HisDataReplayer.cpp:266-283`（basefiles 加载顺序）
- `wondertrader/src/WtBtCore/HisDataReplayer.cpp:3238-3245`（HOT raw 反查）

ks2wtpy 端落地：
- `tools/ks2wtpy/standardizer.py`（`std_code / minute_bar_time / wt_day_time`）
- `tools/ks2wtpy/dsb_writer.py:219-227`（`_dsb_path` 严格匹配 C++）
- `tools/ks2wtpy/csv_writer.py:14-22, 237-242`（CSV 命名）
- `tools/ks2wtpy/config_generator.py:163-206`（合约号解析，含 CZCE 3/4 位）
- `tools/ks2wtpy/acceptance/wt_readback.py:67-78`（`_is_wt_std_code` gate）
- `tools/ks2wtpy/acceptance/_wt_readback_worker.py`（subprocess 隔离 setStorage）

锁定测试：
- `tools/ks2wtpy/tests/test_wt_readback.py:31, 103-111`
  （`CZCE.AP.HOT` / `CZCE.AP.610` 反推）
- `tools/ks2wtpy/tests/test_ks2wtpy_regression.py:896-911`
  （CZCE 3 位月反推三段，RED→GREEN 已锁）
- `tools/ks2wtpy/tests/test_ks2wtpy_regression.py:1184-1252`
  （basefiles 派生：maxlimitqty / maxmarketqty / margin）

---

## 12. 修改本文档须遵守

1. 不引入新约定。所有规则必须能在 C++ 源 / 已有 ks2wtpy 测试中直接定位。
2. 修订 §1-§8 前请同步对应测试；若 C++ 源更新了 `extractStdCode`，
   优先更新本表然后再调 ks2wtpy。
3. ks2wtpy 现状栏（§9）会随转换器演进；任何 CSV / DSB / basefiles 输出
   形态变化都必须更新本表后再合并。
