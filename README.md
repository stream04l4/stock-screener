# stock-screener · v7.0.54（双系统版：A股 + 加密货币，443 单域名 HTTPS）

A股**深回撤金字塔加仓**策略（`drawdown_martingale`）：离线数据湖驱动。v7 起移除了
多因子选股/日跑体系，仓库只保留一条链路：

```
DuckDB 数据湖（lake/ 包维护，cron 每日同步）
   │  read_only
   ▼
martingale/ 包（5 条件股票池 → 逐股状态机回测 → 产物 CSV/MD）
   │
   ▼
Web（FastAPI :9090，四页签：回测 / 查询 / 策略 / 数据湖）
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

池内**每只股票**独立状态机：

- **信号**：后复权收盘 hfq = raw × adj_factor（隐含除权推导见 engine.py 注释）；**成交**按原始收盘价。
- **高点 H**：初始 = 窗口首日 hfq 收盘。rearm 两模式：
  - `bounce`（默认）：价格 ≥ 本周期低点 L × (1+rearm_pct) → 重记 H = 当前价、重置 k=0。
    每 bar **先**更新 low_since_high **再**判反弹（创新低当日不触发）。
  - `newhigh`：仅创新高刷新 H。
- **仓位口径 = 手数（v7.0.28 C 方案）**：1 手 = `lot_size` 股（A 股固定 100）。
  首仓 = `first_lots` 手（k=0 档）；**加仓**每再跌 `add_step_pct` 一档（档线
  H×(1−first_dd−k×step)，k=1,2,…）穿一档买一档，第 k 档（0 起）买
  `first_lots + k×add_lots_step` 手（1→2→3→… 递增），上不封顶；跳空一日穿多档。
  再武装（rearm）后 k 归零，新轮首买重新从 `first_lots` 手起算。
- **不卖出、不封顶、无熔断**（无 exit/stop/max_position 参数）。
- **分红**（cash 口径固定）：除息日前一日收盘持仓 × cash_dps 收现金，当日买入前执行；
  送转按比例加股；停牌期除权事件顺延复牌首个 bar。
- **成本**（引擎常量，不进配置、不进 Web）：佣金 3bps（买）；印花税 5bps（无卖出，no-op）。
- **组合聚合**：gdates 全局轴 + 末值向前结转（停牌持仓不消失）。
- 已知局限：湖内无退市股（幸存者偏差）→ 回测产物带此声明。

## 2. 股票池 = 候选池（v7.0.12，基础 5 条件 + 选股门槛 ∪ 手动添加）

**候选池 ≠ 买入信号**：入选只代表进入策略候选名单；是否买入看价格是否到策略触发线
（Web「我的持仓」页信号 / 微信 PushPlus 提醒，同一口径，`martingale/signals.py`）。

基础 5 条件（`build_pool`）：`kline_daily 5556 只 → 行业白名单(csric2) → SOE(国资前十大股东)
→ 总市值≥200亿 → 上市≥250 bars → 连续分红N年`。
选股门槛（v7.0.12，Joel 2026-10-24 拍板；波动率门槛经讨论不加）：
⑦ **十年滚动回测门槛**（≥1 次触发且净收益率 ≥ backtest_min_return_pct%，默认 20
——v7.0.39 Joel 定案，原「必须为正」= 0% 门槛；含分红扣佣金；预计算表
`selection_backtest_10y`，fail-closed：缺行/收益率为 NULL 不入选）→
⑧ **TTM 股息率≥3%** → ⑨ **价格 3~100 元**。
**手动添加**：`data/manual_pool.json`（策略页签股票池卡片内的手动录入条 /
`GET/POST/DELETE /api/pool/manual`）绕过 ⑦⑧⑨ 恒在候选池；退市自动剔除；
与自动入选重叠时保留自动版并标记 `is_manual`。
生产首跑（2026-10-24，as_of 10-23）：98 → 82(⑦) → 60(⑧) → **59 只**。

SOE 双规则：holders_snapshot 最新 as_of 的 holder_rank≤10 中，share_nature='国有股'
或名称含 soe_keywords 任一（国务院/国资委/汇金/财政部/国资）。

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
.venv/bin/python -m martingale status --out /tmp/bt_out   # 读产物：逐股当前状态
```

