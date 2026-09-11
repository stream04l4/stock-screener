# A股选股策略程序（stock-screener）

面向 **"攒股养老"** 场景的 A 股多因子选股器：以**高分红央国企**为核心，用
**技术面 + 股息可持续性 + 行业内排名 + 基本面质量**四维截面 Z-Score 打分，
输出 Top-N 榜单 `result_YYYYMMDD.csv` + 人类可读报告 `report_YYYYMMDD.md`。

投资目标：20 年陆续攒够养老股底仓，靠 4%~5% 分红率产生被动现金流。三条选股原则：
① 分红率高且**可持续**；② 央国企、国计民生行业（长期稳定）；③ 当前处于较低估值区间（不一买就套十年）。

## 版本演进

| 版本 | 主题 | 关键变更 |
|---|---|---|
| v1 | 骨架 | BaoStock 单源 + 四维 AND 硬过滤 + CLI |
| v2 | 打分 + Web | Z-Score 截面多因子打分（legacy 可回退）+ SSE Dashboard |
| v3 | 回测 | 多因子策略历史回测引擎（月度/季度调仓、真实成本模型、PIT 安全） |
| v4 | 数据源架构 | 高频日K/快照切**腾讯批量接口**，BaoStock 降为低频；除权检测 preclose 信号 |
| **v5** | **策略重构** | **"攒股养老"因子体系**：分红五因子、央国企过滤、行业白名单、估值分位因子、再投资参考价输出（Phase 1） |
| **v5.1** | **评审精加工** | payout 软约束区间 / reinvest 多期平滑参考价 + DPS CAGR 列 / 边界测试补全 |

当前默认配置 = v5（`config/strategy.yaml`）。v4 原配置原样保存于 `config/strategy_v4.yaml`
作零回归基线，任何时刻可回退对照。

## 1. 环境安装

```bash
cd ~/stock-screener
# 独立 venv（uv；系统 pip 太老勿依赖）
~/.hermes/bin/uv sync --all-extras        # 创建 .venv 并装依赖
source .venv/bin/activate
```

Python 3.10；核心依赖：baostock、pandas≥2.0、PyYAML、requests、fastapi、uvicorn（dev: pytest）。

### baostock + pandas≥2.0 兼容说明

BaoStock 的 `get_data()` 在结果 >2000 行翻页时内部调用已被 pandas 2.x 移除的
`DataFrame.append()` → `AttributeError`。本项目统一手动 `while rs.next(): rows.append(rs.get_row_data())`
翻页（见 `screener/data/baostock_client.py`），与 pandas 版本无关，不钉死旧版。

## 2. 运行

```bash
cd ~/stock-screener
python -m screener --date YYYY-MM-DD --config config/strategy.yaml
# 可选参数：
#   --output-dir DIR     输出目录（默认 ./output）
#   --no-crosscheck      跳过腾讯交叉验证
```

- `--date` 非交易日 → 自动回退到最近一个交易日（报告注明）。
- `--date` **必须 ≤ 当前日期**：程序拒绝未来日期并报错退出（exit 1）。
- 退出码：0 成功；1 运行失败（含未来日期被拒、数据源级失败）；2 参数错误。
- 输出：`output/result_YYYYMMDD.csv`、`output/report_YYYYMMDD.md`、日志 `logs/run_YYYYMMDD.log`。

**耗时**：全市场 ~5200 只的日K/快照走腾讯批量接口（首跑 bootstrap 约 1~2h，之后增量秒级）；
v5 新增的央国企股东/现金流数据对**硬过滤后的候选股**逐只取新浪 F10（数百只，约 15~30min）。
同一天重复运行全部命中本地缓存，秒级完成、零网络。

## 3. 配置（config/strategy.yaml）

**所有筛选阈值都在 strategy.yaml，代码不硬编码任何阈值**
（`tests/test_no_hardcoded_thresholds.py` 做静态检查兜底）。带 `_pct` 的字段是百分数，
代码内部统一转小数比较。

### 四维打分权重（v5）

