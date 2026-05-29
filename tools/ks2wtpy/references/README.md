# ks2wtpy references

非代码资产 —— 用于 basefiles 生产化的权威快照。

## wt_commodities_snapshot.json

来源: `wondertrader/dist/common/commodities.json` (WT 官方发行模板)

用途:
  ks2wtpy 生成 `common/commodities.json` 时优先从此快照拷贝品种字段
  (category/pricetick/volscale/precision/session/holiday 等), 未覆盖的
  品种 fallback 到内置占位逻辑并打 warning。

升级流程:
  1. 确认 WT 版本: `git -C /path/to/wondertrader log -1 -- dist/common/commodities.json`
  2. 直接覆盖:
       cp /path/to/wondertrader/dist/common/commodities.json \
          tools/ks2wtpy/references/wt_commodities_snapshot.json
  3. 重排 + 规范化(可选):
       python -c "import json; d=json.load(open('tools/ks2wtpy/references/wt_commodities_snapshot.json',encoding='utf-8')); json.dump(d,open('tools/ks2wtpy/references/wt_commodities_snapshot.json','w',encoding='utf-8'),ensure_ascii=False,indent=2,sort_keys=True)"
  4. 跑回归: `pytest tools/ks2wtpy/tests/test_ks2wtpy_regression.py -k commodities`

字段语义参考:
  - `category` int: 1=Future 2=Stock 3=ETF 6=SpotOption (WT 内部枚举; CFFEX 股指期权 IO/HO/MO 用 6, 期货期权用 2=FutOption)
  - `covermode` int: 0=不区分平今/平昨, 1=区分 (上期所/能源中心特有)
  - `pricemode` int: 0=普通 (大商所/郑商所), 1=动态 (上期所/能源中心)
  - `pricetick` float: 最小变动价位 (元)
  - `volscale` int: 合约乘数 (1手对应的标的数量)
  - `precision` int: 价格小数位数
  - `session` str: 引用 sessions.json 中的模板 ID (如 FN0230, SD0930, FD0900)
  - `holiday` str: 引用 holidays.json 中的市场 ID (统一 CHINA)

本地补丁记录 (非 WT 官方模板):
  - 2026-05-29 补 CFFEX 股指期权 IO/HO/MO 三条
    * IO 沪深300股指期权: category=6, pricetick=0.2, volscale=100, session=SD0930
    * HO 上证50股指期权:  category=6, pricetick=0.2, volscale=100, session=SD0930
    * MO 中证1000股指期权: category=6, pricetick=0.2, volscale=100, session=SD0930
    * 来源: 中金所合约规格 (官方); 标的乘数 100 与 IF(300)/IH(300)/IM(200) 期货不同
    * 修复路径: ks2wtpy ConfigGenerator 对纯期权品种从 fallback (volscale=1) 转为走 references 权威值
    * 配套: config.py OPTION_ONLY_PRODUCTS={'CFFEX':['IO','HO','MO']}, ConfigGenerator 走 category=6


## wt_contracts_snapshot.json

来源: `wondertrader/dist/common/contracts.json` (WT 官方发行模板, 922 个合约)

用途:
  ks2wtpy 生成 `common/contracts.json` 时按品种从此快照众数派生
  `maxlimitqty/maxmarketqty/minlimitqty/minmarketqty/longmarginratio/shortmarginratio`
  6 个字段。未覆盖品种 fallback (1000/0/1/1/0.1/0.1) + warning。

升级流程:
  1. 确认 WT 版本: `git -C /path/to/wondertrader log -1 -- dist/common/contracts.json`
  2. 直接覆盖:
       cp /path/to/wondertrader/dist/common/contracts.json \
          tools/ks2wtpy/references/wt_contracts_snapshot.json
  3. 重排 + 规范化(可选):
       python -c "import json; d=json.load(open('tools/ks2wtpy/references/wt_contracts_snapshot.json',encoding='utf-8')); json.dump(d,open('tools/ks2wtpy/references/wt_contracts_snapshot.json','w',encoding='utf-8'),ensure_ascii=False,indent=2,sort_keys=True)"
  4. 跑回归: `pytest tools/ks2wtpy/tests/test_ks2wtpy_regression.py -k p22`

不派生的字段 (运行时按 KS 数据计算):
  - `name`        = commodities[product].name + YYMM (中文化)
  - `code`        = KS 合约 code (如 ag2603)
  - `exchg`       = 当前 EXCHG
  - `product`     = 当前 product
  - `opendate`    = bars[0].date  (KS 数据起点, 注意活跃合约 ≠ 真实上市日)
  - `expiredate`  = bars[-1].date (活跃合约 = 最后数据日, 不是真实到期日)

