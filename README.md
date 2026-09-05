# A股选股策略程序（stock-screener）

可配置、可重复运行的 A 股四维选股：**技术面 + 股息率 + 行业内排名 + 基本面**，
输出 `result_YYYYMMDD.csv` + 人类可读报告 `report_YYYYMMDD.md`。

主数据源 **BaoStock**（历史日K / 分红 / 季度基本面 / 行业分类 / 全市场股票列表），
腾讯 `qt.gtimg.cn` 实时接口**仅做交叉验证**，不进主计算路径。

## 1. 环境安装

```bash
cd ~/stock-screener
# 独立 venv（uv；系统 pip 太老勿依赖，不要碰 /tmp/ashare-venv）
~/.hermes/bin/uv sync --all-extras        # 创建 .venv 并装依赖
source .venv/bin/activate
```

Python 3.10；依赖：baostock、pandas≥2.0、PyYAML、requests（dev: pytest）。

### baostock + pandas≥2.0 兼容说明（二选一，本项目选 A）

- **A（本项目采用）**：统一手动 `while rs.next(): rows.append(rs.get_row_data())`
  翻页（见 `screener/data/baostock_client.py`）。原因：baostock 的
  `get_data()` 在结果 >2000 行翻页时内部调用 `DataFrame.append()`，pandas 2.x
  已移除该方法 → `AttributeError`。手动翻页与 pandas 版本无关。
- B（未采用）：把 pandas 钉在 <2.0。

## 2. 运行

```bash
cd ~/stock-screener
python -m screener --date YYYY-MM-DD --config config/strategy.yaml
# 可选参数：
#   --output-dir DIR     输出目录（默认 ./output）
#   --no-crosscheck      跳过腾讯交叉验证
```

- `--date` 非交易日 → 自动回退到最近一个交易日（报告注明）。
- `--date` **必须 ≤ 当前日期**：程序会拒绝未来日期并报错退出（exit 1，
  "请求日期晚于当前日期，拒绝运行"）——未来交易日的空数据不得被当作历史缓存。
- 退出码：0 成功；1 运行失败（含未来日期被拒绝）；2 参数错误。
- 输出：`output/result_YYYYMMDD.csv`、`output/report_YYYYMMDD.md`、
  日志 `logs/run_YYYYMMDD.log`。

**首次运行耗时**：全市场 ~5200 只股票的日K/分红/季报为逐只顺序请求
（BaoStock 单 socket 串行接口，实测并发登录会挂起），约 **1.5~2 小时**。
之后**同一天重复运行全部命中本地缓存，秒级完成、零 BaoStock 查询**
（行业分类快照 TTL 24h，交易日历 TTL 1h）。

## 3. 配置（config/strategy.yaml）

**所有筛选阈值都在 strategy.yaml，代码不硬编码任何阈值**
（`tests/test_no_hardcoded_thresholds.py` 做静态检查兜底）。

| 配置项 | 默认 | 说明 |
|---|---|---|
| `technical.ma_period` | 200 | 收盘价 > MA{n}（后复权日K） |
| `technical.return_window_days` | 250 | 区间收益率回看窗口（交易日） |
| `technical.min_return_pct` / `max_return_pct` | 0 / 100 | 近250日收益 ∈ [0%, 100%]（含边界） |
| `technical.max_annual_volatility_pct` | 45 | 年化波动率 < 45% |
| `dividend.window_days` | 365 | 股息率窗口 [运行日-365天, 运行日] |
| `dividend.min_yield_pct` | 3 | 股息率 ≥ 3% |
| `industry.top_pct` | 30 | 行业内 ROE 前 30% |
| `industry.min_group_size` | 5 | 组不足 N 只 → 跳过排名约束并报告注明 |
| `fundamental.roe_min_pct` | 10 | ROE ≥ 10%（最近披露报告期，累计口径未年化） |
| `fundamental.net_profit_yoy_field` | YOYPNI | 净利同比字段：YOYPNI(归母)/YOYNI(净利润)，须 >0 |
| `fundamental.liability_max_pct` | 60 | 资产负债率 ≤ 60% |
| `fundamental.gross_margin_min_pct` | 20 | 毛利率 > 20%（金融业该字段为空 → 落缺失名单） |
| `fundamental.probe_quarters_back` | 3 | "最近披露报告期"最多回退探测季数 |
| `universe.a_share_prefixes` | sh.60/68, sz.00/30 | A股前缀过滤（剔除指数/ETF/B股） |
| `universe.listing_min_trading_days` | 250 | 上市满 N 个交易日 |
| `data.kline_calendar_days_back` | 420 | 后复权窗口回溯日历天 |
| `data.retry_max_attempts` | 5 | BaoStock 失败重试次数（指数退避+抖动） |