| 维度 | 权重 | 子因子（维度内权重和=1） |
|---|---|---|
| technical | **0.15** | `ma_bullish` / `low_vol` / `div_yield_pctile`（股息率历史分位）/ `yield_spread`（股息率−10Y国债利差）各 0.25 |
| dividend | **0.40** | `ttm_yield`(0.30) / `payout_ratio`(0.15，软约束 30~80%：区间外降分不剔除，v5.1) / `consecutive_div_years`(0.20) / `fcf_coverage`(0.20) / `div_stability`(0.15) |
| industry | 0.15 | `roe_rank_pct`(0.5) / `yoy_pni_rank_pct`(0.5) |
| fundamental | 0.30 | `roe_level`(0.3) / `roe_stability`(0.2) / `low_liability`(0.15) / `gross_margin`(0.15) / `piotroski` F-Score(0.2) |

> v5 相对 v4：technical 0.25→0.15、dividend 0.30→0.40（分红可持续性是核心）；
> technical 维度剔除趋势类 `window_return/rsi/macd`，换入**估值安全边际**因子
> （`div_yield_pctile`/`yield_spread`）——从"追涨趋势"转向"估值分位"，匹配长期持有目标。
> 估值因子并入 technical 维度是引擎固定 4 维的务实选择（config + 代码注释均写明）。

### v5 硬过滤（打分前剔除，全部 config 驱动）

| 配置项 | 默认 | 说明 |
|---|---|---|
| `universe.soe_required` | true | **央国企过滤**：前十大股东"股本性质=国有股" OR 名称命中关键词 → soe；否则剔除并进报告复核清单 |
| `universe.industry_whitelist_csric2` | 12 类 | **行业白名单**（证监会二级）：煤炭/石油/燃料加工/电力/燃气/水/铁路/道路/水上/航空运输/电信/银行 |
| `universe.min_total_mv_yi` | 200 | 总市值下限（亿元，腾讯快照 idx45），避免小市值央国企波动 |
| `universe.soe_keywords` | 国务院/国资委/汇金/财政部/国资 | 央国企识别关键词（⚠️银行类前十大实控人标记全为 0，必须靠关键词兜住国有大行） |
| `hard_filter.min_consecutive_div_years` | 5 | **连续分红年数**下限；IPO<7 年的新股改为"IPO 后每个完整年度均分红"（不硬砍优质次新） |

### 再投资参考输出（v5，TL D8；v5.1 V1-5 多期平滑）

| 配置项 | 默认 | 说明 |
|---|---|---|
| `reinvest.target_ttm_yield_pct` | 4.0 | 目标 TTM 股息率（%）：参考价 = **近 N 年平均 DPS / 目标股息率**（默认 N=3，窗口内无分红年按 0——断档拉低参考价）+ 当前股息率历史分位，辅助判断买入窗口 |
| `reinvest.dps_smooth_years` | 3 | v5.1：参考价平滑窗口年数（N<1 → 单年 DPS 原行为；键缺失同义） |
| `reinvest.dps_growth_years` | 5 | v5.1：**近 5 年 DPS CAGR** 展示列（`dps_cagr_5y_pct`，首末年均有分红才计算，否则空） |

### 其他维度阈值（v1~v4 沿用）

| 配置项 | 默认 | 说明 |
|---|---|---|
| `technical.ma_period` | 200 | 收盘价 > MA{n}（后复权日K） |
| `dividend.min_yield_pct` | 3 | TTM 股息率 ≥ 3% |
| `industry.top_pct` | 30 | 行业内 ROE 前 30% |
| `fundamental.roe_min_pct` | 10 | ROE ≥ 10%（最近披露报告期，累计口径未年化） |
| `fundamental.liability_max_pct` | 60 | 资产负债率 ≤ 60% |
| `fundamental.gross_margin_min_pct` | 20 | 毛利率 > 20%（金融业该字段为空 → 落缺失名单） |
| `universe.a_share_prefixes` | sh.60/68, sz.00/30 | A股前缀过滤（剔除指数/ETF/B股） |
| `universe.listing_min_trading_days` | 250 | 上市满 N 个交易日 |

