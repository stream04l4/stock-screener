# stock-screener · v8.2.0（双系统版：A股 + 加密货币，443 单域名 HTTPS）

**v8 = 双系统版**：一个仓库、两个独立的马丁格尔（Martingale）系统，均由离线
DuckDB 数据湖驱动（v7 起已移除多因子选股/日跑体系）：

- **A股**（稳定区）：深回撤金字塔加仓（`drawdown_martingale`，日线状态机）
- **加密货币**（v8 主特性，`crypto/` 独立单元）：深回撤网格 + 10x 止盈
  （1h 状态机，BTC/ETH）

A股链路：

```
DuckDB 数据湖 data/lake/（lake/ 包维护，cron 每日同步）
   │  read_only
   ▼
martingale/ 包（5 条件股票池 → 逐股状态机回测 → 产物 CSV/MD）
   │
   ▼
Web（FastAPI :9090，四页签：我的持仓 / 策略 / 回测 / 数据湖）
```

加密货币链路：

```
Binance 主源 + OKX 交叉校验 → data/crypto/crypto.duckdb（1h K线，全史 2017-）
   │  read_only（+ crypto-gateway 实时 K 线，纯内存态）
   ▼
crypto/strategy（70% 回撤建仓 → 1% 网格加仓 → 10x 止盈 → 下一轮）
   │
   ▼
Web（FastAPI :9091，/crypto/ 前缀，四页签：我的持仓 / 策略 / 回测 / 数据湖）
```

## 0. 项目结构（双系统，v7.0.44）

一个仓库，两个独立系统：**股票**（`https://stream04l4.duckdns.org/` → :9090，
web/ + lake/ + martingale/ + screener/，数据 `data/lake/` 等）与**加密货币**
（`/crypto/` → :9091，`crypto/` 独立单元，数据独占 `data/crypto/`，
湖 = data/crypto/crypto.duckdb）。443 单域名 HTTPS 入口由 nginx 按路径分流，
共享仅：.env 凭据 + web/auth.py 登录模块 + nginx 入口（同域 cookie 双端互免登）。

**目录职责 / 扩展落点 / 不可动清单 → 权威文档 `docs/architecture.md`；
加密货币设计细节 → `docs/crypto_design.md`。** 简记：股票侧是稳定区（防护
契约锁定，不挪顶层布局）；加密货币新功能一律落 `crypto/` 内（sources/
positions/strategy/backtest 子包 + web/），运行时数据只进 `data/crypto/`。

## 1. 策略（规格 = brief §3 + 参考实现逐行移植）

池内**每只股票**独立状态机（v8.1.0，Joel 2026-11-12 定案：首仓双条件 + 加仓锚点 P0）：

- **信号**：后复权收盘 hfq = raw × adj_factor（隐含除权推导见 engine.py 注释）；**成交**按原始收盘价。
- **高点 H**：初始 = 窗口首日 hfq 收盘。rearm 两模式：
  - `bounce`（默认）：价格 ≥ 本周期低点 L × (1+rearm_pct) → 重记 H = 当前价、重置 k=0
    + **本轮首仓锚点 P0 清零**（再武装 = 新轮，无独立买入权）。每 bar **先**更新
    low_since_high **再**判反弹（创新低当日不触发）。
  - `newhigh`：仅创新高刷新 H（同上重置 P0）。
- **首仓（任何一轮的 k=0 档）= 双条件（v8.1.0 核心变更）**：
  ① **回撤线**：hfq ≤ H×(1−first_drawdown_pct)；**且** ② **N 年收盘分位** ≤
  `price_pctile_max`（默认 5 年 40%，逐 bar PIT：窗口 = 当前 bar 前 N 个日历年、
  含当前 bar、`first_ok_series` 与 selection_timing 页面同口径；窗口 <60 bar
  fail-closed）。——「保证股价在长期低位**且**在下行通道才开始买入」；
  再武装后的第二轮首仓**同一双条件**。生产路径（页面「当前信号」/微信推送/
  触碰判定/G7 回测门/回测引擎）全部同一单点（`run_stock(first_ok=…)`）。
- **加仓（k≥1）= 锚点 P0 越跌越买（v8.1.0 核心变更）**：P0 = 本轮首仓信号价
  （首仓当根 bar 的 hfq 收盘）；hfq ≤ P0×(1−k×add_step_pct) 穿一档买一档，
  第 k 档（0 起）买 `first_lots + k×add_lots_step` 手（**v8.1.3 首仓 2 / 步长
  2**：2→4→6→8… 递增），上不封顶；跳空一日穿多档；加仓**无分位条件**
  （只认 P0 相对跌幅）。
  仓位口径 = 手数（v7.0.28 C 方案）：1 手 = `lot_size` 股（A 股固定 100）。
  （v8.1.0 前锚点 = 轮高 H：档线 H×(1−first_dd−k×step)——语义已变，f6 基线
  已按新口径重生成。）
- **不卖出、不封顶、无熔断**（无 exit/stop/max_position 参数）；出资优先分红
  余额（v7.0.36）。
- **分红**（cash 口径固定）：除息日前一日收盘持仓 × cash_dps 收现金，当日买入前执行；
  送转按比例加股；停牌期除权事件顺延复牌首个 bar。
- **成本**（引擎常量，不进配置、不进 Web）：佣金 3bps（买）；印花税 5bps（无卖出，no-op）。
- **组合聚合**：gdates 全局轴 + 末值向前结转（停牌持仓不消失）。
- 已知局限：湖内无退市股（幸存者偏差）→ 回测产物带此声明。

**额外持仓策略提醒 = 建仓日锚定的累计差值口径（v8.2.0，Joel 2026-11-13 定案）**：
持仓（我的持仓账本，**可含池外自选股**）同样按策略给建议，但以「理论上当前应该
持仓的手数」对照已买仓位——少则提醒补差、够了不提醒。**理论持仓从「我建仓时」
起算**（而非状态机全历史、也非当前轮）——因为实际建仓可能晚于策略理论首仓（错过
前几档）、也可能早于建仓时机（自买在前），锚到建仓日两种情形都正确：

- **理论持仓** = **用户建仓日（`since_date`）起（含当日）策略买入的累计股数**
  （`shares_since`，状态机真实买入序列按日期过滤）＋ **本档手数**（新档触发即
  执行的那一档：持仓中第 k 档 → `first_lots+k×step`；未持仓首仓 → `first_lots`）。
  「策略模拟股数」列（全历史 backtest 尺度，可达数百手）**不计入**理论持仓；
- **实际持仓** = 用户账本股数；**差值** = 理论 − 实际；
- **建仓日锚点**：账本每条持仓记 `since_date`——**手填 = 固定锚点；留空 =
  自动（= 最早加仓日，真实建仓日）**。升级前旧账本首次填建仓日时按「自该日起
  持有当前股数」回填一笔（不造虚假增量）；清仓重买 = 新条目新锚点。
- **加仓自动记录**：每次手动加仓自动追加到账本 `buys` 时间线 `{date, shares,
  price}`（直接改持仓按股数增量、日期=建仓日输入或当天；交易登记买入按登记日/
  价格、支持回填；减仓/改价不记）——时间锚定计算与对账的依据，行弹层「🕒 建仓
  时间线」展示。
- **提醒条件**：仅「新档首次触发（实时价首次穿线）**且**差值 > 0」→ 建议补差值
  （向上取整到整手，至少 1 手），推一次；差值 ≤ 0（持仓够了/超配）→ 不产信号、
  不提醒；同一缺口不重复推（现有 `代码|动作|档|点位` 去重态机天然覆盖——补仓
  后差值归 0，信号消失）。
- **展示**：Web「我的持仓」每行恒显 策略理论(手)/实际(手)/差值(手)（理论 =
  建仓日起累计，湖最新 bar 口径；实时新档补差在顶部信号条）；行弹层显示 建仓日/
  建仓日起策略股数；微信推送标注「（策略理论累计 X 手 / 现持 Y 手）」。
- **机制边界（Joel 定案）**：建仓日锚定**只在判断「我的持仓」时生效**——
  `state_rows(since_map=…)` 仅 Web 持仓页 / 微信持仓通道传入；**回测、新建仓
  信号、策略状态页一律不传**（`since_map=None` → 整段跳过，零影响，f6 基线不变）。
- 实现单点 = `martingale/signals.py::build_signals`（Web 与微信共用）；
  `shares_since` 由 `martingale/engine.py::state_rows(since_map=…)` 计算；缺数据
  （湖中无该股 / 字段缺失）回退旧行为（建议本档手数）；无新增配置键；`since_date`
  是账本字段（data/my_positions.json），非策略参数。


## 2. 股票池 = 候选池（v7.0.12 门槛 + v8.0.8 行业全放开/国企控股强度门 ∪ 手动添加）

**候选池 ≠ 买入信号**：入选只代表进入策略候选名单；是否买入看价格是否到策略触发线
（Web「我的持仓」页信号 / 微信 PushPlus 提醒，同一口径，`martingale/signals.py`）。

基础池（`build_pool`）：`全市场 → 行业白名单(csric2；v8.0.8：["*"] 全放开) → SOE(国资前十大股东)
→ v8.0.8 国企控股强度门 → 总市值≥200亿 → 上市≥250 bars → 连续分红N年`。
选股门槛（v7.0.12，Joel 2026-10-24 拍板；波动率门槛经讨论不加）：
⑦ **十年滚动回测门槛**（≥1 次触发且净收益率 ≥ backtest_min_return_pct%，**v8.0.8 生产 30**
——v7.0.39 定案 20，v8.0.8 Joel 2026-11-11 提档；含分红扣佣金；预计算表
`selection_backtest_10y`，fail-closed：缺行/收益率为 NULL 不入选）→
⑧ **TTM 股息率≥min_ttm_yield_pct%（v8.0.8 生产 3.5，v7 起 3）** → ⑨ **价格 2~100 元**。
**手动添加**：`data/manual_pool.json`（策略页签股票池卡片内的手动录入条 /
`GET/POST/DELETE /api/pool/manual`）绕过 ⑦⑧⑨ 恒在候选池；退市自动剔除；
与自动入选重叠时保留自动版并标记 `is_manual`。
生产首跑（2026-10-24，as_of 10-23）：98 → 82(⑦) → 60(⑧) → **59 只**；
**v8.0.8 扩容后（as_of 2026-11-11）：70 只**（全行业 5,029 → SOE 2,204 →
控股强度门 1,580 → 市值 426 → 分红 329 → ⑦≥30% 170 → ⑧≥3.5% **70**）。

SOE 双规则：holders_snapshot 最新 as_of 的 holder_rank≤10 中，share_nature='国有股'
或名称含 soe_keywords 任一（国务院/国资委/汇金/财政部/国资）。

**国企控股强度门（v8.0.8，Joel 2026-11-11 定案）**：行业全放开后「top10 任一国有股东」
判定过宽（美的 1.88%/青年服饰 5.58% 这类证金公司财务性小持股也能进池）。口径 =
holders_snapshot 最新 as_of、top10、share_nature='国有股'：
**通过 = 最大单一国有占比 ≥ min_soe_ratio_pct（8）或 top10 国有合计 ≥
min_soe_sum_ratio_pct（30）或 soe_control_overrides 豁免**。
8/30 定值依据：湖数据实测伪国企（民营控股）最大国有占比全部 ≤5.58%、真国企全部
≥8.21%（5.6~8.2 为数据自然断层）；合计档覆盖多平台持股型地方国企（江苏银行单一
6.98% 但合计 35.51%）。纯 30% 单一线被否决（会误杀全部城商行，平台直持 8~25%）。
配套键：`exclude_codes`（硬剔除，现状 sh.603766 隆鑫通用=民营控股）+
`soe_control_overrides`（数据标注缺口豁免，现状 4 只：中国海油 BVI 控股/四方股份
国网系/宇通客车郑州国资/潍柴动力潍坊国资，逐项理由见 yaml）。5 键均为
**yaml-only 治理键**（Web 元数据守卫覆盖但不渲染表单行，改值走 yaml 手工编辑）。

**央企收紧门（v8.1.1 引入，v8.1.2 起默认开，Joel 2026-11-12 定案：行业风控
复杂机制搁置，央国企资质作兜底前置筛选）**：`central_only: true` 时，池在国企
门之上再要求「最大国有股东（≥ 控股强度门槛 min_soe_ratio_pct，缺省 8%）为
**央企**」——把「国资」收紧为「央企」，地方国企（深铁/上海国资/广州/北京/
安徽/重庆…）与国资参股（民企实控 + 央企小比例股东）均剔除。口径 =
holders_snapshot 最近快照（动态回测 PIT 版取 ≤ as_of）、top10、
share_nature='国有股'，股东名须命中 `martingale/central_soe.py`
`CENTRAL_TOKENS`（98 家国务院国资委央企集团 + 中央军委新兴际华 + 中央金融
六大行/保险/四大 AMC/中央汇金/证金/中信系 + 国资运营国新 + 三大运营商）。
TOKEN 清单 2026-11-12 对照湖内 3,996 个 ≥8% 国有 top10 股东名人工审计定稿
（含 2 期快照控制权变更 PIT、短 token 误判审查）；只增不删，央企重组新名手工
追加。背景：v8.1.0 十年动态池 18 只亏损股 17 只已是国资控股（兜底本已在池内），
亏损由行业塌方驱动（地产 6/建材钢铁 3/汽车 2），其中央企亦亏 6 只——收紧为
央企防「消失/跑路」，不防「行业下行」。**v8.1.2 起生产默认 true**（行业仍
全放开，仅收紧央/地口径；改 false 即回 v8.1.1 前行为）。
全湖 ≥8% 国有控股的上市央企共 **261** 只（另 2 只央企标注缺口豁免：中国海油
BVI 控股/四方股份国网系，见 soe_control_overrides）；基础池 329 → 央企口径
**111** 只（剔 218 只地方国企/国资参股）；当前生产候选池 ≈30 只。

## 2.5 选股时机（v7.0.13，候选池之上筛「何时首仓买入」；v7.0.24 起单条件门）

候选池解决「买什么」，时机门解决「何时买」（Joel 2026-10-24 拍板，5 点全按
推荐口径；**2026-11-09 v7.0.24 回测定案：门 = ① 单条件**；**v7.0.26 定案：
②③ 彻底删除、首仓建仓回撤归入本策略**）：

- **① 价格低位【时机门唯一条件】**：当前价处近 N 年（默认 5）qfq 收盘
  **分位 ≤ price_pctile_max**（默认 40%）；
- **② 股息率 / ③ 250 日线：v7.0.26 彻底删除**（config 键、湖表列、页面列、
  信号明细全移除，不留观察项；回测结论：② 是主要盈利拖累且与池门槛 G8
  同维冗余、③ 年线近乎无效）。
- **首仓建仓回撤（first_drawdown_pct，默认 15%）**：首仓线 = 本轮高点×(1−该值)。
  v7.0.26 自「持仓策略」段移入「选股策略」段管理——首仓属买入时机；
  引擎经 StrategyParams 读取，数学零改动。
  **v8.1.0（Joel 2026-11-12 定案）**：首仓线（①）与 ① 价格低位（分位门）
  **合并为引擎首仓双条件**——任何一轮首仓（含再武装后第二轮）须 ① 且 ② 同时
  成立才买，页面/微信/回测同一单点（此前分位门仅页面观察+推送侧，回测引擎
  首仓只认回撤线，2856 次历史首仓 83% 买在 5 年分位 >40%——系统性买高）。