## 4. 筛选逻辑与数据口径（实现要点）

1. **股票池**：`query_all_stock(day=...)` 始终显式传 day（非交易日不传会返回空；
   CLI 先用 `query_trade_dates` 定位最近交易日）→ 前缀过滤 A股 → tradeStatus=1。
2. **ST 剔除以日K `isST=1` 为准**（all_stock 无 isST 列；名称含 ST 仅作辅助标记）。
3. **技术面**：后复权日K `adjustflag="1"`（**1=后复权, 2=前复权, 3=不复权**——
   原任务书 §2 标注有误，以调研报告 §6.1 官方+实测口径为准；adjustflag 必须传字符串）。
4. **当前价/股息率分母**：BaoStock 不复权 close（af=3），与历史K同源、可缓存。
5. **分红**：`query_dividend_data(code, year, yearType="operate")` **逐年循环**
   （窗口涉及的 1~2 个自然年；不传 year 会静默只返回最近 1 条）→
   筛选 `window_start <= dividOperateDate <= 运行日`（同时实现"已实施"+窗口内）→
   求和前 `drop_duplicates(subset=['code','dividOperateDate'])`（同一除权日有
   预案+正式两行，不去重股息率翻倍）→ `dividCashPsBeforeTax` 已是**元/股**，
   不再除 10 → 无分红记录 = 该维度不通过（不是报错）。
6. **基本面**：字段全是**小数**（roeAvg=0.10 即 10%），阈值用小数比较、展示 ×100；
   缺失值返回空串 `''`（判缺失用 `== ''`，代码里统一转 None）；
   "最近披露报告期"从当前季度往前逐季探测（基准股 sh.601398，最多回退 N 季），
   **不硬编码**（2026-09 应为 2026Q2）。ROE 用 `roeAvg` 原值、不做年化。
7. **行业**：`query_stock_industry` 全量（证监会口径，含退市股）按 code left-join；
   industry 为空 → 单列"无行业"不崩溃；组内按 ROE 百分位保留前 30%；
   不足 5 只的组跳过排名约束并在报告注明。
8. **输出**：CSV（代码/名称/行业/收盘价/股息率/ROE/行业百分位/各维度是否通过+复核指标）；
   报告（每层漏斗、最终入选列表、数据时间戳与来源、缺失/异常名单）。

## 5. 本地缓存（同一天重复运行不重复拉取）

- 目录 `cache/`，CSV 原子写（.tmp + os.replace），首行哨兵标记防损坏文件误读。
- 键 = (查询类型, 参数)：个股日K/分红/季报不可变 → **永不过期**；
  行业分类每周一更新 → TTL 24h；交易日历 → TTL 1h。
- 全市场股票列表 `all_stock`：非空结果不可变 → 永不过期；**空结果不落盘**
  （空列表可能只是"数据尚未产生"，永久缓存会污染该日真实运行）；遗留的空
  缓存文件读取时视为 miss 重新拉取。
- 验证方式：两次运行对比 `logs/run_*.log` 里的 "BaoStock请求 N 次"——
  第二次应显著下降（只剩 TTL 过期项）。