## 4. 数据源架构（v4/v5）

多源分工，**单一主源失效不阻塞整体**：

| 数据 | 源 | 说明 |
|---|---|---|
| 日K / 实时快照（高频） | **腾讯** `qt.gtimg.cn` 批量 | v4 起主源；后复权重建、除权检测（preclose 信号）、市值/股息率交叉校验 |
| 全市场股票列表 / 行业分类 / 季度基本面 | **BaoStock**（低频） | 半封禁态有 ≤7 天陈旧池回退；`query_all_stock` 返回空时自动回退陈旧池而非伪装成空结果 |
| 前十大股东（央国企识别）/ 经营现金流（FCF覆盖） | **新浪 F10** `vip.stock.finance.sina.com.cn` + `quotes.sina.cn` JSON API | v5 新增；对硬过滤后候选股逐只取，串行限速 ≥1s、WAF(456) 长退避、连续失败降级 None |
| 10Y 国债收益率（yield_spread 的 rf） | **TradingEconomics** 页面解析 | v5 新增；每日抓现值落盘 `cache/rf_10y_daily.csv`，解析失败回退 config `2.0%` + 告警 |
| 分红全史（连续年数/稳定性/股息率分位） | 本地缓存 `cache/em_dividend_all.csv` | 静态历史数据源（1991→今），零网络；Phase 2 用 BaoStock 对账修正 |
| ~~东财 datacenter-web~~ | **已停用**（`datasource.em.enabled: false`） | 海外 IP 封禁（本机在欧洲）。代码保留不删，引擎侧 D-EM 守卫 + 单测断言零调用 |

> **PIT（point-in-time）纪律**：分红事件锚 = 除权日 ≤ 运行日；股东/现金流表按公告日 ≤ 运行日取报告期。
> 回测与实盘共用同一数据口径，无未来函数。

## 5. 筛选逻辑（实现要点）

1. **股票池**：`query_all_stock(day=...)` 显式传 day → 前缀过滤 A股 → tradeStatus=1；
   空结果自动回退 ≤7 天陈旧池（半封禁态防护）。
2. **ST 剔除以日K `isST=1` 为准**（名称含 ST 仅作辅助标记）。
3. **技术面**：后复权日K `adjustflag="1"`（1=后复权,2=前复权,3=不复权，必须传字符串）。
4. **分红可持续性五因子**：TTM 股息率、股利支付率（软约束 30%~80%，区间外降分不剔除，v5.1）、连续分红年数、
   FCF/OCF 对分红的覆盖倍数、近 N 年 DPS 波动率（越低越好）。
5. **估值安全边际**：当前 TTM 股息率在自身历史序列中的分位（越高=越低估）、
   股息率 − 10Y 国债利差（越大安全垫越厚）。
6. **央国企识别双规则**：股本性质=国有股 OR 名称关键词命中；单列"仅标记未命中关键词"复核清单。
7. **基本面**：字段全是小数（roeAvg=0.10 即 10%），阈值用小数比较、展示 ×100；
   "最近披露报告期"从当前季度往前逐季探测，不硬编码。Piotroski F-Score 九信号。
8. **行业**：证监会二级（84 类）按 code left-join；白名单外剔除；组内 ROE 百分位保留前 30%；
   不足 5 只的组跳过排名约束并在报告注明。
9. **输出**：CSV（代码/名称/行业/收盘价/各因子值/soe判定依据/再投资参考价/综合分）；
   报告（每层漏斗、最终入选列表、SOE 复核清单、数据时间戳与来源、缺失/异常名单）。

## 6. 本地缓存（同一天重复运行不重复拉取）

- 目录 `cache/`，CSV 原子写（`.tmp` + `os.replace`），首行哨兵标记防损坏文件误读。
- 键 = (查询类型, 参数)：个股日K/分红/季报不可变 → **永不过期**；行业分类 TTL 24h；交易日历 TTL 1h。
- `all_stock`：非空结果不可变 → 永不过期；**空结果不落盘**（防"数据尚未产生"污染真实运行）。
- v5 新增缓存键：`em_dividend_all.csv`（分红全史）、`rf_10y_daily.csv`（国债日频）、
  新浪股东/现金流逐只文件。独立哨兵，不改动任何 v4 缓存文件。