- 窗口留空 = 各股数据起点 → 数据终点。
- `selection` = 基础池逐股十年滚动回测 → 整表刷新湖表 `selection_backtest_10y`
  （98 只 ~2s，纯湖内计算零网络零配额）；每日作业在湖增量后自动追加执行，
  失败不阻断（Web 候选池 ⑦ 读旧快照 fail-closed，次日重算）。
- `timing` = 候选池逐股 ①分位（v7.0.26 起唯一时机条件）→ 整表刷新湖表 `selection_timing`
  （v7.0.25 起纯预计算不推送——微信统一为进入当前信号才推，策略点位提醒）。
- 进度标记 `[PROGRESS] stage=bt done=k total=N`（Web SSE 分类器消费）。
- 产物：`<out>/{per_stock.csv, buys.csv, equity_curve.csv, summary.md, status.json}`。

## 4. 参数（config/strategy.yaml）

| 段 | 键 | 说明 |
|---|---|---|
| strategy | name | 固定 `drawdown_martingale` |
| | lot_size | 每手股数（A 股固定 100） |
| | first_lots | 首仓手数（k=0 档；再武装后新轮首买同此值） |
| | add_lots_step | 加仓手数增量（第 k 档 = first_lots + k×add_lots_step 手） |
| | add_step_pct | 每档加仓步长（% → 小数） |
| | rearm_mode | `bounce` \| `newhigh` |
| | rearm_pct | bounce 反弹重记高点阈值（% → 小数） |
| | accounting | 固定 `cash`（只读，Web 不可改） |
| universe | industry_whitelist_csric2 | 行业白名单（3 位 csric2 码） |
| | soe_keywords | 国资股东名称关键词（SOE 恒强制，无开关） |
| | min_total_mv_yi | 总市值门槛（亿） |
| | min_consecutive_div_years | 连续分红年数 |
| | listing_min_trading_days | 最少上市 bar 数 |
| | backtest_positive | ⑦ 十年回测门槛开关（v7.0.12） |
| | backtest_window_years | ⑦ 回测窗口年数（滚动） |
| | backtest_min_return_pct | ⑦ 十年回测净收益率下限（%，默认 20；v7.0.39，可选键） |
| | min_ttm_yield_pct | ⑧ TTM 股息率下限（%） |
| | price_min_yuan / price_max_yuan | ⑨ 最新价区间（元） |
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

## 6. 定时任务

crontab 仅剩一条业务行：**lake-sync**（`30 11 * * 1-5 scripts/run_lake_sync_daily.sh`，
数据湖每日同步，martingale 的一切查询都依赖它）。v6 的 `35 9 * * 1-5 run_cron.sh`
（多因子日跑）已删除。

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
.venv/bin/python -m pytest tests/ -q          # 后端（591 用例，含 test_timing 边沿语义 + test_web_auth 登录门禁）
cd web/frontend && npm test                   # 前端 vitest（239 用例，含登录页/门禁/买入登记）
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
martingale/        # v7 策略包：config/data/universe/engine/report/__main__(CLI)
lake/              # 数据湖（v6 保留，零改动红线）
screener/          # 退化为数据客户端库：config + data/{baostock_client,tencent,sina,rf}
web/               # FastAPI app.py + frontend/（Vue3 + vitest；dist/ 构建产物）
config/strategy.yaml   # strategy(9键)/universe(10键)/timing(5键,可缺省)/lake/datasource
scripts/           # lake 运维：lake_backfill / run_lake_sync_daily.sh / lake_smoke / …
data/lake/         # lake.duckdb（gitignore）
tests/             # pytest（含 fixtures/f6_baseline/）
output/            # 回测产物（gitignore；旧多因子产物保留未清）
```

## 9. v7 变更摘要（相对 v6.3.6，git 8c49f71）

- 删：多因子选股/打分/徽章/回测引擎（screener 主体、backtest/、run_cron.sh、
  5 个多因子 scripts）、Web「运行」页签与 /api/runs、/api/backtest 族、45 个测试。
- 增：`martingale/` 包（状态机自 scripts/backtest_martingale.py 逐行移植，f6 七股
  逐位对齐后删参考脚本）、`/api/bt/runs` 族（SSE）、前端回测/查询页签。
- 保留：`lake/` 包（`git diff 8c49f71 -- lake/` 为空）、lake-sync cron、数据湖页签。
- 旧多因子 output/ 产物保留未清（清理交 Joel 定夺）。

## 10. 近期版本（v7.0.11 → v7.0.27）

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