**口径**（`martingale/timing.py` 单一定义点，v7.0.24 起观察/提醒同条件）：
- **页面口径**（「今日选股」表）= ①（`in_watchlist`；`push_eligible` 列保留
  = ① 同值，仅页面徽标用）。
- **推送纪律（v7.0.25，Joel 2026-11-09 定案）**：微信**不再**为时机独立推送
  （旧「时机成熟（首仓待价）」通道 + 边沿状态 `timing_push_state.json` 已退役，
  只有时机没到价无意义）。微信统一为**进入当前信号才推**（盘中「策略点位
  提醒」：首仓 = 时机✓ 且 价破首仓线；加仓 = 价破档线）。
- PE 估值分位经实测不加（池内 0 只低于自身 5y 20% 分位，无区分度）。

**流程**：每日 lake-sync 后 `python -m martingale timing`（~1s 纯湖内计算，
零网络零配额零推送）→ 整表刷新湖表 `selection_timing`（PK ts_code）→ Web O(1) 读；
失败不阻断（页面 fail-closed 空表 / 读旧快照，次日重算）。
生产首跑（as_of 10-23）：59 候选 → 观察 18 → 提醒 1（大秦铁路：
5 年分位 21.9% / 回撤 27.5% / 股息 4.59%）。

**闭环**：今日选股表「加持仓」→ 行内填股数/成本（PUT /api/my_positions）→
「我的持仓」账本 → 持仓策略接管档位（现有信号/提醒逻辑零改动）。

## 3. CLI

```bash
.venv/bin/python -m martingale pool                       # 打印候选池 + 全漏斗
.venv/bin/python -m martingale selection                  # 候选池选股预计算（湖表刷新）
.venv/bin/python -m martingale timing                     # 选股时机预计算（v7.0.25 起不推送）
.venv/bin/python -m martingale backtest --start 2016-01-04 --end 2026-10-21 \
    --out /tmp/bt_out                                     # 全池回测
.venv/bin/python -m martingale backtest --codes sh.601088,sz.000651 --out /tmp/bt2
.venv/bin/python -m martingale backtest --dynamic-pool --out /tmp/bt_dyn
.venv/bin/python -m martingale status --out /tmp/bt_out   # 读产物：逐股当前状态
```

- 窗口留空 = 各股数据起点 → 数据终点（**动态池模式例外**：留空自动收敛到
  **最近 10 年**（`ROLLING_DAYS`）——全跨度 35 年要 430+ 次月度重算，内存/时长
  爆炸且 2016 前 SOE/分红 PIT 数据稀疏、池无意义）。
- `selection` = 基础池逐股十年滚动回测 → 整表刷新湖表 `selection_backtest_10y`
  （98 只 ~2s，纯湖内计算零网络零配额）；每日作业在湖增量后自动追加执行，
  失败不阻断（Web 候选池 ⑦ 读旧快照 fail-closed，次日重算）。
- `timing` = 候选池逐股 ①分位（v7.0.26 起唯一时机条件）→ 整表刷新湖表 `selection_timing`
  （v7.0.25 起纯预计算不推送——微信统一为进入当前信号才推，策略点位提醒）。
- 进度标记 `[PROGRESS] stage=bt done=k total=N`（Web SSE 分类器消费）。
- 产物：`<out>/{per_stock.csv, buys.csv, equity_curve.csv, summary.md, status.json}`。
- **动态池回测**（v8.0.13，`--dynamic-pool`，与 `--codes` 互斥；Web 回测页签
  「动态股票池（月度重算，较慢）」同参数）：每月第一个交易日**按当天 point-in-time
  数据**重算全漏斗（基础 6 门 ∩ ⑦滚动十年策略回测 ∩ ⑧TTM 股息率 ∩ ⑨价格区间，
  不含手动池），重算日之间的 bar 沿用最近一次池（首次重算日前禁交易）。
  - **PIT 近似**：退市 `delist_date > as_of` 判存活；ST `st_since ≤ as_of` 从严
    （无摘帽日期）；股东快照取 ≤ as_of 最近季度（缺期沿用更早）；估值/价格/分红/
    K线行数全部 `date ≤ as_of`。
  - **出池语义**：冻结新开仓+加仓（引擎 `trade_mask` 只门控买入块；档位不推进=
    「暂停」，回池后按现价补买）；**不强制平仓**（策略无卖出信号，持仓/分红/
    浮盈跟踪照常）。
  - **内存架构（v8.0.13 实踩定案）**：月度重算跑在**一次性子进程**
    （`martingale dynpool-month`，3 路并行）——DuckDB/mimalloc 的 C 层
    allocator 滞留 ~100MB/月且不归还 OS（gc/重连均无效；进程内 121 个月
    → 10GB 全局 OOM 三次实踩），子进程退出即归零；父进程只聚合小体积池
    集合。单月 ~4.5s → 十年全量 **~5 分钟**。
  - 产物增量：`per_stock.csv` +`pool_months/pool_first/pool_last`；
    `pool_timeline.csv`（月度池规模+入池/出池名单）；`status.json` +`mode`/`pool`
    键。静态模式产物**逐位不变**。
  - 动机：旧「按股票池回测」= 用**今天**的基础池（329 只，非候选池 70 只）回测
    全窗口 → 近年才达标的股从第一天就出现（「一堆没见过的股票」）；动态池
    先算当天池再看建仓/加仓，与生产候选池同口径。

## 4. 参数（config/strategy.yaml）

| 段 | 键 | 说明 |
|---|---|---|
| strategy | name | 固定 `drawdown_martingale` |
| | lot_size | 每手股数（A 股固定 100） |
| | first_lots | 首仓手数（k=0 档；再武装后新轮首买同此值；**v8.1.3 生产 2**） |
| | add_lots_step | 加仓手数增量（第 k 档 = first_lots + k×add_lots_step 手；**生产 2** = 2/4/6/8…，v8.1.3 首仓 2） |
| | add_step_pct | 每档加仓步长（% → 小数） |
| | rearm_mode | `bounce` \| `newhigh` |
| | rearm_pct | bounce 反弹重记高点阈值（% → 小数） |
| | accounting | 固定 `cash`（只读，Web 不可改） |
| universe | industry_whitelist_csric2 | 行业白名单（3 位 csric2 码；**v8.0.8：`["*"]` = 全放开**，生产已放开） |
| | soe_keywords | 国资股东名称关键词（SOE 恒强制，无开关） |
| | min_total_mv_yi | 总市值门槛（亿） |
| | min_consecutive_div_years | 连续分红年数 |
| | listing_min_trading_days | 最少上市 bar 数 |
| | backtest_positive | ⑦ 十年回测门槛开关（v7.0.12） |
| | backtest_window_years | ⑦ 回测窗口年数（滚动） |
| | backtest_min_return_pct | ⑦ 十年回测净收益率下限（%，可选键；v8.0.8 生产 30） |
| | min_ttm_yield_pct | ⑧ TTM 股息率下限（%；v8.0.8 生产 3.5） |
| | price_min_yuan / price_max_yuan | ⑨ 最新价区间（元） |
| | min_soe_ratio_pct / min_soe_sum_ratio_pct | **v8.0.8 国企控股强度门**：最大单一国有占比 ≥ 前者 **或** top10 国有合计 ≥ 后者（%，可选键，0 = 关闭；生产 8 / 30） |
| | exclude_codes / soe_control_overrides | **v8.0.8** 硬剔除名单（伪国企 ts_code）/ 控股强度门豁免（code→理由，数据标注缺口）；4 治理键 yaml-only（Web 元数据守卫覆盖、不渲染表单行） |
| | central_only | **v8.1.1 央企收紧门**：true = 池再取央企交集（最大国有股东 ≥ 控股强度门槛须为央企，清单见 central_soe.py）；可选键，**v8.1.2 起默认 true**（false = 回 v8.1.1 前全国资行为）；yaml-only 治理键 |
| timing（**段可缺省**，缺省=默认值；旧快照无此段时 first_drawdown_pct 回退读 strategy 段） | price_window_years | ① 价格分位窗口（年，默认 5） |
| | price_pctile_max | ① 分位上限（%，默认 40）【时机门唯一条件】 |
| | first_drawdown_pct | 首仓建仓回撤（%，默认 15）；首仓线 = 本轮高点×(1−该值)；v7.0.26 自 strategy 段移入 |
| lake / datasource.* | — | lake-sync / 补数专用，Web 只读不渲染 |

**手数口径说明（v7.0.28）**：金额 3 键（initial_amount/base_add_amount/growth_ratio）
已删除——仓位一律以手数表达，参考金额 = 手数 × lot_size × 现价（前端派生展示）。
佣金/印花税是引擎常量（comm=3bps / stamp=5bps），不在 schema 中。

## 5. Web（:9090，systemd user 服务）

```bash
export XDG_RUNTIME_DIR=/run/user/1000
systemctl --user status stock-screener-web     # uvicorn web.app:app
systemctl --user restart stock-screener-web    # 前端 build 或 web/app.py 改动后
```

四页签：**我的持仓**（录入/信号/提醒状态；一键加持仓也写这里）·
**策略**（三组卡片：**持仓策略**（原「策略参数」，v7.0.13 改名）/ **股票池**（候选池：
基础 5 条件 + 选股门槛字段 + 候选股全列表（59 只，10年回测%/股息率/最新价/来源，
可搜索排序）+ 手动添加录入条）/ **选股策略**（v7.0.13 时机门参数（v7.0.24 起
= ① 分位单条件；v7.0.26：②③ 删除 + 首仓建仓回撤移入）+
**今日选股表**（时机门命中，「时机成熟」徽标 = ① 命中，每行一键加持仓））；
参数编辑/校验；v7.0.29 起策略库退役，仅编辑当前策略）· **回测**（参数表单 + SSE 进度 + 结果区）·
**数据湖**（v6 原样）。候选池不设独立页签（v7.0.12 按 Joel 要求收敛进策略页签）。

主要 API：`/api/health` · **登录门禁（v7.0.14）**：`/api/login`（POST，写会话
cookie）· `/api/logout`（POST）· `/api/auth/me`（GET 当前用户，未登录 401）——
除这三者外全部 `/api/*` 需有效会话 cookie ·
`/api/pool`（候选池：漏斗 + 成员，读预计算表 O(1)）·
`/api/pool/manual`（GET 读手动池 / POST 加入 / DELETE 移除，写 `data/manual_pool.json`）·
`/api/strategy`（GET/PUT，PUT 语义校验 + .bak 备份；timing 段可选）·
`/api/timing`（v7.0.13 今日选股：读 `selection_timing` O(1)，as_of/rows/params；
表缺失 → 空列表 fail-closed，湖持锁 → 503）·
`/api/my_positions`（GET 账本+信号 / PUT upsert 单只，shares=0 删除）·
`/api/positions`（同步全池）· `/api/bt/runs`（POST 触发 / GET 列表 / `{id}` status /
`{id}/events` SSE / `{id}/cancel`）· `/api/bt/results/{id}` · `/api/lake/*`。
回测 = 异步子进程（单任务 409 + pidfile 锁），进度 SSE（15s 心跳，断线 3 次降级 3s 轮询）。

## 5.5 加密货币系统（crypto/，:9091 /crypto/，v8 主特性）

独立进程 / 独立端口 / 独立 SPA / 独立湖（与 A 股湖物理隔离）；登录/鉴权与 A 股
同源（同一账号，ssc_session cookie 同域互免登）。设计细节 → `docs/crypto_design.md`；
目录职责 / 扩展约定 → `docs/architecture.md`。

**策略**（`crypto/strategy/engine.py` 纯函数状态机，1h bar；参数 = `crypto/config/
crypto.yaml` strategy 段 + 页面覆盖 `data/crypto/strategy_params.json`，页面覆盖
不改仓库配置）：

- **建仓** = 价 ≤ 一年高点×(1−first_drop_pct)（默认跌 70% → 30% 建仓位），首仓
  10 USDT（base_amount_usdt）；
- **加仓** = 建仓价×(1−1%×k) 固定网格，第 k 级 = 10×1.05^k USDT（growth 1.05，
  不设上限）；**上涨不加仓、新高不刷新建仓价**；
- **止盈** = 价 ≥ 建仓价×10（take_profit_mult 10）全卖 → entry 重置进入下一轮；
- 手续费 fee 10bps（taker）；一年高点 = 滚动 8760h（lookback_h）收盘价最大（含当日 bar）；
- **账户模型**（v7.0.53）：单账户闭环——买入先扣余额（了结所得滚存），不足计
  外部投入；净值曲线 = 持仓市值 + 余额；
- **边界纪律**：回测 = 策略验证工具，不接自动交易。

**数据**（独立湖 `data/crypto/crypto.duckdb`）：

- 表：`kline_1h`（PK exchange,symbol,ts，幂等 upsert）+ `coin_master`（is_active
  开关）；1d/4h 由 1h 本地聚合，不建表。
- 全量历史：Binance 官方月度快照 data.binance.vision（`--full`，4 线程 + 月度
  CSV 沙盒缓存 + 403/429 退避，纯 stdlib 无新依赖）——BTC/ETH 自 2017-08 起；
  日常增量：cron 每小时 `python -m crypto.sync`（3h 回补窗口自愈 + OKX 最近
  24h 收盘交叉校验，告警阈值 0.1%）。
- 币种清单：聚焦 **BTC/ETH**（SOL v7.0.52 退役，退役共 18 币；数据保留、
  is_active=FALSE，恢复 = 加回 coins 重跑 sync）。
- **实时 K 线网关**（`crypto-gateway.service`）：WS 组合流（1 连接多标的 1h
  K 线）+ 120s 看门狗 + 指数退避重连 + 同 ts 去重；**纯内存态不入湖**（服务
  后续实时买点判断），状态文件 `data/crypto/gateway_state.json`。

**Web**（`crypto/web/app.py`，:9091，systemd `crypto-web.service`；443 入口
`/crypto/` 前缀透传 + 外层 auth 门）：四页签 **我的持仓**（P2 占位，ready=
false）/ **策略**（7 参数编辑-锁死模式，v7.0.50 与 A 股 tab 同语义）/
**回测**（全史 + 时间范围（v7.0.54，窗口账户从空 + 前置高点基线）+ 双图
（净值 + 价格对数坐标，建仓▼/加仓●/平仓▲ 事件标记）+ 总外部投入）/
**数据湖**（聚焦 2 币主表 + 退役币折叠 + 实时网关卡片）。主要 API：
`/api/coins` · `/api/kline` · `/api/positions`（P2 占位）· `/api/strategy`
（GET / POST 保存 / POST reset）· `/api/backtest`（POST，可选 start/end
UTC 日界）· `/api/gateway` · `/api/lake/status`。

**CLI**：

```bash
.venv/bin/python -m crypto.sync                # 1h K线增量（cron 每小时）
.venv/bin/python -m crypto.sync --full         # 全量历史（vision 月度快照 + REST 尾巴）
.venv/bin/python -m crypto.backtest            # 全史回测 → data/crypto/backtest_report.md
.venv/bin/python -m crypto.backtest --start 2021-11-11 --end 2026-11-11
.venv/bin/python -m crypto.backtest.paramsearch --start --end --coins --base --budget --out   # 参数网格寻优（v7.0.55 / v8.0.1 预算）
```