## 6. 单元测试（离线，不联网）

```bash
source .venv/bin/activate
python -m pytest -q          # 65 passed
```

覆盖：技术面指标（MA/收益区间边界/波动率公式/数据不足）、股息率
（真实样例 601398：重复除权日去重、窗口边界、无分红不报错、未实施不计入）、
基本面（小数口径、边界含/不含、字段可切换、季报未披露不崩溃）、行业排名
（前30%、小组跳过、空行业不崩溃、ROE缺失排最后）、股票池（真实 7376 行样例：
5215 A股 / 5207 正常交易）、缓存（命中/过期/损坏文件/原子写）、
腾讯 GBK 转码与解析、配置校验、**无硬编码阈值静态检查**。

## 7. cron 部署说明

服务器时区 **UTC**；A股收盘后日K约北京时间 17:30（UTC 09:30）起更新，
T+1 必可取。建议每个交易日 **UTC 09:35**（北京 17:35）起跑：

```cron
# /etc/cron.d/stock-screener （或 crontab -e）
# 周一~周五 UTC 09:35；程序内部用 query_trade_dates 判断是否交易日，
# 节假日自动跳过（非交易日请求会回退到最近交易日，建议配合下方守卫）
35 9 * * 1-5 cd /home/ubuntu/stock-screener && \
  .venv/bin/python -m screener --date $(date -u +%F) \
    --config config/strategy.yaml >> logs/cron.log 2>&1
```

更稳的写法（节假日守卫，避免回退重算旧数据）：

```bash
# /home/ubuntu/stock-screener/run_cron.sh
#!/usr/bin/env bash
cd /home/ubuntu/stock-screener || exit 1
TODAY=$(date -u +%F)
# 用交易日历判断今天是否交易日（BaoStock query_trade_dates，走缓存）
.venv/bin/python - <<'EOF' >/dev/null 2>&1 || { echo "$TODAY not a trading day, skip"; exit 0; }
from datetime import date
import baostock as bs
bs.login()
rs = bs.query_trade_dates(start_date="$TODAY", end_date="$TODAY")
row = None
while rs.error_code == "0" and rs.next():
    row = rs.get_row_data()
bs.logout()
raise SystemExit(0 if row and row[1] == "1" else 1)
EOF
.venv/bin/python -m screener --date "$TODAY" --config config/strategy.yaml
```

```cron
35 9 * * 1-5 /home/ubuntu/stock-screener/run_cron.sh >> /home/ubuntu/stock-screener/logs/cron.log 2>&1
```

注意：首次（某运行日第一次）拉取全市场约 1.5~2 小时，cron 无超时限制即可；
重复运行秒级。`--date` 必须 ≤ 当前日期（程序会拒绝未来日期并报错退出），
误用未来日期不会污染该交易日的真实运行。

## 8. Web 前端（控制台）

在 `web/` 下提供 FastAPI 后端 + 无构建静态单页应用，用于查看运行结果、编辑策略阈值、触发新筛选。复用现有 `.venv`（已加装 fastapi / uvicorn），不引入数据库——所有数据读自 `output/`、`config/`、`logs/` 文件系统。

### 启动（systemd user service）

```bash
# unit 已写入 ~/.config/systemd/user/stock-screener-web.service
systemctl --user daemon-reload
systemctl --user enable --now stock-screener-web.service   # enabled + started
systemctl --user status stock-screener-web.service
# 绑定 0.0.0.0:3080（访问控制由云安全组负责，应用层不加认证）
```

手动前台启动（调试用）：

```bash
cd /home/ubuntu/stock-screener
.venv/bin/python -m uvicorn web.app:app --host 0.0.0.0 --port 3080
```

打开 `http://<host>:3080/`，左侧/顶部三个页签：**结果**（运行列表→漏斗条形图→入选表→技术面幸存者全量表，可搜索/排序/分页）、**策略**（分组卡片展示各维度阈值+说明，编辑模式改完提交 PUT）、**运行**（选日期触发筛选，轮询实时日志尾部，完成后自动回结果页）。