## 7. 单元测试（离线，不联网）

```bash
source .venv/bin/activate
python -m pytest -q          # 350 passed
```

覆盖：技术面指标、股息率（真实样例去重/窗口边界/无分红不报错）、基本面（小数口径/字段切换）、
行业排名、股票池（真实样例）、缓存（命中/过期/损坏/原子写）、腾讯 GBK 解析、配置校验、
**无硬编码阈值静态检查**、v5 新因子（连续分红年数/D1新股边界/FCF覆盖/股息率分位/soe双规则/
每10股单位换算/同除权日去重）、**D-EM 零东财调用断言**（AST + runtime 双断言）、
**v5.1 评审精加工**（payout 软约束区间外降分、reinvest 多期平滑参考价/DPS CAGR、
consecutive_div_years 边界、rf fallback data_notes 回归、v4 配置零回归）。

## 8. Web 前端（控制台）

`web/` 下 FastAPI 后端 + 无构建静态单页应用，用于查看运行结果、编辑策略阈值、触发新筛选。
复用现有 `.venv`，不引入数据库——所有数据读自 `output/`、`config/`、`logs/` 文件系统。

### 启动（systemd user service）

```bash
# unit: ~/.config/systemd/user/stock-screener-web.service
systemctl --user daemon-reload
systemctl --user enable --now stock-screener-web.service
systemctl --user status stock-screener-web.service
# 绑定 0.0.0.0:9090（访问控制由云安全组负责，应用层不加认证）
```

打开 `http://<host>:9090/`，页签：**结果**（运行列表→漏斗条形图→入选表→全量幸存者表，可搜索/排序/分页）、
**策略**（分组卡片展示各维度阈值+说明，编辑模式改完提交 PUT）、**运行**（选日期触发筛选，轮询实时日志尾部）。

### API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查（ok/time/project_root/active_task） |
| GET | `/api/runs` | 扫描 `output/` 列出已有运行，倒序 |
| GET | `/api/runs/{YYYYMMDD}` | 该次运行详情：漏斗、入选列表、全量 CSV(JSON)、缺失名单、报告原文 |
| GET | `/api/strategy` | 当前 strategy.yaml 解析后 JSON + 原始文本 |
| PUT | `/api/strategy` | 更新阈值（逐项校验，非法 400；先备份 `.bak` 再原子写回，保留注释） |
| POST | `/api/runs` | body `{run_date}`。后台子进程执行 CLI，立即返回 task_id；同一时间只允许一个任务（已有则 409） |
| GET | `/api/runs/{task_id}/status` | running/done/failed + 日志尾部 + 完成后指向结果 |

- **只读复用** `screener/` 主代码：Web 不改动筛选逻辑，仅调用 CLI 子进程与读取产物。
- 触发运行用**子进程**而非 uvicorn 线程内跑（首跑可能 ~1h+，不能阻塞 API）。
- PUT 写回保留 YAML 注释（ruamel round-trip + 叶子级最小替换），NO-OP 保存字节一致。

## 9. cron 部署说明

服务器时区 **UTC**；A股收盘后日K约北京时间 17:30（UTC 09:30）起更新，T+1 必可取。
建议每个交易日 **UTC 09:35**（北京 17:35）起跑，配节假日守卫避免回退重算旧数据：

```cron
# crontab -e（周一~周五 UTC 09:35）
35 9 * * 1-5 /home/ubuntu/stock-screener/run_cron.sh >> /home/ubuntu/stock-screener/logs/cron.log 2>&1
```

`run_cron.sh` 先用交易日历判断今天是否交易日（非交易日直接 exit 0），再跑 CLI。
进程级超时兜底（BaoStock 是 ctypes C 库，socket 超时无效，必须进程级 `timeout -k`）。
数据源级失败（如 BaoStock 封禁）显式失败 + sidecar 标红，**不写误导性空结果**。