**参数寻优**（v7.0.55，`crypto/backtest/paramsearch.py`）：建仓回撤 × 止盈倍数 ×
加仓倍数网格（默认 4×4×4 = 64 组合/币，2 币 ~17s），指标 = 交易频率 / 总外部
投入 / 回报率 / 资金周转 CAGR / 均持仓天数 / MDD，按 CAGR 降序。近 5 年
（2021-11-11→2026-11-11）结论 → `data/crypto/param_search_5y.md`：两币最多
1~2 轮（触发集中在 2022 熊市）；**止盈 5x 显著优于 8/10x**（5x 已了结落袋、
8/10x 至今持仓）；加仓倍数只放大投入规模；80% 回撤 BTC 五年内从未触发
（机会风险）。

## 6. 定时任务

crontab 业务行（2026-11-11 现状，6 条：A 股 5 + 加密货币 1）：

| 行 | 时间（UTC） | 职责 |
|---|---|---|
| lake-sync-daily | 1-5 11:30 | A 股数据湖每日同步 + 候选池预计算（selection ~2s → timing ~1s），martingale 一切查询依赖它 |
| gate1-latency-3pts | 1-5 01:35/04:35/06:35 | Gate 1 实时价源（腾讯主/push2delay 备）延时观察 |
| signal-touch-daily | 1-5 12:05 | 盘中触碰事件落盘（lake-sync 灌完当日 bar 后，与页面同口径） |
| t1-delist-weekly | 周日 11:30 | T1 退市股处理 |
| t6-quarterly | 9/11/5 月 5 日 11:30 | T6 季度维护 |
| crypto-sync-hourly | 每小时 :05 | 加密货币 1h K 线增量 + OKX 交叉校验 |

另有 systemd user timer `stock-screener-alerts.timer`（持仓点位微信提醒，不在
crontab）。v6 的 `35 9 * * 1-5 run_cron.sh`（多因子日跑）已删除。

**v7.0.12/7.0.13**：lake-sync 脚本在湖增量结束后自动追加
`python -m martingale selection`（候选池选股预计算，~2s）→
`python -m martingale timing`（选股时机预计算 + 新触发微信提醒，~1s；
均纯湖内计算零配额；失败不阻断，exit 仍取湖增量 RC）。

**v7.0.11 持仓点位提醒**：`scripts/position_alert.py` + systemd user timer
（`stock-screener-alerts.timer`，工作日每 10 分钟 + 时段门 09:15-11:35 /
12:55-15:05 北京时间；实时价 TencentClient 失败回退湖价，只漏报不误报）。
「我的持仓」中持仓股到策略点位（首仓线/加仓线）→ PushPlus 微信推送，
去重键 `code|action|k|触发价`，状态 `data/alerts/pushplus_alert_state.json`。
PushPlus 客户端单一定义点 `martingale/pushplus.py`（token 在 `.env` PUSHPLUS_TOKEN，
gitignore，禁入 git/日志）。

**v7.0.8 增量提速**：T2 K线/T3 估值快照改 tushare 全市场单日批量
（`pro.daily`/`pro.adj_factor`/`pro.daily_basic` 各 1 次/分钟限频，~2.5min），
取代逐只 sina+腾讯（~9-10h）；多日缺口股（停牌）仍逐只多源 fallback；
批量失败/行数异常自动回退逐只（`stats["batch"]` 记痕）。
**配额纪律（免费 120 分实测）**：`adj_factor`/`daily_basic` 各 **5 次/天**，
超限进 1 次/小时惩罚态（重置口径未确认）——每日批量各用 1 次，
**手动调试/回测勿再调这两个 trade_date 端点**；`daily` 配额宽裕。

## 7. 测试

```bash
.venv/bin/python -m pytest tests/ -q          # 后端（708 用例，A股+加密货币；全离线：tmp-duckdb/monkeypatch，零真实网络）
cd web/frontend && npm test                   # A股前端 vitest（253 用例，22 文件）
cd crypto/web/frontend && npm test            # 加密货币前端 vitest（13 用例，3 文件）
cd web/frontend && npm run build               # dist/ 供 StaticFiles 挂载
```

**f6 回归基准**：`tests/fixtures/f6_baseline/`（七股 per_stock.csv + summary.md，
2026-10-22 Joel 验收数字逐位基准）。`tests/test_f6_regression.py` 重跑七股并与
fixture 逐位比对（n_buys / total_invested / final_assets / pnl_pct / dividends /
首末买日）。改 engine 后必跑。

守卫：`test_lake_zero_import.py`（lake 包不得 import martingale/screener）、
`test_no_hardcoded.py`（新包路径无硬编码）、grep 守卫（tracked 文件无
`python -m screener` / run_cron.sh 残留）。

## 8. 目录结构

```
martingale/        # v7 策略包（A股）：config/data/universe/engine/report/__main__(CLI)
crypto/            # 加密货币系统（v8 主特性）：sources/strategy/backtest/gateway/positions/web + config/crypto.yaml
lake/              # 数据湖（v6 保留，零改动红线）
screener/          # 退化为数据客户端库：config + data/{baostock_client,tencent,sina,rf}
web/               # A股 FastAPI app.py + frontend/（Vue3 + vitest；dist/ 构建产物）
config/strategy.yaml   # A股策略：strategy(8键)/universe(11键)/timing(5键,可缺省)/lake/datasource
scripts/           # lake 运维：lake_backfill / run_lake_sync_daily.sh / lake_smoke / …
data/lake/         # A股数据湖 lake.duckdb（gitignore）
data/crypto/       # 加密货币数据湖 crypto.duckdb + 运行时文件（gitignore）
tests/             # pytest（含 fixtures/f6_baseline/；test_crypto_* 覆盖加密系统）
output/            # 回测产物（gitignore；旧多因子产物保留未清）
```

## 9. v7 变更摘要（相对 v6.3.6，git 8c49f71）

- 删：多因子选股/打分/徽章/回测引擎（screener 主体、backtest/、run_cron.sh、
  5 个多因子 scripts）、Web「运行」页签与 /api/runs、/api/backtest 族、45 个测试。
- 增：`martingale/` 包（状态机自 scripts/backtest_martingale.py 逐行移植，f6 七股
  逐位对齐后删参考脚本）、`/api/bt/runs` 族（SSE）、前端回测/查询页签。
- 保留：`lake/` 包（`git diff 8c49f71 -- lake/` 为空）、lake-sync cron、数据湖页签。
- 旧多因子 output/ 产物保留未清（清理交 Joel 定夺）。

## 10. 版本历史（v7.0.11 → v8.2.0）

- **v8.2.0**：额外持仓（我的持仓，可含池外自选股）策略提醒改为**建仓日锚定的
  累计差值口径**（Joel 2026-11-13 定案）——按「理论上当前应该持仓的手数」对照
  实际持仓，少则提醒补差、够了不提醒；**理论持仓从「我建仓时」起算**：
  - 理论持仓 = **用户建仓日（`since_date`，账本字段）起（含当日）策略买入的
    累计股数**（`shares_since`，状态机真实买入序列按日期过滤；建仓晚于理论首仓 /
    早于建仓时机两种情形都正确）＋ 本档手数；「策略模拟股数」（全历史 backtest
    尺度，数百手量级）**不计入**；实际 = 账本股数；差值 > 0 → 建议补差值（向上
    取整到整手）；差值 ≤ 0 → 不产信号不提醒。仅「新档首次触发 且 差值>0」推一次，
    同一缺口不重复推（现有去重态机覆盖）。
  - **机制边界**：建仓日锚定**只在判断「我的持仓」时生效**（Web 持仓页 / 微信
    持仓通道传 `since_map`）；**回测 / 新建仓 / 策略状态页不传**（`since_map=None`
    → 整段跳过，零影响，f6 红线不变）。
  - 账本每条持仓记 `since_date`：手填 = 固定锚点；不填 = 自动（最早加仓日）；
    升级前旧账本首填建仓日时按「自该日起持有当前股数」回填一笔；清仓重买 =
    新锚点。Web 录入条/行弹层可显式填/改。
  - **加仓自动记录**（Joel 2026-11-13）：每次手动加仓自动追加到账本 `buys`
    时间线 `{date, shares, price}`——直接改持仓按股数增量记（日期=建仓日输入，
    未填=当天）；交易登记买入按登记日/价格记（支持回填）；减仓/改价不记。行弹层
    「🕒 建仓时间线」展示。时间锚定计算 + 对账依据。
  - Web「我的持仓」每行恒显 策略理论(手)/实际(手)/差值(手)（湖最新 bar 口径），
    行弹层显示 建仓日/建仓日起策略股数；微信推送标注「（策略理论累计 X 手 /
    现持 Y 手）」。
  - 实现单点 `martingale/signals.py::build_signals`（Web/微信共用）＋
    `martingale/engine.py::state_rows(since_map=…)`；缺数据回退旧行为；无新增
    配置键；`since_date`/`buys` 是账本字段（data/user_positions.json），非策略参数。
  - 测试：engine shares_since 锚定单测（含当日/非成员股/None 零影响）+ signals
    差值口径 9 例 + 管道级 2 例（够→不推/缺→补差文案）+ 账本加仓自动记录
    （新建仓/加差值/减仓不记/手动锚点/旧条目回填/交易登记回填 + 锚点自动前移）
    + Web 持仓行字段一致性 + 前端建仓日录入/时间线弹层断言；后端全量回归通过、
    前端 269/269。
- **v8.1.3**：首仓手数 `first_lots` 1 → 2（Joel 2026-11-12 定案：首仓 2 手、
  步长 2，即 2→4→6→8… 手，加大单股投入规模）
  - 触发时机不变（仅首笔手数 1→2、其后各档 +2），f6 基线按新 sizing 重生成
    （2026-11-12；7 股 n_buys/日期/价格与 v8.1.0 基线逐笔零 diff，仅金额变）。
  - 首仓双条件网格寻优（回撤 10/15/20% × 5 年分位 40/45/50/55/60%，5% 步长
    共 15 组，同窗口动态池 PIT，首仓 2 步长 2，2026-11-12 跑完）：

    | 回撤＼分位 | 40 | 45 | 50 | 55 | 60 |
    |---|---|---|---|---|---|
    | **10%** | 53万/+61.2%/37只 | 58万/+63.2%/38只 | 61万/+67.9%/38只 | 79万/+54.6%/37只 | **91万/+55.6%/38只/胜率84.2%/绝对+51.0万** |
    | **15%**（现） | **47万/+75.0%/31只** | 46万/+66.0%/32只 | 49万/+67.7%/31只/胜率80.6% | 61万/+62.2%/33只 | 64万/+61.1%/35只 |
    | **20%** | 41万/+84.0%/24只 | 40万/+83.0%/22只 | 41万/+85.2%/22只 | 47万/+69.7%/24只 | 50万/+67.6%/26只 |

    （格内 = 十年外部净投入 / 总收益率 / 触发只数；10%/60% 另注：全网格最高
    胜率、最深浮亏仅 -80%（与其余组同量级 -78~80%）、绝对盈亏 +51.0 万为
    全网格最高。）规律：**投入规模主要由分位门驱动**（40→60 每档 +10~25
    万）；回撤深度反向（20% 档信号少而精：+83~85% 但仅 22~26 只）；
    **10%/60% 在投入（91 万，现参 2 倍）、绝对盈亏（51 万）、胜率（84.2%）
    三项全网格第一**，代价是收益率率值最低（55.6%）；全网格投入天花板 91
    万——200 万/10 年不在 15 组内，差距在池深（30 只候选）不在参数。
- **v8.1.2**：两项生产默认变更（Joel 2026-11-12 定案）
  - **`universe.central_only` 默认 false → true**：股票池收紧为央企（行业仍
    全放开，仅收紧央/地口径）；改 false 即回 v8.1.1 前全国资行为。
  - **加仓步长 `add_lots_step` 1 → 2**：首仓 1 手 / 加仓 3 手 / 再加 5 手 /
    7 手…（第 k 档 = first_lots + k×add_lots_step 手；触发时机不变，仅手数
    放大）。f6 基线按新 sizing 重新生成（2026-11-12；7 股 n_buys/日期/价格
    与 v8.1.0 基线逐笔一致，仅金额/汇总变化）。
  - **回测对照**（2016-11-11 ~ 2026-11-11 十年动态池 PIT，仅央企 + 步长 2，
    vs v8.1.1 仅央企 + 步长 1）：总收益 +72.0% → **+71.8%**（投入 23.3 万 →
    **35.9 万**，盈亏 +16.7 万 → +25.8 万，期末净值 40.0 万 → 61.6 万）；
    触发 29 → 31 只（招商系 token 补齐后招商蛇口等入池；招商银行在池但十年
    双条件从未触发——长期上行股分位门恒不满足）；胜率 72.4%（21/8）→
    **74.2%（23/8）**；买入 106 → 107 笔；分红/价差占比 24.9/75.1% →
    29.2/70.8%。
    **口径注**：107 笔中 48 笔为 1 手首仓，加仓深档（7 手以上）仅 14 笔——
    步长放大只作用于少数深档，投入 +54% 而非翻倍；投入瓶颈 = 信号笔数
    （31 只触发），非单笔规模。规模化实验（同池同窗口）：首仓 2 手/步长 2 →
    投入 46.5 万/+72.0%；首仓 5 手/步长 2 → 投入 75.6 万/+65.4%（约每 +1
    首仓手 +10 万投入，200 万量级需首仓 ≈15-18 手，单股集中度过高；实验
    池为 token 补齐前口径，规模化趋势不受影响）。
- **v8.1.1**：央企收紧门 `universe.central_only`（Joel 2026-11-12 定案：行业
  风控复杂机制搁置，央国企资质作兜底前置筛选；可选键，默认 false = 零行为变化）
  - **口径**：池在国企控股强度门之上再取「央企」交集——top10 国有股东
    （≥ min_soe_ratio_pct，缺省 8%）名须命中 `martingale/central_soe.py`
    `CENTRAL_TOKENS`（98 家国务院国资委央企集团 + 中央军委新兴际华 + 中央金融
    六大行/保险/四大 AMC/中央汇金/证金/中信系 + 国资运营国新 + 三大运营商）。
    清单 2026-11-12 对照湖内 3,996 个 ≥8% 国有 top10 股东名人工审计定稿
    （含控制权变更 PIT 两期快照、短 token 误判审查、央企子公司名补齐：保利
    南方/宝钢/柳州钢铁/海钢/蓝星/中重汽/先正达等）；只增不删。`soe_control_
    overrides` 4 只中仅央企属性 2 只（中国海油/四方股份）豁免央企门（宇通/潍柴
    =地方国企照常剔）。**生产 yaml 保持 false，生产池 70 只候选/329 基础零变化**。
  - **回测对照**（2016-11-11 ~ 2026-11-11 十年，v8.1.0 全国资池 → v8.1.1 仅央企）：
    - **十年动态池（PIT 诚实口径）**：总收益 +43.6% → **+72.0%**（投入 56.1 万
      → 23.3 万，盈亏 +24.5 万 → +16.7 万，期末净值 80.6 万 → 40.0 万）；
      触发 68 → 29 只；胜率 73.5%（50/18）→ 72.4%（21/8）；买入 254 → 106 笔；
      分红/价差占比 38.3/61.7% → 24.9/75.1%。
    - **静态池（前视口径，仅对照买法）**：329 池 +78.4%/83.1%/2,842 万 →
      103 池 **+49.4%/84.0%/601 万**（触发 320 → 100）。
    - **结论**：收益 +28.4pp、绝对盈亏 −31%、投入 −59%——18 只亏损股中 11 只
      地方国企（万科 −76%/上汽/陆家嘴/广汽等，合计 −5.6 万）全部挡在池外；
      但 **6 只真央企亏损股分毫未减（−15,667：华侨城 −68%/保利 −42%/天山
      −56%/鞍钢/宝信/ST际华）**——央企身份防「消失/跑路」，不防「行业下行」。
      池子从 68 只触发缩到 29 只，分散度下降（最深浮亏仍 −78.1%，华侨城）。
