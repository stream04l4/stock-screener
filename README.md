# stock-screener · v7.0.14（drawdown_martingale 单策略版）

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

## 1. 策略（规格 = brief §3 + 参考实现逐行移植）

池内**每只股票**独立状态机：

- **信号**：后复权收盘 hfq = raw × adj_factor（隐含除权推导见 engine.py 注释）；**成交**按原始收盘价。
- **高点 H**：初始 = 窗口首日 hfq 收盘。rearm 两模式：
  - `bounce`（默认）：价格 ≥ 本周期低点 L × (1+rearm_pct) → 重记 H = 当前价、重置 k=0。
    每 bar **先**更新 low_since_high **再**判反弹（创新低当日不触发）。
  - `newhigh`：仅创新高刷新 H。
- **首仓**：hfq ≤ H×(1−first_drawdown_pct) → 买 `initial_amount`。
  **每股一次性、全策略仅第一次触发使用**，占用 k=0 档；此后所有轮次首买用 `base_add_amount`。
- **加仓**：每再跌 `add_step_pct` 一档（档线 H×(1−first_dd−k×step)，k=1,2,…）穿一档买一档，
  金额 `base_add_amount × growth_ratio^k`，上不封顶；跳空一日穿多档。
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
⑦ **十年滚动回测为正**（≥1 次触发且净利>0，含分红扣佣金；预计算表 `selection_backtest_10y`，
fail-closed：缺行不入选）→ ⑧ **TTM 股息率≥3%** → ⑨ **价格 3~100 元**。
**手动添加**：`data/manual_pool.json`（策略页签股票池卡片内的手动录入条 /
`GET/POST/DELETE /api/pool/manual`）绕过 ⑦⑧⑨ 恒在候选池；退市自动剔除；
与自动入选重叠时保留自动版并标记 `is_manual`。
生产首跑（2026-10-24，as_of 10-23）：98 → 82(⑦) → 60(⑧) → **59 只**。

SOE 双规则：holders_snapshot 最新 as_of 的 holder_rank≤10 中，share_nature='国有股'
或名称含 soe_keywords 任一（国务院/国资委/汇金/财政部/国资）。

## 2.5 选股时机（v7.0.13，候选池之上筛「何时买」）

候选池解决「买什么」，时机条件解决「何时买」（Joel 2026-10-24 拍板，5 点全按推荐口径）：

- **① 价格低位**：当前价处近 N 年（默认 5）qfq 收盘**分位 ≤ price_pctile_max**（默认 40%）；
- **② 高股息**：TTM 股息率 ≥ yield_min_pct（默认 4.5%）；
- **③ 年线之下**（可选，默认关）：当前价 < MA(ma_period)（默认 250）。

**双口径**（`martingale/timing.py` 单一定义点）：
- **观察口径**（页面「今日选股」表）= 已启用条件 **OR**（`in_watchlist`）；
- **提醒口径**（微信 PushPlus）= **①且②**（③启用时叠加，`push_eligible`），
  且**仅新触发**：边沿状态 `data/alerts/timing_push_state.json`（上轮 eligible 集合；
  掉出买区清除 → 再进入才重推；持续在买区不重推；**推送失败不落状态 → 下轮重试**）。
- PE 估值分位经实测不加（池内 0 只低于自身 5y 20% 分位，无区分度）。

**流程**：每日 lake-sync 后 `python -m martingale timing`（~1s 纯湖内计算，
零网络零配额）→ 整表刷新湖表 `selection_timing`（PK ts_code）→ Web O(1) 读；
失败不阻断（页面 fail-closed 空表 / 读旧快照，次日重算重推）。
生产首跑（as_of 10-23）：59 候选 → 观察 18 → 提醒 1（大秦铁路：
5 年分位 21.9% / 回撤 27.5% / 股息 4.59%）。

**闭环**：今日选股表「加持仓」→ 行内填股数/成本（PUT /api/my_positions）→
「我的持仓」账本 → 持仓策略接管档位（现有信号/提醒逻辑零改动）。

## 3. CLI