⚠ opendate/expiredate 限制:
  - WT C++ HOT 切换走 hots.json, 不依赖 expiredate
  - 如未来需求要"真实到期日", 需引入交易所到期规则表 (按品种 + 月份反推)
  - 当前方案 A 的精度对回测/CTA 套利策略足够


## wt_fees_snapshot.json

来源: `wondertrader/dist/common/fees.json` (WT 官方发行模板, 36 个品种)

用途: KS 数据本身不含手续费, ks2wtpy 在 `generate_fees()` 中按 EXCHG.product
查表拷贝 {open, close, closetoday, byvolume} 4 字段; 未覆盖品种 fallback
全 0 (byvolume=False) 并发聚合 warning 提示手动补全。

字段语义 (WT 标准):
  - open       float: 开仓手续费率/单 (按 byvolume 解释)
  - close      float: 平仓手续费率/单
  - closetoday float: 平今手续费率/单 (CFFEX/SHFE 区分平今平昨)
  - byvolume   bool:  True=按手数固定金额; False=按成交额比例

13 目标品种覆盖率: 8/13 = 61%
  ✓ 命中: SHFE.ag, CFFEX.IF, CFFEX.IC, CZCE.AP, CZCE.CF, CZCE.SR, DCE.a, DCE.m
  ✗ 缺失 (fallback 0/0/0/False): CFFEX.IH, CFFEX.IM, DCE.b, INE.ec, GFEX.pd

升级流程:
  1. 修改/补充 `wondertrader/dist/common/fees.json`
  2. 重新规范化导出: python -c "import json; \
        s=json.load(open('wondertrader/dist/common/fees.json',encoding='utf-8')); \
        json.dump(s, open('tools/ks2wtpy/references/wt_fees_snapshot.json','w',encoding='utf-8'), \
                  ensure_ascii=False, indent=4, sort_keys=True)"
  3. 重跑 ks2wtpy 全量, 确认 manifest fee fallback warning 数量下降

警告: 字段值随交易所政策调整, 该快照仅为参考底版; 实盘前务必比对
经纪商最新费率表 (开/平/平今 + 是否打折), 在产物 common/fees.json 中直接覆盖。


## wt_holidays_snapshot.json (p2-4)

WT 官方 holidays.json (2008-2022, 265 天) 的 UTF-8 排序去重副本, 顶层 `{"CHINA": [YYYYMMDD,...]}`。
配合 `extra_holidays_2023_2026.json` (国务院发文手工维护) 补全 2023-2026 共 77 天, 合计 342 天。

升级流程: 每年 11-12 月国务院发布次年放假安排后, 编辑 `extra_holidays_2023_2026.json` 添加新一年即可。

比对源: `wondertrader/dist/common/holidays.json`

## extra_holidays_2023_2026.json (p2-4)

中国法定节假日扩展表 (2023-2026), KS 数据源不提供, 必须手工维护:
- 2023: 国办发明电[2022]16号 (19 天)
- 2024: 国办发明电[2023]7号 (19 天)
- 2025: 国办发明电[2024]9号 (19 天)
- 2026: 暂用 2025 同期估值, 待国务院发文后修正 (20 天)

按年组织 `{"CHINA": {"2023":[...],"2024":[...]}}`, `_` 前缀字段为审计注释自动跳过。
调休补班的工作日 (周末上班) 不算节假日, 不入此表; 仅含真实闭市日。

## wt_sessions_snapshot.json (p2-5)

WT 官方 sessions.json 的 UTF-8 副本 (原版 GBK 编码, 转码并 `sort_keys=True`)。
8 个 session ID 按收盘时间命名:
- `FD0900` / `FD0915` - 期货白盘 (15:00 / 15:15 收, 商品/工业品)
- `SD0930`            - 股票白盘 (沪深 300/中证 500 等股指)
- `FN0100` / `FN0230` / `FN2300` / `FN2330` - 期货夜盘 (按收盘时间区分品种)
- `TRADING`           - 全天交易模板

`commodities.session` 字段必须引用这 8 个 ID 之一, 命名必须一致才能在 C++ loader 中解析到。
**老实现自定义命名** (CFFEX_STOCK/SHFE_NIGHT1/...) **与 commodities 失配 (4 个 session 全断裂), 已废弃**。

比对源: `wondertrader/dist/common/sessions.json` (注意 GBK 编码)