- **v8.1.0**：策略语义变更——首仓双条件 + 加仓锚点 P0（Joel 2026-11-12 定案：
  「不管首次建仓还是二次建仓，都要同时满足高点回撤与 5 年分位两个条件，保证
  股价在长期低位且在下行通道的时候才开始买入」）
  - **首仓（任何一轮 k=0，含再武装后第二轮）= 双条件**：① hfq ≤ 本轮高点×
    (1−first_drawdown_pct) 且 ② N 年收盘分位 ≤ price_pctile_max（默认 5 年 40%，
    逐 bar PIT，`engine.first_ok_series` 与 selection_timing 页面同口径；窗口
    <60 bar fail-closed）。再武装只是状态重置（k 归零 + P0 清零 + 刷新高点），
    **无独立买入权**。
  - **加仓锚点 = 首仓价 P0**（原为轮高 H）：P0 = 本轮首仓信号价（首仓当根 bar
    hfq 收盘）；hfq ≤ P0×(1−k×add_step_pct) 买 k+1 手；加仓无分位条件。
  - **生产/回测统一**：单点 `run_stock(..., first_ok=…)`——页面「当前信号」
    模拟（state_rows）、微信首仓/加仓推送（v7.0.23 起本就双条件）、触碰判定
    （signal_touch）、G7 十年回测门（selection.refresh_selection +
    dynamic_pool._rolling_g7）、静态/动态回测（CLI/Web）全部同口径；回测 load
    起点前移 5 年喂分位窗口（`engine.extend_start`），算完切回原窗口。
  - **f6 基线重生成**（策略口径变更标准流程，逐字段漂移留痕）：买点时间表
    变化——长江电力 1→0 买（双条件未触发，rearm 16 次）、中国神华 9→2 笔（首仓
    2018-03→2020-02，等分位回落）、五粮液 22→9 笔（首仓 2016-12→2021-08）、
    格力 43→16 笔（首仓 2018-02→2023-10）、招商银行 22→6 / 中国建筑 18→6 /
    中国海油 10→1；f6 测试改读 buys.csv（summary.md 时间表段退役）+ never 股
    nan 断言。
  - **回测对照**（2016-11-11 ~ 2026-11-11 十年，v8.0.13 → v8.1.0）：
    - **十年动态池（PIT 诚实口径）**：总收益 +28.2% → **+43.6%**（投入
      219.9 万 → 56.1 万，盈亏 +62.1 万 → +24.5 万）；触发 117 → 68 只
      （双条件挡住 103 只）；胜率 64% → **73.5%**（50/18）；买入 719 →
      254 笔；分红/价差占比 40.4/59.6% → 38.3/61.7%。
    - **静态 329 池（前视口径，仅对照买法）**：+61.2% → **+78.4%**（投入
      9,892 万 → 2,842 万）；胜率 78.4% → 83.1%；触发 329 → 320。
  - 文件：`martingale/engine.py`（first_ok_series/extend_start/run_stock 双
    条件+P0/state_rows+tp）、`__main__.py`（backtest/status/selection 入口）、
    `selection.py`（refresh_selection+tp）、`dynamic_pool.py`（_rolling_g7/
    _month_pool/pool_schedule/主回测+tp）、`web/app.py`（_strategy_state_rows
    自动带 tp）、`scripts/signal_touch.py`（tp）、`tests/test_f6_regression.py`
    + fixture 重生成、README。

- **v8.0.13**：动态池回测（Joel：按股票池回测出现一堆没见过的股票——应**先算
  当天股票池**再看建仓/加仓，每天池子不一样）
  - **根因**：旧「按股票池回测」实际用 `build_pool` **基础池（329 只）**固定回测
    全窗口，且非候选池（70 只，G7/G8/G9 未应用）——今天才达标的股从窗口第一天
    就被回测。
  - **`martingale/dynamic_pool.py`（新模块）**：`month_starts`（每月第一个交易日）
    + `pool_schedule`（逐月 PIT 重算全漏斗：基础 6 门 point-in-time ∩ G7 滚动
    十年策略回测 ∩ G8 TTM ∩ G9 价格区间，不含手动池）+ `build_code_mask`（bar
    级交易掩码：适用池=日期 ≤ bar 的最近一次重算，首次重算日前禁交易）+
    `run_dynamic_pool_backtest`（重算→并集→带掩码逐股回测→产物）。
  - **引擎**（`engine.py`）：`run_stock(..., trade_mask=None)`——False bar 只冻结
    买入块（首仓/加仓），状态机其余（高点/再武装/分红/浮盈）照常、档位不推进
    （出池期间跌穿档位不消费，回池当日按现价补买=「暂停」语义）；`None` 缺省
    **逐位=既有行为**（f6 14 例零 diff 验证）。
  - **PIT 漏斗**（`universe.py` +`build_pool_asof`/`_soe_set_asof`）：生产
    `build_pool` **零改动**；退市/ST 按 as_of 从严、股东快照取 ≤ as_of 最近季度、
    估值/价格/分红/K线行数 `date ≤ as_of`。
  - **产物**（`report.py`）：动态池 `per_stock.csv` +`pool_months/pool_first/
    pool_last`；新增 `pool_timeline.csv`（月度池规模+入池/出池名单）；
    `status.json` +`mode="dynamic_pool"` +`pool{n_recomputes, recompute_dates,
    min_pool, max_pool}`。静态产物逐位不变。
  - **CLI/Web**：`--dynamic-pool`（与 `--codes` 互斥）；`/api/bt/runs`
    `dynamic_pool` 字段（与 codes/pool 互斥 400）；回测页签「动态股票池
    （月度重算，较慢）」radio 与静态并存（A/B 对比池漂移影响）+ per_stock 表
    在池月数/首次入池/最后入池 三列。
  - 测试：`tests/test_dynamic_pool.py` 新增 7 例（合成湖：PIT 漏斗、月度门控、
    掩码语义、引擎冻结/分红照收/None 恒等）；web +1 例；前端 +2 例 + 既有
    radio 索引适配（2→3 选）。

- **v8.0.12**：候选池 +「累计分红」列（Joel：中国外运股息率 4.12% 但分红占比
  才 7.15%、余额才 2301——余额口径易误读；占比口径不变，加累计分红透明展示）
  - `selection_backtest_10y` 本就有 `dividends`（累计分红）列，**零 schema 改动、
    零重算**——纯透传：`Member.bt_dividends` → `/api/pool` → 池表
    「分红余额 / **累计分红** / 价差盈亏」三列并排。
  - 口径说明：分红占比仍 = 分红余额/总盈亏（严格拆解、与上涨占比和=100%）；
    余额 = 累计分红 − 策略再投买股（v7.0.36 分红优先出资）。例：中国外运
    累计分红 9,624 元，76% 已自动再投，余额 2,301 元 → 占比 7.15%。
  - 测试：后端 +2 断言（54/54 定向）；前端 PoolMembers +1 断言；README 同步。
- **v8.0.11**：候选池「分红占比 vs 上涨占比」拆解 + 总市值小数修复
  （Joel：四个指标应加在股票池；池的总市值列小数太多）
  - **预计算表**（`martingale/selection.py`）：`selection_backtest_10y` +
    `div_pool`（分红余额）/ `price_pnl`（价差盈亏）两列；`ensure_table` 对旧表
    自动 DROP 重建（整表刷新快照，安全），`refresh_selection` 落盘。
  - **候选池**（`build_candidate_pool` + `/api/pool`）：成员 +4 字段
    `bt_div_pool` / `bt_price_pnl` / `bt_div_share` / `bt_price_share`（占比 =
    分红余额/总盈亏、价差盈亏/总盈亏，百分数口径同 bt_pnl_pct；总盈亏≤0 或
    旧表 → null）。已重算生产表（329 只，7.68s）。
  - **候选池表**（`PoolMembers.vue`）：+4 列 分红占比/上涨占比/分红余额/价差盈亏；
    **市值 2165.50348228 → 2165.50**（全部数值列挂 fmt：金额 2 位、比率带 %）。
  - 线上实测：神华 分红 22.49%/上涨 77.51%、陕西煤业 4.58%/95.42%、
    宇通 17.94%/82.06%（70/70 成员占比齐全）。
  - 测试：后端 +2（54/54 定向）；前端 +2（267/267）+ build。
- **v8.0.10**：回测结果「分红占比 vs 上涨占比」拆解（Joel：低波红利策略导向——
  分红占比更大更符合预期）
  - **`per_stock.csv`**（`martingale/report.py`）：+`div_pool`（分红余额）列——
    原引擎报告专用键，现落盘供 Web 拆解（f6 按 fixture 键遍历断言，加列安全）。
  - **回测 per_stock 表**（`BacktestTab.vue`）：+4 列——分红余额 / 价差盈亏 /
    **分红占比** / **上涨占比**。口径与 summary.md「拆分」行一致：
    分红占比 = 分红余额 / 总盈亏、上涨占比 = 价差盈亏 / 总盈亏（恒等式
    总盈亏 ≈ 价差盈亏 + 分红余额，差额为手续费）；总盈亏 ≤0 不拆、旧产物
    （无 div_pool 列）显示 —。
  - **顺修显示 bug**：`盈亏%` / `最低浮盈%` 列此前把比率裸显示成「3.12」挂
    「%」列名下（实为 +311.9%），与 summary.md 对不上 → 百分比格式化。
  - **summary.md**：Top/Bottom 10 表 +「分红/上涨」占比列（n>20 的大回测也可见）。
  - 测试：前端 +2（265/265）；后端 +1（bt_results div_pool 透传/旧格式）。
    线上实测 3 只高股息股：格力 分红 74.1%/上涨 25.9%（红利主导）、
    神华 22.5%/77.5%、长电 27.6%/72.4%。
- **v8.0.9**：股票搜索栏统一模糊匹配（Joel：策略/回测/数据湖搜索栏做成「我的持仓」那种模糊匹配）
  - **`/api/stocks/search`**（`web/app.py`）：+`include_delisted` 参数（缺省 False =
    持仓录入/手动池不出现退市股；True = 含退市，对齐旧 `/api/lake/search` 的
    stock_master 全表语义）；索引项 +`delisted` 标记。
  - **数据湖搜索栏**（`LakeSearchBar.vue`）：端点 `/api/lake/search`（仅
    ts_code/name LIKE）→ `/api/stocks/search?include_delisted=1`——与「我的持仓」同口径：
    代码数字 / 中文名 / 拼音首字母（zgsh）/ 全拼（shenhua）；退市股下拉带「（退市）」标记；
    503 lake_locked 与 409 灌数锁同中性处理（不弹红横幅）。
  - **回测「指定标的」输入**（`BacktestTab.vue`）：新增末段模糊搜索下拉——只对最后一个
    逗号段做候选，选中回写 `ts_code,`（续填下一只）；含退市股；短码/空段不发请求。
  - **策略页**：手动池录入条（ManualPoolBar）v7.0.12 起已是同口径，无改动。
  - 测试：前端 +10（LakeSearchBar 6 / BacktestTab 4，263/263）；后端 +1
    （include_delisted 分支，test_martingale_web 41/41）。
- **v8.0.8**：A股候选池扩容——行业全放开 + 门槛提档 + 国企控股强度门
  （Joel 2026-11-11：行业限制漏掉证券/保险/黑有色金属/家电/食品/建筑等行业的
  央国企；"要全部放开…保证没有伪国企…股息 3.5%、十年回测 30%"）：
  - **config**（`martingale/config.py`）：`industry_whitelist_csric2: ["*"]` =
    全放开语义；universe +4 可选键 `min_soe_ratio_pct` / `min_soe_sum_ratio_pct`
    （均 0 = 门关闭，向后兼容）/ `exclude_codes` / `soe_control_overrides`。
  - **universe**（`build_pool`）：SOE 交后插入**国企控股强度门**——最大单一
    国有占比 ≥ 8% **或** top10 国有合计 ≥ 30% **或** overrides 豁免；
    `exclude_codes` 硬剔除；funnel +`soe_ratio` 计数。8/30 定值 = 湖数据实测
    断层（伪国企 ≤5.58% / 真国企 ≥8.21%）。
  - **生产 yaml**：白名单 → `["*"]`；⑦ 20 → **30**；⑧ 3 → **3.5**；
    `exclude_codes: [sh.603766]`（隆鑫通用=民营控股）；overrides 4 只
    （中国海油 BVI 控股/四方股份国网系/宇通客车郑州国资/潍柴动力潍坊国资——
    湖数据 top10 标注缺口豁免，逐项理由留痕）。
  - **结果**（as_of 2026-11-11，生产湖重算 selection 7.7s / timing 2.0s）：
    候选池 **59 → 70 只**（全行业 5,029 → SOE 2,204 → 强度门 1,580 →
    市值 426 → 分红 329 → ⑦ 170 → ⑧ 70）。伪国企 12 只（美的/格力/青年服饰/
    南山铝业/际华/亿联/海澜之家/新奥/恒电东方/宝丰能源/隆鑫通用/平安银行）
    全部在池外；旧池出局 3 只：平安银行、新奥股份（伪国企剔除）、中国海油
    （⑦ 21.78% < 30%，override 救入基础池但被⑦挡——门槛提档的预期结果）。
  - **Web**：4 治理键进 strategyMeta 元数据守卫（FIELD_LABELS/DESC/TYPE）+
    HIDDEN_FIELDS（yaml-only 不渲染表单行；PUT 全量 JSON 底回传不丢键）；
    股票池卡片标题/漏斗文案更新（基础池/回测≥门槛/股息率≥门槛）。
  - 测试：后端 +11（`tests/test_soe_control.py` 合成湖零网络）；
    `test_v7_guards` universe 键集守卫 11 → 15 键。
- **v8.0.7**：我的持仓 tab 实体化 + Gate 模拟盘接入（Joel 2026-11-11：
  "把我的持仓也建起来" / "先页面上落地"）：
  - **`/api/positions`**：策略当前轮（纸面持仓）——每个活跃币按生效参数
    （页面覆盖 > yaml）全史回测取 open_round（建仓价/级数/数量/均价/
    现价/市值/浮盈/止盈线）；+ Gate 模拟盘状态（60s 缓存探测）。
  - **`crypto/live/gate.py`**：Gate testnet API v4 最小客户端（纯 stdlib，
    HMAC-SHA512 签名；base 代码层锁定 `api-testnet.gateapi.io` 模拟盘，
    拒绝实盘 URL；Key 读仓库根 .env `GATE_API_KEY/SECRET`，只读不回显）。
  - **PositionsTab.vue**：策略当前轮表格 + Gate 模拟盘状态卡
    （连通/余额 或 未连通原因）；当前纸面持仓 = 模拟盘镜像基准
    （BTC 0.002217，2026-02-05 建 L7，均价 60,155，止盈线 188,730）。
  - 测试：后端 +2（open_round / 无开仓轮，gate 状态 monkeypatch 不打网络）、
    前端 +2（表格渲染 / 空态+余额）；crypto 38/38 + 前端 15/15 绿。