### API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/runs` | 扫描 `output/` 列出已有运行（日期/入选数/候选数/生成时间），倒序 |
| GET | `/api/runs/{YYYYMMDD}` | 该次运行详情：漏斗各层数量、最终入选列表、全部技术面幸存者 CSV(JSON)、缺失名单、跳过排名的行业组、报告 markdown 原文 |
| GET | `/api/strategy` | 当前 `config/strategy.yaml` 解析后的 JSON + 原始文本 |
| PUT | `/api/strategy` | 更新阈值：逐项校验类型与范围（如 `min_yield_pct∈[0,50]`、`ma_period∈[20,500]` 整数），非法值 400；合法则先备份 `.bak` 再原子写回（纯 YAML，所有 key 齐全） |
| POST | `/api/runs` | body `{date}`（可选，默认今天）。后台**子进程**执行 `python -m screener --date D --config config/strategy.yaml`，立即返回 task_id；同一时间只允许一个任务（已有则 409） |
| GET | `/api/runs/{task_id}/status` | running/done/failed + 日志尾部（最后 ~20 行）+ 完成后指向结果 |

### 说明与约束

- **只读复用** `screener/` 主代码：Web 不改动筛选逻辑，仅调用其 CLI 子进程与读取产物文件。
- 触发运行用**子进程**而非 uvicorn 线程内跑（首跑可能 ~2h，不能阻塞 API）；子进程日志写 `logs/web_run_<task_id>.log`。
- PUT 写回前备份到 `config/strategy.yaml.bak`（保留最近一份）。注释会被简化为纯 YAML，但所有 key 齐全且经 `screener.config.load_config` 复校验。
- 前端不硬编码业务阈值：策略字段一律以 `/api/strategy` 返回为准渲染。

## 9. 目录结构

```
stock-screener/
├── pyproject.toml            # uv 项目定义
├── config/strategy.yaml      # 全部筛选阈值（唯一配置源）
├── screener/
│   ├── __main__.py           # CLI 入口 (python -m screener)
│   ├── config.py             # 配置加载+校验（百分数→小数换算）
│   ├── universe.py           # 股票池（前缀过滤/停牌剔除/ST辅助标记）
│   ├── metrics.py            # 指标计算纯函数（技术面/股息率/基本面/行业排名）
│   ├── screener.py           # 筛选引擎（五层漏斗编排）
│   ├── report.py             # CSV + Markdown 报告输出
│   └── data/
│       ├── baostock_client.py  # 登录态/指数退避重试/手动翻页（pandas≥2.0 兼容）
│       ├── cache.py            # 本地 CSV 缓存（原子写+哨兵+TTL）
│       ├── fetchers.py         # 各接口抓取（缓存优先）
│       └── tencent.py          # 腾讯实时快照（GBK 转码封装，仅交叉验证）
├── tests/                    # 74 个离线单测 + fixtures（真实样例数据）
├── web/                      # Web 前端（FastAPI 后端 + 静态 SPA，端口 3080）
│   ├── app.py                # FastAPI 应用（API + 子进程任务管理 + 策略校验）
│   └── static/               # index.html / style.css / app.js（vanilla JS，无构建）
├── cache/                    # 原始数据缓存（自动生成）
├── output/                   # result_*.csv / report_*.md（自动生成）
└── logs/                     # run_*.log / web_run_*.log / cron.log
```

## 10. 已知限制

- BaoStock 单 socket 串行：全市场首拉慢（~1.5-2h），无法安全并发（实测多进程
  并发登录会挂起）；缓存命中后无此问题。
- yfinance 仅作可选抽样交叉验证源，未接入主流程（避免 Yahoo 限频风险）。
- 行业分类为证监会口径（84 个行业），未引入申万依赖。
