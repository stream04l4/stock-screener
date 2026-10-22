# stock-screener · v7.0.0（drawdown_martingale 单策略版）

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

## 2. 股票池（universe，5 条件全链，全离线单库）

`kline_daily 5556 只 → 行业白名单(csric2) → SOE(国资前十大股东) → 总市值≥200亿
→ 上市≥250 bars → 连续分红N年 = 池`（当前 101 只：86 老股 + 15 新股；
新股规则按 brief as-is 移植，与研究口径 94/95 的差异全部来自新股分红语义）。

SOE 双规则：holders_snapshot 最新 as_of 的 holder_rank≤10 中，share_nature='国有股'
或名称含 soe_keywords 任一（国务院/国资委/汇金/财政部/国资）。

## 3. CLI

```bash
.venv/bin/python -m martingale pool                       # 打印池 + 漏斗
.venv/bin/python -m martingale backtest --start 2016-01-04 --end 2026-10-21 \
    --out /tmp/bt_out                                     # 全池回测
.venv/bin/python -m martingale backtest --codes sh.601088,sz.000651 --out /tmp/bt2
.venv/bin/python -m martingale status --out /tmp/bt_out   # 读产物：逐股当前状态
```

- 窗口留空 = 各股数据起点 → 数据终点。
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

四页签：**回测**（参数表单 + SSE 进度 + 结果区：per_stock 表/净值曲线/buys/summary）·
**查询**（/api/positions 逐股状态 + /api/pool 池；行点击弹「状态+历史」）·
**策略**（参数编辑/校验/策略库快照）· **数据湖**（v6 原样）。

主要 API：`/api/health` · `/api/pool` · `/api/strategy`（GET/PUT，PUT 语义校验 + .bak 备份）·
`/api/positions`（同步全池）· `/api/bt/runs`（POST 触发 / GET 列表 / `{id}` status /
`{id}/events` SSE / `{id}/cancel`）· `/api/bt/results/{id}` · `/api/lake/*` · 策略库 CRUD。
回测 = 异步子进程（单任务 409 + pidfile 锁），进度 SSE（15s 心跳，断线 3 次降级 3s 轮询）。

## 6. 定时任务

crontab 仅剩一条业务行：**lake-sync**（`30 11 * * 1-5 scripts/run_lake_sync_daily.sh`，
数据湖每日同步，martingale 的一切查询都依赖它）。v6 的 `35 9 * * 1-5 run_cron.sh`
（多因子日跑）已删除。

**v7.0.8 增量提速**：T2 K线/T3 估值快照改 tushare 全市场单日批量
（`pro.daily`/`pro.adj_factor`/`pro.daily_basic` 各 1 次/分钟限频，~2.5min），
取代逐只 sina+腾讯（~9-10h）；多日缺口股（停牌）仍逐只多源 fallback；
批量失败/行数异常自动回退逐只（`stats["batch"]` 记痕）。
**配额纪律（免费 120 分实测）**：`adj_factor`/`daily_basic` 各 **5 次/天**，
超限进 1 次/小时惩罚态（重置口径未确认）——每日批量各用 1 次，
**手动调试/回测勿再调这两个 trade_date 端点**；`daily` 配额宽裕。

## 7. 测试

```bash
.venv/bin/python -m pytest tests/ -q          # 后端：纯 lake 套件 + martingale 套件 + web + 边界守卫
cd web/frontend && npm test                   # 前端 vitest（208 用例）
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
config/strategy.yaml   # strategy(9键)/universe(5键)/lake/datasource
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