- **v8.0.6**：ETH 退役（Joel 2026-11-11："把 eth 也退役吧，只保留 btc"）：
  yaml `coins` 只留 BTCUSDT；sync 自动置 ETHUSDT `coin_master.is_active=FALSE`
  （湖内 80,841 根 1h 数据保留，恢复 = 加回清单重跑 sync）；crypto-gateway
  重启后 1 标的流（BTC live）；回测/寻优/Web 币列表全部跟随（默认币源 =
  yaml coins 或湖 is_active）。退役币累计 19（17 + SOL + ETH）。
- **v8.0.5**：追踪止损失败出场升级（Joel 2026-11-11："超过建仓价的
  1.5 倍就开始追踪止损，回撤 20% 就平仓"）：
  - **引擎**：`StrategyParams` 增 `trail_activation_mult`（激活：价 ≥
    建仓价 × 1.5）+ `trail_drawdown_pct`（平仓：自激活后最高收盘回撤
    20%）；`take_profit_mult=None` 关闭固定止盈；两者并存先到先了结，
    轮次记录 `reason`（tp/trail）+ `peak_price`；持仓中报
    `trail_on/trail_peak/trail_stop`。yaml 可选键
    `trail_activation_mult`/`trail_drawdown_pct`/`take_profit_mult: null`。
  - **runner**：参数段/轮次表（了结方式列）/持仓中均展示追踪状态。
  - **paramsearch**：`--trail-act` / `--trail-dd`（%）。
  - **近 5 年回测（15 U/1.03/70%，3000 U 预算）结论：纯 20% 追踪
    显著差于固定止盈**——BTC +211 U（vs 5x 的 +2,371 U）：2023 盘整
    期（峰 31.6k 回撤 20%）被甩出在 1.25×，完全错过 2024 主升
    （2024-12 才到 5x）；ETH +2,033 U（vs 3x 的 +3,137 U）。加密牛市
    中 20~30% 回撤是常态（2023 多次、2024-08 一次 30%），1.5× 激活 +
    20% 回撤会在盘整期以 1.2~1.5× 出局。敏感性：1.5×/30% BTC +939、
    2×/15% ETH +2,225，均仍不及固定止盈；**50% 回撤追踪 = 纯保险**
    （BTC 全程未触发、结果与纯 5x 相同；ETH 2025-03 的 50% 回撤触发，
    1.39× 了结保住了 +39%，但市场 5 个月后给了 3x）。**维持固定止盈
    为主出场（BTC 5x / ETH 3x）**；要追踪就 ≥50% 当灾难保险。
  - 测试：+6 例（激活/未激活前回撤/单调上涨持有/浅回撤存活/固定止盈
    优先/yaml null TP）；crypto 36/36 绿。
- **v8.0.4**：paramsearch 加 `--lookback`（高点回看窗口，年；缺省 =
  yaml 8760h）。验证：1.5 年 vs 1 年高点，2021-11→2026-11 窗口两币
  同配置结果完全一致（唯一入场 2022-06，两窗口锚定高点均为 2021-11
  顶；1.5 年只在"13~18 个月前高点 > 近一年高点"时才会更早触发建仓）。
- **v8.0.3**：寻优报告加**净收益（绝对 USDT）**列（Joel 2026-11-11：
  "不看回报率看净收益……3000 只用 487 剩下吃灰"）。`_metrics` 增
  `net = 期末净值 − 外部投入`；`param_search_3k_fine.md` 重生成含净收益
  列。要点：5 年窗口 BTC 最深 L22，净收益 ≈ 投入×ROI（ROI 恒定），
  资金利用率随 base/growth 单调升（30U/1.1 投 2,386 U 净 +12,042 U 但
  仅覆盖 L24；15U/1.02 投 433 U 净 +2,095 U 覆盖 L80，其余 2,567 U 为
  L23~L80 深跌储备而非闲置）。
- **v8.0.2**：寻优细化——起始资金 5 U 步长 + 预算覆盖深度（Joel
  2026-11-11："3000 不是硬限制……比特币可能大概跌去 80% 就是上限"；
  "10-30u 之间太粗了，还要细分到 5u 步长"）：
  - **paramsearch**：新增预算覆盖深度指标（`_cover_levels`，解析式
    base·Σgr^j ≤ budget + 浮点边界校验）——报告加"预算可至 L{n}"列 +
    起始×加仓覆盖深度矩阵（L0 = 首仓，L80 = 较建仓价再跌 80%）。
  - **寻优**：base 5/10/15/20/25/30（5 U 步长）× 回撤 50/60/70 × 止盈
    3/5/8/10 × 加仓 1.02/1.05/1.08/1.1 = 288 组合/币，3000 U 预算，
    报告 `data/crypto/param_search_3k_fine.md`。
  - **结论（3000 U 覆盖到 L80 的充要条件）**：加仓 1.02 且起始 ≤15 U
    （5 U→L128、10 U→L97、15 U→恰 L80）；加仓 1.05 最深仅 L69（5 U）、
    1.08 仅 L49、1.1 仅 L42——**"加仓比例尽量小"的直觉在 3000 U 体量
    下成立**：只有 1.02 撑得到 80% 极限。代价很小：BTC 70%/5x 回报率
    1.02→1.1 仅 +484%→+505%（5 年），而覆盖深度 L128→L42。同一 growth
    下 ROI 与起始资金无关（各 base 回报相同），起始资金只是"投入规模
    × 防御深度"的权衡（base 15/1.05：621 U 中 +492% 但只到 L48；
    base 5/1.05：207 U 中 +492% 到 L69）。
  - 推荐（3000 U，双币合计）：深防御 BTC 15 U/1.02/5x + ETH 15 U/
    1.02/3x → 均覆盖 L80，5 年 +484%/+297%，合计投入 1,275 U 留
    1,725 U 储备；均衡型 BTC/ETH 各 10 U/1.05/5x/3x → +492%/+313%
    （覆盖 L55）。
  - 回归：crypto 30/30 绿（paramsearch 纯新增指标，无引擎改动）。
- **v8.0.1**：回测资金预算 + 起始资金寻优（Joel 2026-11-11："目前投入
  3000usd，基于这个资金量看看起始资金和加仓比例设置多少"）：
  - **引擎**（`engine.run_coin`）：新增 `external_cap` 外部投入硬预算
    （USDT，累计口径）——买不起的档位**整档放弃、不做部分买入**：首仓
    买不起 → 不建仓；加仓买不起 → 该轮截断（`capped=True` 标记，报告
    ✂）。余额（了结所得滚存）不受预算限。缺省 None = 无限（原行为）。
  - **runner/CLI**：`run_backtest(..., external_cap=…)` 透传；报告轮次
    表加 ✂截断 标记。
  - **paramsearch**：新增 `--base`（起始资金网格，默认
    10,30,100,300,1000）+ `--budget`（外部投入硬预算 USDT）；报告表加
    起始资金列 + 外部投入/预算（✂=被截断）。
  - 近 5 年 3000 U 预算寻优报告 `data/crypto/param_search_3k.md`
    （5 起始资金 × 3 回撤 × 4 止盈 × 4 加仓 = 240 组合/币）：**起始
    10~30 U 单位效率最高**（70% 回撤整轮只需 288~2386 U，预算宽松）；
    起始 ≥100 U 深跌段被截断（1000 U 首仓只买得起首仓+1 级），单位
    效率递减（BTC 70%/5x：base30 每 U 外部 6.05 U 期末 → base1000 5.18 U）；
    加仓倍数 1.02~1.10 在 3000 U 内都装得下（base≤30），ROI 随 growth
    略升（深档均价更低）——预算不是压制 growth 的理由，budget 约束只在
    base≥100 或 50/60% 浅回撤时生效。推荐：BTC 30 U / 70% / 5x / 1.05
    （外部 2,386 U → +492%，2024-12 落袋）；ETH 10 U / 70% / 3x / 1.05
    （外部 1,077 U → +313%，2025-08 落袋）。
  - 测试：+3 例（预算截断精确对账 / 首仓买不起跳过 / None=原行为）；
    crypto 46/46 绿。
- **v8.0.0**：major 版本——加密货币系统升格为双系统主特性（2026-11-11 Joel
  「推成 v8」）；A 股侧零改动（策略/数学/数据不变）：
  - 加密货币模块系列（v7.0.42~v7.0.55，详见下）：旧 Quant 清理 + 双系统登录
    解耦 → 加密框架（同仓库双系统定案）→ 数据湖全史 + 实时 K 线网关 →
    聚焦 2 币（SOL 退役）→ 443 单域名 HTTPS → 策略 + 回测落实到页面 →
    编辑模式 / 马丁格尔品牌统一 → 图表时间轴 + 交易事件标记 → 双图（净值
    + 价格）+ 总外部投入 → 回测时间范围 → 近 5 年参数寻优
  - README 统一更新：标题 / 引言双链路 / §5.5 加密货币章节（新增）/
    §6 定时任务 6 条现状 / §7 测试数 / §8 目录结构全部对齐 v8 现状；
    A 股页面页脚版本标注 v7.0 → v8.0
  - 入口：`https://stream04l4.duckdns.org/`（A 股）/ `/crypto/`（加密货币）

- **v7.0.11**：持仓股到策略点位 → PushPlus 微信提醒（timer 盘中 10 分钟节奏，
  去重 + 实时价回退湖价）。
- **v7.0.12**：候选池（5 条件 ∩ 回测为正 ∩ 股息≥3% ∩ 价 3~100 元 ∪ 手动）
  收敛进策略页签「股票池」卡片 + 59 只全列表 + 手动添加；移动端视口修正。
- **v7.0.13**：选股时机（§2.5）——「策略参数」卡改名**持仓策略**，新增
  **选股策略**卡（3 条件 + 今日选股表 + 一键加持仓）；`timing` 子命令 /
  `selection_timing` 湖表 / `GET /api/timing` / 边沿状态微信新触发推送。
- **v7.0.14**：登录门禁——全部 `/api/*`（除 login/logout/health）需登录；
  无状态签名 cookie（`web/auth.py`，HttpOnly/SameSite=Lax/7 天），凭据在
  `.env`（`WEB_ADMIN_USER`/`WEB_ADMIN_PASS`/`WEB_SESSION_SECRET`，禁入 git/日志）；
  前端登录页（未登录不渲染页签）+ 顶栏「退出」；连续 5 次失败锁 120s（429）；
  运行中 API 401（会话过期）自动回登录页。
- **v7.0.15**：持仓弹层「📝 买入登记」——加仓/首仓信号触发、券商实盘买入后，
  点持仓行弹层内登记「本次股数 + 本次价格」即更新当前持仓：新总股数 = 旧 + 本次，
  新成本价 = 新旧加权平均（3 位小数，券商口径不含手续费）；有活跃信号时股数
  按建议金额÷现价预填整手、价格预填最新价；登记前实时预览合并结果；
  复用 `/api/my_positions` PUT upsert（后端零改动），刷新后策略档位自动重算。
  （v7.0.16~v7.0.18.4 详见 git log：登录/回测/湖同步运维等修补。）
- **v7.0.19**：我的持仓页新增「📡 策略信号中心」卡——股票池 ∪ 账本全量，
  三档分段：当前信号（价已触发：持仓→加仓信号 / 非持仓→买入信号，含建议金额、
  10 年回测收益与档数标注、价源标注）、即将进入信号区（approach_pct=3.0%
  纯显示带宽，不进 config/Web schema）、历史信号（**仅近 90 天**，日期降序，
  首仓/第 N 档标注 + **微信推送标记**——只读 `pushplus_alert_state.json` 按
  `代码|动作|档` 前缀匹配，缺失/损坏按未推送）；区分持仓/非持仓。
  **时机门**：非持仓股买入信号须过选股时机（`selection_timing.push_eligible`
  = 价格分位 且 股息率），未过只显示价格状态；时机列**逐条件 ✓/✗ 明细**
  （每条带数值+阈值——整体过/未过都列明哪条已达成哪条未达成，端点由
  selection_timing 原始字段 + config 阈值拼出）；持仓股不受时机限制
  （只给加仓信号）。后端 `GET /api/strategy_signals`（逐股状态机回放，
  `classify_signal` 四态分类，现货价→湖价降级，湖锁定降级只列账本代码，
  与微信提醒同口径；触发价按 in_pos 分支：in_pos=模拟 next 档，未 in_pos=
  首仓线——老股 entry_high 锚点会致非持仓误报）；状态机 buys 元组 3→5 元
  （追加 k 档下标 + first_of_round，前 3 元逐位不变，f6 红线守护）。
- **v7.0.20**：信号中心「盘中触碰保留」（Joel 2026-11-08：盘中达成信号又涨回
  出区的，保留一段时间、第二天再移入历史信号）——
  - **判定源**：腾讯行情新增当日最高/最低（`screener/data/tencent.py`
    IDX_HIGH=33 / IDX_LOW=34，实测确认）；触碰 = **最新行情日最低价 ≤ 触发线**
    （与 active 判定同向；停牌股行情停在旧日 → 不算「今日触碰」，须行情日=全池
    最大行情日）。
  - **当前信号** = 现价在区内 **或** 今日已触碰（`status=touched`「已触回」，
    动作/金额按 active 同口径补齐；非持仓股仍受时机门约束）；新增「日内低」列；
    行情日滚动后自动移出当前段。**段归属定案（Joel 2026-11-08 v2）**：当前信号
    仅含 持仓股（加仓信号）+ 非持仓股的**买入信号**（in_pos=false → 触发线=
    首仓线）——非持仓但模拟已建仓（in_pos=true）的股票触发线是模拟加仓档，连
    建仓都未建，不入当前信号（前端过滤；其触碰事件仍落历史信号）。
  - **历史信号** = 状态机回放 buys ∪ 触碰事件（同 代码|日期|档 去重，**回放
    buy 优先**——收盘过线那天的触碰不重复记），同 90 天窗口。
  - **事件落盘**：`scripts/signal_touch.py` 每交易日 12:05 UTC（北京 20:05，
    lake-sync 灌完当日 bar 后 → job 信号线与页面同口径）写
    `data/alerts/signal_touch_state.json`（原子替换、90 天滚动、同日重跑幂等；
    退出码 0=正常/2=配置错/3=湖不可用/4=行情失败，失败下轮重试不清状态）；
    Web 端点只读，缺失/损坏按无事件（fail-safe）。
  - 实现下沉：`_strategy_state_rows` → `martingale.engine.state_rows`（job 与
    Web 共用单一口径防漂移）；`martingale/touches.py` 纯函数模块（compute_touches
    / merge_events / dedupe_buys / quote_date_of）。状态机数学零改动（f6 红线
    守护）；approach_pct 仍为纯显示参数。
- **v7.0.21**：af 基准假跳变防复发护栏（2026-11-08 大秦铁路 5 年分位 0.08%
  假值复盘）——根因：11-04 tushare 批量超时 + sina 持续超时 → 多源池按可用性
  换源 → **同一股票 af 序列中途换基准**（15% 假断崖；af 绝对尺度各源任意定义、
  同股时间序列一致性才是不变量）→ qfq 断步 → 分位钉底。日增 `rebase_recent`
  设计上次日才收口，窗口内跑的 timing/selection 快照会被污染。护栏：
  - `martingale timing` / `selection` CLI 开跑前先执行同一幂等 `rebase_recent
    (days=10)`（`_af_prealign`；命令本就持写连接；已对齐零写入、生产实测 +2s，
    失败不阻断记痕）→ 快照恒基于已治愈序列（周末手动运维亦安全）；
  - `run_lake_sync_daily.sh` selection/timing 步骤加 DuckDB 锁竞争重试（2026-11-08
    实锤：湖进程退出瞬间锁未释放 → 两步 rc=1 回退旧快照）；
  - 全市场核查：全史 `repair_all_af` 0 假跳变、无任何「qfq 分位钉底而 raw 正常」
    异常股（残余最新 bar qfq≠raw 为无害恒定锚偏移，分位比较中自消）。