> 注：当前生产环境 cron 按需求保持精简，首次切换手动 / Web 按钮触发。

## 10. 目录结构

```
stock-screener/
├── pyproject.toml            # uv 项目定义
├── config/
│   ├── strategy.yaml         # v5 全部筛选阈值（唯一配置源）
│   └── strategy_v4.yaml      # v4 原配置（零回归基线）
├── screener/
│   ├── __main__.py           # CLI 入口 (python -m screener)
│   ├── config.py             # 配置加载+校验（百分数→小数换算）
│   ├── universe.py           # 股票池（前缀/停牌/ST + v5 白名单/SOE/市值硬过滤）
│   ├── metrics.py            # 指标纯函数（技术面/股息五因子/估值分位/基本面/行业/F-Score/soe_flag）
│   ├── scoring.py            # Z-Score 截面打分引擎（四维固定，sub_weights 驱动）
│   ├── screener.py           # 筛选引擎编排（硬过滤→取数→打分→报告）
│   ├── report.py             # CSV + Markdown 报告输出（含再投资参考价/SOE复核清单）
│   ├── runstatus.py          # 运行状态 sidecar
│   ├── migrate.py / prewarm_fundamentals.py / reconstruct.py   # 数据迁移/预热/后复权重建
│   └── data/
│       ├── baostock_client.py  # BaoStock 登录态/指数退避/手动翻页（pandas≥2.0 兼容）
│       ├── cache.py            # 本地 CSV 缓存（原子写+哨兵+TTL）
│       ├── fetchers.py         # 各接口抓取（缓存优先 + 半封禁态陈旧回退）
│       ├── sources.py          # 数据源抽象层（腾讯主源 / BaoStock 低频）
│       ├── tencent.py          # 腾讯批量快照/日K（GBK 转码，除权检测）
│       ├── sina.py             # v5：新浪 F10 股东 + 财务 JSON（OCF），WAF 鲁棒
│       ├── rf.py               # v5：TradingEconomics 10Y 国债收益率
│       └── em.py               # 东财 datacenter-web 客户端（v5 已停用，代码保留）
├── tests/                    # 350 个离线单测 + fixtures（真实样例数据）
├── web/                      # Web 前端（FastAPI + 静态 SPA，端口 9090）
│   ├── app.py                # FastAPI 应用（API + 子进程任务管理 + 策略校验）
│   └── static/               # index.html / style.css / app.js（vanilla JS，无构建）
├── cache/                    # 原始数据缓存（自动生成）
├── output/                   # result_*.csv / report_*.md（自动生成）
└── logs/                     # run_*.log / web_run_*.log / cron.log
```

## 11. 已知限制

- **BaoStock 半封禁态**：全市场 `query_all_stock`/基本面接口间歇性返回空或挂起，
  已用 ≤7 天陈旧池回退 + 进程级超时兜底；Phase 2 计划解封后做分红双源对账。
- **新浪 F10 WAF**：非官方接口，高频触发 HTTP 456，已用串行限速 ≥1s + 长退避 + 连续失败降级 None（missing_policy=neutral_renorm 兜底）。
- **东财海外封禁**：本机在欧洲，datacenter-web 不可达；`em.py` 代码保留但 `enabled: false`，
  分红全史改用本地静态缓存。若迁回境内服务器可重新启用做对账源。
- **行业分类为证监会二级口径**（84 类），未引入申万依赖（无免费可靠源）。
- **FCF 覆盖用 OCF 口径**：新浪财务 API 无资本开支字段，`fcf_coverage` = 经营现金流量净额 / 年度分红总额
  （比严格 FCF 略宽松，方向一致、更保守地衡量分红安全垫）；接口失败降级 `cfo_to_np/payout` 代理。
- **rf 历史序列**：Phase 1 仅用 TE 现值算 yield_spread，不要求 rf 历史分位（div_yield_pctile 只用本地价格+分红史，与 rf 无关）。