```bash
.venv/bin/python -m martingale pool                       # 打印候选池 + 全漏斗
.venv/bin/python -m martingale selection                  # 候选池选股预计算（湖表刷新）
.venv/bin/python -m martingale timing [--no-push] [--dry-run]  # 选股时机预计算 + 新触发微信提醒
.venv/bin/python -m martingale backtest --start 2016-01-04 --end 2026-10-21 \
    --out /tmp/bt_out                                     # 全池回测
.venv/bin/python -m martingale backtest --codes sh.601088,sz.000651 --out /tmp/bt2
.venv/bin/python -m martingale status --out /tmp/bt_out   # 读产物：逐股当前状态
```

- 窗口留空 = 各股数据起点 → 数据终点。
- `selection` = 基础池逐股十年滚动回测 → 整表刷新湖表 `selection_backtest_10y`
  （98 只 ~2s，纯湖内计算零网络零配额）；每日作业在湖增量后自动追加执行，
  失败不阻断（Web 候选池 ⑦ 读旧快照 fail-closed，次日重算）。
- `timing` = 候选池逐股 ①分位 ②股息率 ③年线 → 整表刷新湖表 `selection_timing`
  + 提醒口径新触发微信推送（边沿状态防重推）；`--no-push` 只算不推，
  `--dry-run` 打印不落状态不推送。
- 进度标记 `[PROGRESS] stage=bt done=k total=N`（Web SSE 分类器消费）。
- 产物：`<out>/{per_stock.csv, buys.csv, equity_curve.csv, summary.md, status.json}`。

## 4. 参数（config/strategy.yaml）

| 段 | 键 | 说明 |
|---|---|---|
| strategy | name | 固定 `drawdown_martingale` |
| | initial_amount | 首仓金额（每股一次性，k=0 档） |
| | base_add_amount | 基础加仓金额 |
| | first_drawdown_pct | 首档回撤阈值（% → 小数） |
| | add_step_pct | 每档加仓步长（% → 小数） |
| | growth_ratio | 加仓金额增长比（第 k 档 = base × ratio^k） |
| | rearm_mode | `bounce` \| `newhigh` |
| | rearm_pct | bounce 反弹重记高点阈值（% → 小数） |
| | accounting | 固定 `cash`（只读，Web 不可改） |
| universe | industry_whitelist_csric2 | 行业白名单（3 位 csric2 码） |
| | soe_keywords | 国资股东名称关键词（SOE 恒强制，无开关） |
| | min_total_mv_yi | 总市值门槛（亿） |
| | min_consecutive_div_years | 连续分红年数 |
| | listing_min_trading_days | 最少上市 bar 数 |
| | backtest_positive | ⑦ 十年回测为正开关（v7.0.12） |
| | backtest_window_years | ⑦ 回测窗口年数（滚动） |
| | min_ttm_yield_pct | ⑧ TTM 股息率下限（%） |
| | price_min_yuan / price_max_yuan | ⑨ 最新价区间（元） |
| timing（**段可缺省**，缺省=默认值） | price_window_years | ① 价格分位窗口（年，默认 5） |
| | price_pctile_max | ① 分位上限（%，默认 40） |
| | yield_min_pct | ② TTM 股息率下限（%，默认 4.5） |
| | below_ma250 | ③ 年线之下开关（默认 false） |
| | ma_period | ③ 年线周期（交易日，默认 250） |
| lake / datasource.* | — | lake-sync / 补数专用，Web 只读不渲染 |

**首轮 k=0 占档说明（必附注）**：首仓占用 k=0 档（金额 initial_amount），
因此**首轮的第一个实际加仓 = base_add_amount × growth_ratio^1**。
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
可搜索排序）+ 手动添加录入条）/ **选股策略**（v7.0.13：时机 3 条件开关/参数 +
**今日选股表**（观察口径 OR 命中，「买入区」徽标 = 提醒口径，每行一键加持仓））；
参数编辑/校验/策略库快照）· **回测**（参数表单 + SSE 进度 + 结果区）·
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
`{id}/events` SSE / `{id}/cancel`）· `/api/bt/results/{id}` · `/api/lake/*` · 策略库 CRUD。
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
cd web/frontend && npm test                   # 前端 vitest（235 用例，含登录页/门禁）
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

## 10. 近期版本（v7.0.11 → v7.0.13）

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