- **v7.0.22**：策略信号中心「即将进入」段加时机门（2026-11-09 Joel 定案：「非
  持仓股即将进入买入时机，或持仓股即将触发加仓才被筛选出来」）——此前该段为
  纯价格带（现价在触发线 3% 以内），时机门（v7.0.19 定案）只作用于**动作**
  （非持仓时机未过 → 无买入动作，但价格状态照展示）；实例：中国海油非持仓、
  分位 85.7% ✗✗，价格距模拟加仓档仅 2.49% 仍显示在段内，却永远发不出买入
  信号 → 噪音。规则：`approaching 且（持仓 或 timing_ok=true）`——持仓股加仓
  不受时机限制照展示；非持仓股时机未过/无时机行（fail-closed）不入段。实现：
  前端 `MyPositionsTab.vue` 段过滤（段归属 v2 同位置，状态字段仍是价格状态
  事实；API `totals.n_approaching` 仍为原始价格带计数）+ 前端测试正反例
  （非持仓时机过展示 / 非持仓时机过未过隐藏）。
- **v7.0.23**：非持仓首仓改**双条件**入场（2026-11-09 Joel 定案：「这两条线
  应该在建首仓的时候都满足——同时满足时机成熟，并且已经距离高点已有一定下跌，
  这样才开始建首仓；首仓后再遵循持股策略」；建仓/加仓自此为两个独立策略）：
  - **取线单点** `martingale.touches.trigger_price(row, held)`：持仓股口径不变
    （in_pos→next 档加仓线；未 in_pos→首仓线）；**非持仓恒用首仓线**
    （first_level_raw = 本轮高点×0.85）——替代 v7.0.19 的「非持仓 in_pos 取
    next 档」分支。旧分支在锚点高、老轮次首仓线远低于市价的老股上会产出
    「模拟加仓档」幽灵信号；新口径下价条件 = 「已距高点回撤到位」，时机门
    （分位≤40% 且 股息≥4.5%，fail-closed）兜住估值侧，两者都过才出买入·首仓
    信号（金额 initial_amount）；建仓后录入「我的持仓」→ 自动转持仓策略
    加仓档（越跌越买）。
  - **页面**（`/api/strategy_signals` + `MyPositionsTab.vue`）：非持仓股触发
    线/动作/金额全部按首仓口径；「当前信号」段过滤改为 `持仓 或 timing_ok`
    （非持仓时机✗的价格态不入段，与「即将进入」同一时机门）；非持仓动作统一
    「买入·首仓」、档位标签「首仓（第 1 档）」。
  - **微信推送同步改**（两条策略线各一条通道）：① `position_alert.py` 新增
    **非持仓首仓通道**——盘中每 10 分钟检查候选池非持仓股「时机✓ 且 现价≤
    首仓线」→ 推「策略点位提醒」首仓买入（含分位/股息率明细；同点位同日去重
    键 `code|buy|0|首仓线`；账本空不再早退；行情失败回退湖价只漏报不误报）；
    持仓股加仓通道口径不变。② 选股时机推送（`timing.py`）措辞从「新进入买入
    区」改为「时机成熟（首仓待价）」——时机只是首仓双条件之一，破线后的首仓
    买入信号由盘中策略点位提醒推送，footer 交代两条策略线衔接。
  - 验证：后端定向 + 全量回归绿（首仓双条件纯函数/端点用例、touches 持仓
    口径两用例、position_alert 首仓通道 3 用例【双条件推/时机缺行
    fail-closed/价未破不推】、timing 措辞断言）、前端 248 绿（大秦真实案例
    入段 + 江苏银行时机✗反例出段）、线上实测：池内 65 只触发线全部取
    first_level_raw，7 只非持仓价破首仓线但时机✗ → 无买入动作（页面干净）、
    position_alert dry-run 无误报（4.1s）。
- **v7.0.24**：选股时机门改**单条件**「5年分位 ≤ 40%」（2026-11-09 Joel
  定案，基于 10 年回测：48 组粗网格 + 106 组细网格，62 只候选池
  2016-01-04→2026-11-06，生产状态机逐笔复刻、gate 只拦 k=0 首笔、
  等价性前置验证零 diff）：
  - **回测结论**：② 股息率门是总盈亏主要拖累（y=4.5 较关少 38~45%，且与
    池入场 G8「当前息率≥3%」同维度冗余——它筛的是历史买入时点而非池成员；
    窗口内 TTM≥4.5% 交易日占比仅 37%，3/62 只 10 年从未到过 4.5% → 门
    永久关）；③ 250 日线近乎无效（±2%）；① 分位对盈亏 30~70 基本平坦
    （±10%）但对质量单调有利（p40 vs p70：ROI 103.8% vs 97.5%、最差个股
    浮亏 -36.4% vs -46.6%、等待 64d vs 36d）。**p=40 = 质量偏好**（深跌才
    买）定案。
  - **门逻辑**（`martingale/timing.py` 单一定义点）：`in_watchlist =
    push_eligible = ① 5年分位≤40%`；② 股息率（yield_min_pct=4.5）、
    ③ 250 日线（below_ma250=false）**从门中移除、降为参考展示**（
    数据列/页面继续显示带数值，不带 ✓/✗ 门判定；config 键保留，策略库
    可继续调参展示）。
  - **首仓双条件不变**：非持仓首仓 = 时机✓（=①）且 现价≤首仓线；状态机
    数学零改动（f6 红线不受影响）。
  - **页面/文案**：策略页「选股策略」参数标签 + 今日选股表（说明行/表头
    「股息率%（参考）」/空态）、我的持仓信号模块脚注、strategyMeta.js
    字段描述同步单条件口径；微信推送措辞不变（「时机成熟（首仓待价）」，
    明细自动只剩 ①）。
  - **即时影响**：当前 6 只价破首仓线的非持仓股在 p=40 下全部仍被分位门挡
    （最接近的山煤国际 41.0%，差 1pp；光大银行 67.1%）→ 短期无新增首仓
    信号，属定案预期。
  - 验证：后端全量回归绿（test_timing 断言同步单条件语义、
    test_strategy_signals 时机明细字符串同步；636 全绿另见提交记录）、
    前端 248 绿（StrategyTab 标签断言同步；TimingStocks 说明行移入
    data 块修复未加载态读 data.params 崩溃）、线上重启后 /api/timing 与
    /api/strategy_signals 实测。
- **v7.0.55**：参数寻优工具（Joel 2026-11-11：对两种标的寻优"仅回测过去 5 年"，
  变量 = 建仓幅度/止盈倍数/加仓倍数，看交易频率/投入资金/回报率/资金周转效率）：
  - 新增 `crypto/backtest/paramsearch.py`（`python -m crypto.backtest.paramsearch
    --start --end --coins --drops --tps --grs --out`）：网格回测（默认
    回撤 50/60/70/80% × 止盈 3/5/8/10x × 加仓 1.02/1.05/1.08/1.1，每币 64 组合，
    2 币 ~17s），复用 v7.0.54 时间范围回测（窗口账户从空 + 前置高点基线）。
  - 指标：交易频率（了结轮次）、总外部投入、回报率（期末净值/外部−1，含浮盈）、
    资金周转效率 CAGR（(净值/外部)^(1/年数)−1）、均持仓天数、MDD；按 CAGR 降序。
  - 近 5 年（2021-11-11→2026-11-11）结论见 `data/crypto/param_search_5y.md`：
    两币最多 1~2 轮（触发罕见，集中在 2022 熊市）；**止盈 5x 显著优于 8/10x**
    （BTC 5x 2024-12-05 了结落袋、ETH 5x 2025-08-22 了结，8/10x 至今未触发
    仍持仓）；加仓倍数只放大投入规模（ROI 随 growth 略升）；回撤 80% 对 BTC
    五年内从未触发（机会风险），50% 仓位最深（L52~62，ETH 外部投入达 4 万 U）
    且 ROI 最低。
  - 测试：纯新增模块，crypto 43/43 回归绿。
- **v7.0.54**：回测时间范围（Joel 2026-11-11："这个回测得加个回测时间范围"）：
  - **口径**（定案）：窗口 = `[start, end]`（UTC 日界，start 含、end 含当日）；
    窗口内**账户从空开始**（不继承窗口前的持仓/余额）；但**一年高点**自动
    用窗口前 `lookback_h`(8760h) 根历史做基线（只算高点、不产生交易），
    避免窗口起点附近因"无前置高点"而误触发/漏触发。start/end 缺省 = 全史。
  - **引擎**（`engine.run_coin`）：新增 `h1y` 入参——外部传入的一年高点数组
    （与 `closes` 等长）；`None` 时回退自算 `rolling_high_1y`（向后兼容）。
  - **runner**（`backtest/runner.py`）：`load_bars(lake, symbol, lo_ts, hi_ts)`
    加窗口过滤；`_date_to_ts(date_str, end_of_day)`（YYYY-MM-DD → UTC epoch 秒）；
    `run_backtest(..., start, end)`——先把**全量**序列算好 `h1y_full`，再按
    `ts >= start_ts` 切片 `ts/closes/h1y`（空窗口返回 `n_bars=0` 空结果，不崩）；
    `render_report(..., start, end)` 报告头加"回测范围"行（仅带范围时）。
  - **CLI**（`backtest/__main__.py`）：`--start YYYY-MM-DD` / `--end YYYY-MM-DD`
    （格式/顺序校验 → 退出码 2）；`save_report(..., start, end)` 透传。
  - **Web**（`/api/backtest`）：body 加可选 `start`/`end`（`YYYY-MM-DD`，UTC）；
    校验失败 400（格式错 / start>end）；响应加 `range: {start, end}` 回显。
  - **前端**（BacktestTab）：工具条加两个 `<input type="date">`（start/end，
    留空 = 全史），标题动态显示当前范围（"全史" / "2020-01-03 → 最新"），
    提示文案说明"一年高点用窗口前 8760h 历史做基线"。
  - 真实湖验证（2019-2021 窗口，growth=1.05）：BTC 2019-01-01 建仓 3700.31
    → 2021-01-07 止盈 37150.66（10.04x）、投入 125.78 U、外部 125.78 U；
    ETH 同步 1 轮；CLI/端点/前端全链路一致；全量回归 **708 passed / 0 failed**
    （crypto 后端 43 + 前端 13，新增引擎 h1y / 端点范围/校验 5 例）。
- **v7.0.53**：回测双图（净值+价格）+ 总外部投入（Joel 2026-11-11："加个
  价格图也标明建仓/加仓/平仓；总结里要看总外部投入——首次建仓全外部，
  了结后余额滚入下一轮，余额耗尽再算外部"）：
  - **账户模型**（引擎 `run_coin`）：单账户资金闭环——`_fund()` 买入先扣
    **余额**（`wallet`，了结所得滚存，恒 ≥0），不足部分计 **外部投入**
    （`external_invested`）；净值曲线改为 **持仓市值 + 余额**（修正旧
    口径在持仓中重复计入本轮支出的问题；首轮余额恒 0，曲线不变）。
  - **价格图**：BacktestTab 每币新增第二张 SVG（对数坐标，1/2/5 网格
    y 轴刻度），事件标记落在**精确成交价**上（建仓▼/加仓●/平仓▲，
    与净值图同色同形、上下时间轴对齐）；`/api/backtest` 增 `price_curve`
    （与净值曲线同 400 点降采样）。
  - 总结模块加 **总外部投入** 统计卡；CLI 报告同步加口径说明。
  - 真实湖（growth=1.05）：**BTC 外部投入 1,781 USDT**（全在 2018 首轮，
    2022 轮 414 U 全部由余额覆盖）/ **ETH 10,208 USDT**（全在 2018 首轮，
    2022 轮 1,077 U 余额覆盖）；期末余额 BTC 26,791 / ETH 317,566，
    远未耗尽；MDD 不变（70.41/73.00，谷底在首轮、余额为 0 的时点）。
  - 顺带修复：清理 v7.0.48 测试遗留的 `strategy_params.json`
    （growth=1.03 覆盖，与拍板规格 1.05 不符，页面此前一直在 1.03 下回测）。
  - 测试：引擎 2 例新（余额滚存/耗尽追加外部 + 恒等式）+ 端点
    external/wallet/price_curve 断言 + 前端双图/刻度/总外部投入断言；
    crypto 38/38 + 前端 12/12；全量回归绿。
- **v7.0.52**：SOL 退役（Joel 2026-11-11："把 sol 去掉吧，也归入到退役币里"）
  —— 聚焦 BTC/ETH：
  - `crypto.yaml` coins 移除 SOLUSDT（退役清单 17 → 18 币，注明 v7.0.52）；
    `coin_master.SOLUSDT.is_active=FALSE`（**54,782 行数据保留**，恢复 =
    加回 coins 重跑 sync）。
  - crypto-gateway 重启 → 2 币 WS 组合流（纯内存态文件自动收敛，SOL 不再
    推送）；crypto-sync cron 读 yaml → 自动停同步 SOL。
  - 回测默认币 = yaml coins → 自动只跑 BTC/ETH（显式 `--coins` 仍可查
    SOL 历史）；数据湖 tab 主表 2 币、SOL 入退役折叠区（active 过滤
    天然支持，文案"聚焦 3 币"→"聚焦 2 币"）。
  - 验证：/api/lake/status active=[BTC,ETH]、退役 18 币（SOL rows=54782
    active=False）；/api/gateway connected 2 币；/api/backtest 默认 2 币
    （BTC 71 事件 / ETH 120 事件）；前端 12/12 + build + 重启 active。
- **v7.0.51**：回测图时间轴 + 交易事件标记（Joel 2026-11-11："图形里要能
  看出来时间，同时在线段上标注出什么时候建仓、什么时候平仓、什么时候能
  加仓，用不同标记区分"）：
  - 引擎 `run_coin` 返回新增 `events`（逐 bar 事件：entry/add/exit，含
    日期/级数/成交价；跳空同 bar 多级 → 多个 add 事件同日期）；端点
    `_ui_result` 透传。
  - BacktestTab 图表升级（仍内联 SVG 零依赖）：**线性时间轴**（6 个等距
    日期刻度 + 竖网格线；跨度 >366 天显示 YYYY-MM，否则完整日期）；
    曲线改按时间定位（与事件对齐）；事件标记 **建仓=蓝▼ / 加仓=灰● /
    平仓=红▲** 落在曲线上（二分找最近净值点），悬停 <title> 看明细
    （如"加仓 第42级 2022-06-18 @915.15"）；图例行；峰值标签。
  - 真实湖验证：BTC 71 事件（68 加仓）/ ETH 120 / SOL 108，末级
    L22/L37/L89 与持仓中轮次一致。
  - 测试：策略 3 处事件断言（逐级/跳空同级/了结序列）+ 端点事件断言；
    前端 BacktestTab 时间轴/标记/图例断言；全量 701 过 + 前端 12/12。
- **v7.0.50**：加密策略 tab 编辑模式与 A 股对齐（Joel 2026-11-11："都改成
  A 股那种，点编辑模式后才能改，保存后锁死"）——纯前端：
  - 默认只读展示 7 参数（数值文本，无输入框）；【✏️ 编辑模式】解锁表单；
    【💾 保存策略】成功 → 锁死回只读 + 提示；**400 拒绝保持编辑态**可改正
    重试；【取消】不发请求、重拉只读视图（setEditing 唯一入口，与股票
    StrategyTab 的 D-W02 语义一致）。
  - 【↺ 恢复默认】仅在只读态且存在页面覆盖时显示（直接 POST reset）。
  - 前端测试重写为 6 例覆盖全流程（只读/编辑/保存锁死/400 保持/取消/
    恢复默认）；crypto 前端 12/12；build + 重启 + 443 bundle 实测含新 UI。
  - 后端零改动（/api/strategy 端点语义不变）。
- **v7.0.49**：标题/文案统一马丁格尔品牌（Joel 2026-11-11："这套系统都是
  基于马丁格尔策略来的，标题和文本统一改一下"）——纯展示层，逻辑零改动：
  - 登录页（两系统同款）：H1 "双系统统一登录" → "马丁格尔 · 双市场统一
    登录"；副标 "A股4维选股 · 加密货币"（v7 已删多因子选股，旧词过时）→
    "A股深回撤金字塔加仓 · 加密深回撤网格 + 10x 止盈"；按钮 "登录
    A股4维选股" → "登录 A股（马丁格尔）"、"登录 加密货币（马丁格尔）"。
  - 股票顶栏 "深回撤金字塔加仓" → "马丁格尔 · A股"（副标保留策略特征）；
    页签标题 A股 title 标签 "A股四维选股 · 控制台" → "马丁格尔 · A股"；
    策略 tab "持仓策略（深回撤金字塔加仓）" 加马丁格尔前缀；页脚策略名同步。
  - 加密顶栏 "加密货币 · 框架 v7.0.43"（过时）→ "马丁格尔 · 加密货币 ·
    深回撤网格 + 10x 止盈 · 1h 数据湖 + 实时网关"；title 标签 → "马丁格尔
    · 加密货币"；页脚 "框架阶段…慢慢完善" 过时话术清除，改为策略/数据
    现状（顺带修正页脚湖路径笔误 data/lake/ → data/crypto/）。
  - 跨系统跳转链接：🪙 加密货币 → "加密马丁格尔"；📉 股票 → "A股马丁格尔"。
  - 测试：两套前端登录页断言同步（stock 253/253、crypto 8/8 全绿）；
    线上实测 443 直连双入口 <title> 已切换，bundle 含新文案。
- **v7.0.48**：策略 + 回测落实到页面（Joel 2026-11-11："先把这个策略和回测
  落实到页面中去，策略后续我再调整"）：
  - `GET /api/strategy`：当前生效参数（yaml 基线 + 页面覆盖）+ 规则说明 +
    默认值对照；`POST /api/strategy` 保存参数 → 覆盖文件
    `data/crypto/strategy_params.json`（运行时覆盖，不改仓库配置，子集可存、
    与既有覆盖合并）；`POST /api/strategy/reset` 恢复默认。7 参数带范围护栏
    （越界/未知字段/非数字 → 400）。
  - `POST /api/backtest`：全史回测（湖 1h → 引擎 → 逐币结果 + 净值曲线
    降采样 ≤400 点；3 币全史 ~0.8s 同步执行）；params 优先级：请求 > 覆盖
    文件 > yaml；端点层做引擎键→页面键归一（`_ui_result`）。
  - 前端 StrategyTab（参数表单 + 保存/恢复默认 + 规则卡）、BacktestTab
    （一键回测 + 逐币：了结轮次表/持仓中卡/净值统计/内联 SVG 净值曲线，
    零图表库依赖）。
  - 引擎小修：`run_coin` 空数据路径补齐 start/end/realized_cash/
    final_equity/dates 键（与正常路径契约一致，防端点 KeyError）。
  - 测试：tests/test_crypto_web.py 7 用例（TestClient + 合成小湖，
    覆盖文件 monkeypatch 到 tmp）；前端 StrategyTab/BacktestTab 4 用例。
    全量 701 过（694 + 7）；前端 8/8。
- **v7.0.47**：加密货币马丁格尔策略 + 全史回测（Joel 2026-11-11 规格，两决策门拍板）：
  - `crypto.yaml` 新增 **strategy 段**：建仓 = 价 ≤ 一年高点×30%（跌 70% 触发，
    首仓 10 USDT）；加仓 = 建仓价×(1−1%×k) 固定网格、第 k 级 10×1.05^k USDT
    不设上限；上涨不加仓、新高不刷新建仓价；止盈 = 价 ≥ 建仓价×10 全卖、
    entry 重置进入下一轮；fee 10bps；一年高点 = 滚动 8760h 收盘价最大。
  - `crypto/strategy/engine.py` 状态机（纯函数）+ `python -m crypto.backtest`
    全史回测 → `data/crypto/backtest_report.md`。全史结果：BTC/ETH 各了结 1 轮
    （2018 建仓 → 2021 10x 止盈，+1427% / +3022%）；2022 熊市轮 3 币均持仓中
    待 10x（止盈线 BTC 20.3万 / ETH 1.45万 / SOL 775）。
  - tests/test_crypto_strategy.py 13 用例（合成路径零网络）；全量 694 过。
  - 边界纪律：回测 = 策略验证工具，不接自动交易。
- **v7.0.46b**：数据湖 tab 聚焦 3 币展示——per_symbol 加 active 标记，主表只列
  active 3 币、退役 17 币折叠展示（数据保留，仅显示层）。
- **v7.0.46**：回测湖依赖自包含化——test_martingale_web 13 例原依赖
  /tmp/lake_bt_20261022.duckdb 外部快照（被 ops 清理 → skip），改为自包含
  合成湖（生产 DDL + 4 股受控 K 线路径），零外部依赖、不随行情漂移。
- **v7.0.45**：加密货币数据湖三标的全量 + 实时 K 线网关（Joel 2026-11-11 正式开工）：
  - coins 聚焦 **BTCUSDT/ETHUSDT/SOLUSDT**（top20 其余 17 币 is_active=FALSE，
    数据保留可恢复）；全量历史走 **Binance 官方月度快照**（`crypto.sources.
    binance_vision`，参考 quant_iTransformer 离线获取：4 线程 + (币,年,月)
    沙盒缓存 + 403/429 退避，纯 stdlib 无新依赖）；BTC/ETH 自 2017-08、
    SOL 自 2020-08 起。
  - `crypto.sync --full` = vision 历史段（每币自动探测首月 → 最后已发布月）
    + REST 当月尾巴（幂等，重复无害）；日常增量仍走整点 cron（:05）。
  - `crypto.gateway/` 实时 K 线网关（systemd crypto-gateway.service）：
    WS 组合流（1 连接 3 标的 kline_1h）+ 120s 看门狗 + 指数退避重连 +
    同 ts K 线去重（替换未完成 K 线）；**纯内存态不入湖**（Joel 定案：
    湖 = 离线数据，网关服务后续实时买点判断）；状态文件
    data/crypto/gateway_state.json 供 web 展示。
  - web：/api/gateway 端点（外层 auth 门覆盖）+ LakeTab 实时网关卡片
    （WS 状态 + 最新价，15s 自刷）；前端 4/4。
  - 测试：tests/test_crypto_data.py 15 用例（湖幂等/vision 解析/网关去重/
    看门狗假死/端点分支，零真实网络）。
- **v7.0.44**：443 单域名 HTTPS 入口（Joel 2026-11-11 放通 443）：
  - nginx 443（Let's Encrypt 证书，acme.sh ALPN-01 签发 + 续期钩子自动腾挪 443
    + 看门狗兜底）按路径分流：`https://stream04l4.duckdns.org/` → 股票 :9090，
    `/crypto/` → 加密货币 :9091（前缀透传不剥，SSE/长连接已配）。
  - crypto 后端统一挂 /crypto 前缀（子应用 mount + **外层 auth 门兜底**——
    修复 Mount 挂载下 auth 中间件不执行导致的未登录直通，探针验证 401/200
    矩阵正确）；前端 vite base '/crypto/' + API 单一入口带前缀。
  - 跨系统跳转改同源路径（/ ↔ /crypto/），cookie 同域共享，双端互免登。
  - 文档：docs/architecture.md 升为项目地图权威（含 443 入口/扩展约定/
    不可动清单）；docs/nginx-stream04l4.conf 为 nginx 配置权威副本。
- **v7.0.43**：加密货币系统框架（Joel 2026-11-11 定案：同仓库双系统，加密货币
  独立页面/进程/服务/数据湖，参照股票四页签，先搭框架慢慢完善）：
  - 新增顶层包 `crypto/`：Binance 主源 + OKX 交叉校验（EU 直连实测 6/6 200）、
    `python -m crypto.sync`（幂等，cron 每小时 :05，--full 7 年回填）、
    独立湖 `data/crypto/crypto.duckdb`（top 20 币，与 A 股湖物理隔离；
    v7.0.43 后期目录整理时由 data/lake/ 迁入 data/crypto/ 独占目录）。
  - 独立 Web：`crypto/web/app.py`（:9091，crypto-web.service）+ 独立 Vue SPA
    （crypto/web/frontend/）——登录页双入口（恢复 v7.0.42 解耦的双系统登录，
    ssc_session cookie 按 hostname 跨端口，任一端登录另一端免登），四页签
    我的持仓/策略/回测/数据湖（框架骨架，数据湖真实展示），顶栏 📉股票 回跳。
  - 股票侧：登录页恢复双按钮（🪙 加密货币 → :9091）、顶栏加 🪙 加密货币
    跳转——仅展示层 3 文件；策略/数学/推送/数据零改动。
  - 验证：跨端口免登双向 200；停 :9091 后 :9090 全功能正常（进程级隔离）；
    股票前端 253/253 + 加密前端 4/4 绿；sync 实测 20 币 1460 行 + 交叉校验
    5/5（最大偏差 0.05%）。详见 docs/crypto_design.md（v0.3）。
- **v7.0.42**：双系统登录解耦（2026-11-11 Joel「把当前本机的加密货币部分完全
  删除掉」，Quant iTransformer 下线）：
  - 登录页移除 ⚡Quant 按钮（回到 A股4维选股单系统入口），顶栏移除 ⚡Quant
    跳转（9091）；LoginView/AppShell/LoginView.test.js 同步，前端 253 绿，
    重建 bundle 后全 JS 零 9091/quant 引用。
  - 本机 quant-gateway(8000)/quant-tower(9091)/quant-live 服务与 unit 文件
    已删除；加密货币研究资产（仓库/joint_project 4 目录）已全删。
  - 纯展示层改动：策略/数学/推送零改动，f6 红线不受影响。
- **v7.0.41**：改策略触发十年回测表重算（2026-11-11 Joel「改策略是应该触发
  重算」）：
  - 背景：G7 十年回测表按「当前生产策略参数」回放，此前仅 19:30 lake-sync 刷新
    ——改策略参数（回撤%/手数等）后 G7 停在旧参数快照直到次日；而门槛值本身
    （20%/3%/价格带）改完本就即时生效，只有回放参数有这一档延迟
  - PUT /api/strategy 与 POST /api/strategy/import 写盘成功后依次调
    `_rescan_selection_after_save`（~2s，102 股×十年回放）→
    `_rescan_timing_after_save`（~1s）；**顺序不可换**：时机表按候选池算，
    候选池 G7 读十年回测表；湖被锁各自降级 refreshed=false（下次 19:30 生效），
    保存本身不受影响
  - 前端保存提示追加「十年回测表已重算（N 只）」/降级说明
  - 验证：新增专测（新值生效 first_drawdown 15→20 读盘 + selection 先于
    timing 顺序断言）+ 湖锁双表降级断言 + web 测试 rescan 安全桩扩展；
    端到端实测：dd 15→16 表即时重算（正收益 91→90）→ 还原 15（91 复原，
    yaml 逐字节一致）
- **v7.0.40**：我的持仓展示价改实时（2026-11-11 Joel「持仓里股票的现价还不是
  实时更新的」）：
  - 根源：`/api/my_positions` 早已取实时行情（Gate 3 腾讯主→push2delay 备），但
    **只用于买卖信号判定**；表格「最新价」/市值/浮动盈亏/汇总仍按湖收盘
    （T+1）算——页面 30s 轮询照转，数字却不动
  - 修复：展示价 = 实时行情优先，缺失按股回退湖价；最新价/市值/盈亏/汇总统一
    按展示价重算（信号判定口径不变，仍 `spot_price` 优先）；状态机参考（档/
    触发价）与「数据截至」（湖日期）不受影响
  - 附带收益：灌数持锁（湖不可用）时价格可来自实时行情照出；交易登记预填价
    = 当前实时价
  - 前端：持仓卡标注「数据截至 X（状态机）· 价源：实时行情 HH:MM:SS / 最新收盘（T+1）」
  - 验证：后端 spot 覆盖专测（双股混合口径：实时 40.0 + 湖价 9.66 → 汇总按股
    结算）+ 湖不可用降级双分支（双缺纯降级 / 行情可用照出）；浏览器实测
    价源「实时行情 20261111154219」、4 持仓全部当日价
- **v7.0.39**：股票池十年回测门槛加收益率百分比（2026-11-10 Joel「十年回测只能
  选择真或否，需要可以设置收益率百分比门槛，默认 20%，过滤掉一些收益不高的
  股票」）：
  - universe 新增 `backtest_min_return_pct`（%，默认 20；**可选键**，老 yaml
    缺省 20.0——v7.0.30 先例；生产 yaml 显式 20）；G7 语义 = n_buys≥1 且
    净收益率 ≥ 门槛（原「必须为正」= 0% 门槛；设 0 恢复原语义）；收益率
    NULL fail-closed；仅在 backtest_positive 开启时生效
  - 消费链零新代码：池构建/策略页/信号/微信推送全部读预计算表 pnl_pct
    （selection_backtest_10y 已有列，无湖 schema 变更、无需重算）；PUT
    /api/strategy schema + 校验 + 策略页字段行（「十年回测收益率下限（%）」，
    「十年回测必须为正」改名「十年回测门槛」）
  - 线上实测（重启后）：G7 漏斗 91→73（过滤 18 只正收益但 <20% 的股票），
    候选池 65→58，成员最低收益率 21.02%
  - 验证：test_selection 门槛专测 3 组（20% 边界≥/门槛 0 恢复原语义/门槛 50
    全剔 + NULL fail-closed）、既有门槛用例 fixture 同步（12%→25% 过门槛）、
    前端 253 绿（21 字段/16 数字输入/PUT 11 键/FIELD_TYPE 24 键同步）
- **v7.0.38**：回测页买入时间表折叠（2026-11-10 Joel「回测报告里这个买入时间表
  太长了，折叠一下」）：
  - SimpleTable 无分页，池回测 200+ 行（实测 102 股 = 1,394 行）整屏铺开 →
    buys 卡改可折叠：默认态按行数（≤30 行展开、>30 行折叠），每次新结果加载
    重置默认态；标题变按钮（▾/▸ + 「N 行」）点击切换；v-show 保表内搜索/
    排序状态；折叠时显示「已折叠 · 点击标题展开（N 行）」
  - 纯展示层，零状态机/数据改动；前端 253 绿（新增折叠 2 用例：长表默认折叠+
    点展开、短表默认展开+折叠/再展开）；线上 102 股历史产物 1,394 行实测
    默认折叠、切换正常
- **v7.0.37**：回测页统一排版（2026-11-10 Joel「把回测页面的字体之类的统一
  排版一下」）：
  - 回测表单区此前 `.bt-form-grid`/`.f-row`/`.radio` 无 CSS 定义 → 浏览器默认
    布局（date 输入原生尺寸、radio 挤行、select 无边框），与其他页签 13px
    控件语言不一致；本次补齐：两列网格、控件 13px/8px 圆角/1px --line 边框、
    标签 13px 定宽列（历史运行 60px 防折行）、代码输入等宽字体、radio
    accent-color 统一
  - 历史运行刷新按钮 class `muted`（仅变色）→ `ghost-btn`（全站幽灵按钮
    语言），文案 🔄 → 🔄 刷新
  - 纯展示层，零状态机/数据改动；前端 251 绿，线上浏览器实测计算样式 +
    截图核对（标签单行、对齐、无溢出）
- **v7.0.36**：分红余额优先出资（2026-11-10 Joel 定案「有分红盈余要优先用
  分红来投入，分红不够了再从外部投入」）：
  - 买入决策零改动（状态机不看出资 → buy 时间表逐位一致，f6 buy 断言 7/7
    过）；仅出资归属变化：每笔 cost 先扣分红余额，缺口计外部净投入；余额
    跨年结转
  - 口径：total_invested=外部净投入 E；final_assets=市值+分红余额；
    total_pnl=净值−E（与旧口径绝对额恒等：M+Σd−C，分红出资在分子分母同时
    消去）；pnl_pct=总盈亏/E（分母变小 → 收益率更高，同绝对收益更少外部钱）；
    dividends=累计收到（口径不变）；min_unreal_pct/unreal_pct 对外部净投入；
    成本轴 c_arr=外部净投入
  - 年度汇总：分红投入列=当年买入中余额出资部分（不再是恒 0）；分红盈余=
    余额净变动（收到−投出，跨年结转）
  - f6 基线更新（6/7 只受影响；sh.600900 无分红零 diff）：total_invested↓
    =Σ分红出资、final_assets↓同额、pnl_pct↑、min_unreal_pct 略深、
    total_pnl 不变（1 例 1e-14 ulp 尾差）
  - 验证：新专测 test_dividend_priority_funding（出资归属/恒等式/恒等恒量/
    state 投影/成本轴一致 7 组断言）；sz.302132 真实回测实样（分红 79,360
    全额转投入、余额清零、总盈亏 +3,320,814 不变、收益率 374%→411%）
- **v7.0.35**：回测 summary.md 个股明细改年度汇总（2026-11-10 Joel 定案「回测
  里已经有买入时间表了，摘要里的个股明细就不需要再列一遍买入时间表了。改成
  个股每年的外部投入、分红再投入（或盈余）以及盈利情况汇总」；同日修订列
  布局：投入拆外部/分红 + 股价盈亏 + 年底总资产）：
  - 明细段（≤20 只时）删逐笔买入时间表（结果区 buys 表/buys.csv 已有），
    改逐年表：年份 | 外部投入（当年买入现金支出含费）| 分红投入（cash 口径
    恒 0，列保留）| 分红盈余（当年现金分红不投入单列）| 股价盈亏（持仓市值
    变化，含新购股份）| 年底总资产（市值+累计分红现金=final_assets 口径）
  - 恒等式（表头注释写明）：年底总资产 = 年初总资产 + 股价盈亏 + 分红盈余
    ——投入已转为市值含在股价盈亏内，不另加（否则重复计算）；
    Σ(股价盈亏+分红盈余) = 期末总资产（telescoping）
  - 数据：engine.run_stock 新增 `cd_arr`（逐 bar 累计分红现金，仅报告用）+
    `stat["annual"]`（`_annual_summary`：建仓前全零年跳过）；状态机数学零
    改动，f6 字段集/per_stock.csv 列不受影响（f6 回归过）。
  - 验证：engine 2 新用例（跨年精确值+恒等式、未建仓空）、report 3 新用例
    （年度表替代/无 annual 键防御/空表跳过）、sz.302132 真实回测实样
    （2010~2026 逐年 17 行，2026 年底 4,207,827 = final_assets 对账一致）。
- **v7.0.34**：回测页历史回测结果读取（2026-11-10 Joel 定案「回测页面最好
  有个历史回测结果读取功能」）：
  - 后端 `GET /api/bt/runs?limit=`（只读，上限 100）：扫 BT_RUNS_DIR 各产物
    目录的 status.json 摘要（窗口/标的数/参数），task_id 时间戳降序；运行中/
    不完整（无 status.json）generated_at=None。
  - 前端回测页新增「📜 历史回测结果」卡：onMounted 拉列表 → 下拉选择
    （时间 · N 股 · 窗口）→ 选中即 `GET /api/bt/results/{id}` 填入同一结果区
    （与 SSE done 同形状复用展示，meta 行加【历史产物】标记）；不碰
    taskStore，新回测完成仍按原链路覆盖。
  - 验证：后端 3 新用例（排序/字段映射/不完整+损坏/空目录+limit）、
    前端 251 绿（新增列表+加载 2 用例；v7.0.29「无策略下拉」守卫限定到
    .bt-form 内）、线上实测 6 次历史回测列出、101 股历史产物 1.5s 加载。
- **v7.0.33**：回测结果个股汇总展示收敛（2026-11-10 Joel 反馈「个股汇总的数值
  保留小数点后位数太多了」）：
  - 引擎产物是全精度 float（如 887013.0240999996 / 3.743816971766769）+
    ISO 时间戳（2026-11-10T00:00:00.000000000），原样进表格。
  - 展示层截断（只改展示，排序/搜索仍走原值）：SimpleTable 列定义支持可选
    第 4 项格式化函数 `[key, label, isNum?, fmt?]`；BacktestTab 金额/价格/
    百分比 → 2 位小数、日期列截 T 后（per_stock + buys 两表）。
  - 验证：前端 249 绿（新增全精度 fixture 用例，实值取自 output 产物）、
    线上单股回测截图实证（887013.02 / 4207827.44 / 3.74 / 2010-09-29）。
- **v7.0.32**：除权日盘中口径修正（2026-11-10 Joel 定案「要修」）：
  - **根因**：除权日盘中湖末 bar 还是事件前 K 线（因子 19:30 CST 灌数后才
    更新），实时原始价已除权而触发价因子未更新 → 触发价比真实除权后口径高
    一个分红幅度；线距现价 < 分红幅度时除权一跌即假触线（假买入/加仓推送，
    当晚湖同步自愈）。日频状态机不受影响（后复权空间，除权缺口被当日 af
    跳变精确吸收）。
  - **修正**：`engine.pending_exrights_factor(con, codes, now)` 单一因子
    r_cum=af_new/af_old（单事件精确 P_prev×(1+stk_div/10)/(P_prev−cash_dps)，
    多事件顺序模拟兜底，无数据→1.0）；`state_rows(now=北京今日)` 触发价÷
    r_cum（附 exrights_rebase 字段）、`live_pctile_map(now=北京今日)` 现价×
    r_cum（除权缺口不再系统性压低分位假「时机成熟」）。接线：web/app.py
    3 处（实时=北京 date、回放=as_of）+ position_alert/signal_touch 脚本。
  - **红线**：f6 回测走 run_stock 直连不经 state_rows；now=None → 零修正
    （回测/回放），f6 回归逐位不变；lake/ 零改动。
  - 验证：tests/test_exrights_live_rebase.py 15 新（tmp-duckdb 全离线）、
    test_position_alert 口径守卫扩展 now 接线断言、后端全量 653 绿、真实湖
    冒烟 200 股今日 0 pending 零影响。
- **v7.0.29**：**策略库退役**（2026-11-09 Joel 定案：「策略里面的策略另存为的功能
  以及加载策略的功能删除掉，后续策略不太会大变了」「回测里的具体策略使用当前策略」）：
  - **另存为/加载/删除/列表全删**：后端 `GET/POST/DELETE /api/strategies`、
    `POST /api/strategies/{name}/load`、`SaveStrategyRequest`、`_sanitize_strategy_name`
    /`_strategy_path`/`STRATEGIES_DIR` 全部移除；前端 StrategyTab 工具栏仅剩
    【编辑/保存/取消】（删另存为弹窗、加载下拉、删除按钮），MyPositionsTab 删
    「策略应用」下拉与 localStorage 快照选择（ss.mypos.strategy 残留静默忽略，
    组件不再读写该键）。
  - **回测/持仓/信号统一当前策略**：`/api/bt/runs` body 不再接受 `strategy`
    字段（恒用 config/strategy.yaml，子进程不带 --config）；`/api/my_positions`
    与 `/api/strategy_signals` 去掉 `?strategy=` 参数，响应固定
    `strategy="current"`。BacktestTab 结果 meta 固定显示「策略 当前策略
    （strategy.yaml）」。
  - **保留**：`POST /api/strategy/import`（裸 yaml 文本导入当前策略，无 UI、
    不涉策略库）与 config.py 老 yaml 兼容回退逻辑不变。
  - **data/strategies/**（gitignore，本地快照）不再被任何代码读写，保留在盘上
    作为 Joel 的历史参数存档。
  - 验证：后端 609 全绿（基线 628，删 test_v621_strategy_library.py 24 用例，
    test_martingale_web/test_strategy_signals 库相关用例改当前策略口径，净 -19）、
    前端 240 绿、f6 回归不受影响（引擎零改动）。
- **v7.0.28**：仓位口径全链统一为**手数 C 方案**（2026-11-09 Joel 拍板：「全部改
  为 c，包括策略页上面的说明等等相关部分都改为 c，按照 a 我没法实际执行」——
  金额 A / 固定一手 B / 递增手数 C 三版十年回测已交付，选 C）：
  - **sizing 切手数**：金额 3 键（initial_amount/base_add_amount/growth_ratio）
    自 config schema / strategy.yaml / Web 校验 / 前端字段元数据全链路删除；
    仓位 = 手数：1 手 = lot_size 股（A 股 100），首仓 = first_lots 手，
    第 k 档（0 起）= first_lots + k×add_lots_step 手（1→2→3→…），再武装后
    k 归零、新轮首买重新从 first_lots 手起算。生产值 lot_size=100 /
    first_lots=1 / add_lots_step=1。
  - **触发时机零改动**：买笔日期 / 档线 / 再武装状态机不变（f6 红线调整为
    「触发时机零改动 + 金额切手数口径」；scratch 已验证 63/63 只池 A/B/C
    三版买入日期序列逐笔一致）。
  - **展示口径**：信号建议量统一 `suggested_lots`（手数）；参考金额 = 手数×100×
    现价（前端派生展示，万单位）；买入登记股数预填 = 建议手数×100；持仓台账 /
    信号中心 / 回测 buys.csv（加 lots 列）/ 微信推送（「建议加仓至第 N 档，X 手」
    「首仓触发，X 手」）全部手数口径。
  - **f6 基线重生成**：7 股买入笔数/日期/价格与 10-26 基线逐笔零 diff，仅金额
    与汇总变化（各股新数字见 tests/fixtures/f6_baseline/README.md）。
  - 验证：后端全量 + 前端 248 绿、f6 回归 14/14、线上重启实测。
- **v7.0.27**：策略编辑模式改动即时生效（2026-11-09 Joel 反馈「编辑模式改动
  数值后好像实际没有生效重新扫描」）：
  - **根因**：选股时机表（selection_timing）是预计算表，历史仅 11:30 UTC
    lake-sync 刷新——编辑模式改 timing 参数（分位上限/窗口/首仓建仓回撤）
    不重算，「今日选股」与信号中心时机门要次日才反映新值（持仓策略/门槛
    参数本就是每请求现算，即时生效）。
  - **修复**：`PUT /api/strategy`、`POST /api/strategy/import`、
    `POST /api/strategies/{name}/load` 写盘成功后同步重算选股时机表
    （`_rescan_timing_after_save`，实测 ~1s）；响应新增 `timing_rescan`
    结果，前端保存提示显示「选股时机已重算（今日选股 N 只）」。
  - **降级**：湖被锁（11:30 同步窗口，DuckDB 写连接立即报锁冲突、不挂起）
    → 保存不受影响，`timing_rescan.refreshed=false`，新值于下次 11:30 同步
    自动生效。
  - **测试卫生**：三个 web 测试模块统一桩掉重算的湖写依赖（测试零写生产湖）。
  - 验证：后端全量 + 前端 248 绿；线上实测「分位上限 40→30」保存 1.3s 内
    今日选股 2→1（湖北能源 39.19%>30% 排除），恢复 40 → 2。
- **v7.0.26**：选股策略梳理（2026-11-09 Joel 定案：「首仓建仓回撤应该放在选股
  策略里，从持仓策略里拿出来；选股策略里没用的都删掉，不要留着观察」）：
  - **首仓建仓回撤归入选股策略**：`first_drawdown_pct`（15%）自 strategy 段
    移入 timing 段（yaml/config schema/Web 校验/前端卡片全链路）；首仓线 =
    本轮高点×(1−该值) 属首仓买入时机，持仓策略段只保留加仓/再武装参数。
    引擎经 `StrategyParams.first_drawdown` 读取——**状态机数学零改动**（f6
    红线不受影响）；策略库旧快照（无 timing 段、dd 在 strategy 段）回退兼容。
  - **② 股息率 / ③ 250 日线彻底删除**（v7.0.24 的参考展示一并移除，不留观察项）：
    config 键（`yield_min_pct`/`below_ma250`/`ma_period`）、湖表列
    （`ttm_yield_pct`/`ma_value`/`below_ma`/`cond_yield`/`cond_below_ma`，
    selection_timing 收缩为 10 列、刷表改 drop+重建）、今日选股表「股息率%（参考）」
    列、信号时机明细「· 股息率 x%（参考）」、微信首仓消息股息率明细——全部
    移除。选股策略参数 = 3 键：分位窗口 / 分位上限 / 首仓建仓回撤。
  - **页面**：策略页字段 23→20（持仓策略 7 + 选股策略 3 + 股票池 10）；
    我的持仓信号模块脚注同步。
  - 验证：后端全量 + 前端 248 绿、f6 红线零 diff、线上 /api/timing +
    策略信号实测。
- **v7.0.25**：微信推送统一为**「进入当前信号才推」**（2026-11-09 Joel
  定案：「微信信号统一改为只有进入当前信号的才推送，不然只有时机到了没有
  意义」）——**时机独立推送通道退役**：
  - 背景：湖北能源（39.2% 分位 / 息率 3.48%）今早触发「时机成熟（首仓待价）」
    推送，但其现价 4.31 距首仓线 4.0967 尚高 5.21% → 页面无信号，推送无
    落地动作，Joel 判无意义。
  - **改动**：`martingale/timing.py` 删 `check_and_push` / `render_timing_message`
    / 边沿状态（`timing_push_state.json` 退役）；`_cmd_timing` 纯预计算
    （删 `--no-push`/`--dry-run`）。微信仅剩「策略点位提醒」两条通道：
    **首仓 = 时机✓ 且 价破首仓线**（position_alert 首仓通道）、**加仓 = 价破
    档线**（position_alert 加仓通道）——即全部微信推送 = 进入当前信号事件。
  - **页面**：今日选股表 = 时机成熟观察名单（参考展示）；徽标「买入区」→
    「时机成熟」；说明行/卡片描述/strategyMeta 同步「微信不为时机单独推送」。
    湖表 `push_eligible` 列保留（= ① 同值，仅页面徽标用）。
  - 验证：后端全量回归绿、前端 248 绿、build 新版、线上 /api/timing 实测。
