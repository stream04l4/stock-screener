# 加密货币板块设计 v0.2（待批准，未开始实现）

- 日期：2026-11-11（Joel 定案两轮：① 加密货币建在 stock-screener 项目内，同一
  GitHub 仓库分 A 股 + 加密货币两部分；② **加密货币系统独立页面、与股票系统
  隔离、保持登录页双入口、股票页有跳转加密货币的入口、数据分两套**）
- 状态：**设计阶段**——P0 数据源已实测验证；架构理解待 Joel 确认后实施
- 纪律：A 股部分代码零改动（展示层 3 文件例外，见 §3）；push 按 Joel 逐次指示

## 0. 结论摘要

- **一个 GitHub 仓库，两个独立系统**：
  - 股票系统（现有）：:9090，独立进程/独立服务/独立湖 `lake.duckdb`
  - 加密货币系统（新增）：**:9091 独立进程 / 独立 systemd 服务 / 独立前端
    SPA / 独立湖 `crypto.duckdb`**——独立页面，不是股票页里的 tab
- **登录页保持双入口**（沿用旧 9090/9091 双系统登录机制，commit `727a3dd`
  同款）：登录页两个按钮「📉 A股4维选股」/「🪙 加密货币」；同一
  `POST /api/login` 写 ssc_session cookie（**按 hostname 跨端口生效**），
  点哪个按钮整页跳哪个系统，免登。
- **交叉跳转**：股票页顶栏加「🪙 加密货币」跳 :9091；加密货币页顶栏对称加
  「📉 股票」回跳 :9090。
- **数据分两套**：两个 DuckDB 文件，互不读写。
- 数据源（2026-11-11 本机 EU 实测 6/6 全 200）：Binance 主源（7 年 1h 全量）
  + OKX 备源交叉校验。
- 三阶段：P0 数据底座 → P1 加密货币独立系统（:9091 + 双入口 + 交叉跳转）
  → P2 持仓/告警（可选，待拍板）。
- **明确不做**：ML 预测/信号/回测（方向已第 5 次带功效关闭）、自动交易。

## 1. 背景

1. 原 Quant iTransformer（:9091 独立系统）2026-11-11 已完全删除（仓库/服务/
   目录全清）。删除时 stock-screener 前端做过的「单入口」解耦（v7.0.42，
   commit `21c4532`，**未推送**）——**该方向作废**：按本设计恢复双入口，
   加密货币按钮改指新的加密货币系统（端口沿用旧 Quant 的 :9091）。
2. 原 GitHub 项目 `stream04l4/quant_iTransformer` 不再引用（远端未动，处置
   待指示）。
3. Joel 观察的「双登录通道」= 目标形态，保留。

## 2. 总体架构（v0.2 核心）

```
GitHub 仓库 stream04l4/stock-screener（一个仓库，两个独立系统）
│
├── 股票系统（现有，进程/服务/数据不动*）
│     服务: 现有 :9090 systemd user service（独立进程）
│     前端: web/frontend/（股票 SPA，独立 build）
│     数据: data/lake/lake.duckdb（16.7M 行，lake-sync 11:30）
│     登录: /login 双入口（按钮一：A股4维选股）
│
└── 加密货币系统（新增，完全独立部署单元）
      服务: 新 crypto-web.service → :9091（独立进程，独立端口）
      前端: crypto/web/frontend/（加密货币 SPA，独立 build，独立页面）
      数据: data/lake/crypto.duckdb（独立湖，crypto-sync 每小时）
      登录: /login 双入口（按钮二：加密货币）

共享登录（= 旧 Quant 机制 727a3dd，恢复）：
  · 两个系统的登录页都是双入口（同一页面组件，对称）
  · 同一 POST /api/login → ssc_session cookie 按 hostname 写（不带端口）
    → 浏览器对 :9090 和 :9091 都带上 → 两个后端用同一 secret 校验
    → 任一端登录，另一端免登；点哪个按钮跳哪个系统

交叉跳转（顶栏链接，= 旧 ⚡Quant 入口的对称版）：
  · 股票页顶栏 → 「🪙 加密货币」:9091
  · 加密货币页顶栏 → 「📉 股票」:9090

隔离边界（进程级）：
  共享 = 同一仓库 / 同一登录 cookie 机制（同 secret）/（P2）同一 PushPlus
  隔离 = 进程、端口、systemd 服务、前端 SPA、数据湖；依赖可独立升级
  → 加密货币系统崩溃/重建/升级，股票 :9090 完全无感；反之亦然
```

\* 股票系统唯一改动 = 登录页恢复双按钮 + 顶栏加跳转链接（展示层 3 个文件，
即 v7.0.42 已改文件的反向调整；策略/数学/推送/数据零改动）。

**为什么不是单进程双 tab（v0.1 方案）**：Joel 要求「独立页面、隔离开」——
独立进程 + 独立端口 + 独立 SPA + 独立湖才是进程级隔离；且与旧 9090/9091
体验一致（Joel 已认可的形态）。代价：维护两个 build/服务，可接受。

## 3. 仓库结构（新增文件）

```
stock-screener/
├── [股票系统，现有]  web/app.py, web/auth.py, web/frontend/, lake/, martingale/, ...
│
├── crypto/                          # 加密货币系统（新增顶层包 = 独立部署单元）
│   ├── __init__.py
│   ├── __main__.py                  # 启动入口（:9091）
│   ├── config/crypto.yaml           # 币种列表/数据源/回补窗口/端口
│   ├── sources/{__init__.py, binance.py, okx.py}
│   ├── sync.py                      # python -m crypto.sync [--backfill]
│   ├── models.py                    # crypto.duckdb 表 DDL + 幂等 upsert
│   └── web/
│       ├── app.py                   # 独立 FastAPI 应用（:9091，/api/*）
│       ├── auth.py                  # ssc_session 校验（与股票同 secret）
│       └── frontend/                # 独立 Vue SPA（独立 package.json/dist）
│           └── src/{views/LoginView.vue(双入口), tabs/CoinTable.vue,
│                    tabs/KlineChart.vue, ...}
│
├── data/lake/crypto.duckdb          # 独立湖（gitignore，同 A 股湖惯例）
└── docs/crypto_design.md            # 本文件
```

现有文件触点（最小）：
- 股票登录页/顶栏：恢复双按钮 + 跳转链接（v7.0.42 反向调整，3 文件）
- 登录 secret：两系统共用同一 secret（同 .env）
- crontab：+1 行 crypto-sync
- systemd：+1 个 user service（crypto-web.service）

## 4. 数据层（P0）

### 4.1 表设计（crypto.duckdb，独立库）

```sql
coin_master(symbol PK, name, exchange, is_active, added_at);
kline_1h(exchange, symbol, ts, open, high, low, close,
         volume, quote_volume, PK(exchange, symbol, ts));   -- 索引 (symbol, ts)
-- P2（可选）：crypto_positions(symbol PK, cost_price, qty, note, updated_at);
```

1d/4h K 线由 1h 本地聚合，不建表（避免多粒度双写不一致）。

### 4.2 数据源（2026-11-11 08:50 UTC 本机 EU 实测）

| 源 | 端点 | 实测 | 角色 |
|---|---|---|---|
| **Binance** | api.binance.com/api/v3/klines | 200；1h 全量回溯至 **2019-01-01**（首根 ts=1546300800000）；BTC 最新 73735.52 | **主源** |
| **OKX** | www.okx.com/api/v5/market/candles | 200；1H 与 Binance 价差 <0.01% | **备源/交叉校验** |
| Bybit / Kraken / Gate | — | 200 | 后备 |
| CoinGecko | — | 200（免费档历史浅） | 弃用 |

- 全公共端点，**无需 API key**；EU 直连全通；SG 出口（oci 中继）最后备用。
- 限速余量：小时增量 <10 请求 ≈ Binance 权重 0.01%。
- 交叉校验：每次同步比对 Binance vs OKX 最近 24h 收盘价，偏差 >0.1% 告警。

### 4.3 同步策略

- 首次回填：手动 `python -m crypto.sync --backfill`（20 币 ≈ 1.06M 行
  <150MB，<30min，限速内分批）。
- 日常：cron 每小时 :05，幂等 upsert 回补最近 3h（自愈漏同步/交易所补发）。
- 实时报价：不跑 cron——前端轮询 15s + 后端 5s TTL 缓存（1 个请求取全部
  币种 24h ticker）。

## 5. 加密货币系统 Web 层（P1，:9091 独立应用）

### 5.1 后端（crypto/web/app.py，独立 FastAPI）

| 端点 | 用途 |
|---|---|
| `POST /api/login` / `POST /api/logout` / `GET /api/auth/me` | 与股票同款（同 cookie/secret） |
| `GET /api/coins` | 币列表 + 最新价 + 24h 涨跌/高低 + 7 日 sparkline |
| `GET /api/kline?symbol=&interval=1h\|4h\|1d&from=&to=` | K 线（ECharts candlestick） |
| `GET /api/quote?symbol=` | 单币实时报价（5s 缓存） |
| `GET /api/health` | 健康检查 |

- 全部 `/api/*` 需登录（与股票同款门禁）。
- 独立进程：崩溃/升级/重启只影响 :9091。

### 5.2 前端（crypto/web/frontend，独立 SPA）

- 登录页：**双入口**（与股票登录页同款组件：「📉 A股4维选股」/「🪙 加密货币」，
  分别跳 :9090 / 停留 :9091）
- 顶栏：「📉 股票」回跳链接 + 系统标识「🪙 加密货币」
- 页面：
  - **市场表**：币种/最新价/24h 涨跌%/24h 高低/成交量/7 日走势（ECharts
    mini-line），行点击 → K 线
  - **K 线详情**：candlestick + MA(20/60) + 成交量副图，周期切换
    1h/4h/1d（4h/1d 本地聚合）
- 报价 15s 轮询（仅页面打开时）。
- 技术栈与股票前端一致（Vue3 + Pinia + ECharts 5.6），独立 build、独立
  node_modules，互不影响；公共组件初期直接复制，后续再考虑抽共享包。

## 6. 调度

```
crontab +1 行：
5 * * * *  cd ~/stock-screener && .venv/bin/python -m crypto.sync >> logs/crypto_sync.log 2>&1
systemd +1 个 user service：crypto-web.service（:9091，同股票服务惯例）
```

现有 3 行 crontab 与股票服务不动。

## 7. 明确不做（边界）

- ❌ ML 预测/信号/回测/仓位建议（方向已第 5 次带功效关闭，11-08 横截面
  终审 FAIL 定论）——加密货币系统 = **数据 + 行情独立页面**，不是交易系统。
- ❌ 自动交易/下单/交易所 key 托管。
- ❌ 股票系统策略/数学/推送/数据任何改动（唯一展示层改动 = 登录双入口 +
  顶栏跳转，见 §3）。

## 8. 阶段与验收

| 阶段 | 内容 | 预估 | 验收标准 | 决策门 |
|---|---|---|---|---|
| **P0 数据底座** | crypto/ 数据源 + sync + 表 + 回填 + cron | 0.5d | 20 币 × 7 年 1h 行数与交易所一致；抽 3 根 Binance vs OKX 一致；股票回归全绿；:9090 无感 | ① 币种清单 |
| **P1 加密货币独立系统** | :9091 应用 + 独立 SPA（双入口登录 + 市场表 + K 线）+ 股票侧恢复双按钮/顶栏跳转 + crypto-web.service | 1d | 双入口互跳实测（任一端登录另一端免登）；顶栏交叉跳转实测；页面 3 币数据与交易所一致；股票全量回归绿 + :9090 无回归；**杀掉 :9091 进程后 :9090 全功能正常（隔离验证）** | ② 是否上 P2 |
| **P2 持仓/告警**（可选） | crypto_positions + 持仓页（成本/盈亏，仿 A 股手动登记）+ PushPlus | 0.5d | 同 P1 + 告警实测 | ③ 账本归属 |

每阶段本地 commit（版本号从 v7.0.43 延续，README changelog 同步）；push 等
Joel 逐次指示（v7.0.42 单入口解耦 commit 被 v0.2 方向取代，未推送不影响远端）。

## 9. 待 Joel 拍板清单

1. **币种范围**：top 20（推荐）/ 仅 BTC+ETH / 自定义清单
2. **功能深度**：P0+P1 先上，P2 看到独立页面后再定（推荐）/ 一步到位含 P2
3. **K 线粒度**：1h 基础 + 本地聚合 4h/1d（推荐）/ 另存 15min
4. **端口/服务**：沿用 :9091 + 新 systemd user service `crypto-web.service`
   （推荐，与旧 Quant 位置一致，ss 已验证 9091 空闲）/ 其他端口
5. **（若上 P2）**加密货币持仓与 A 股持仓：分账本（推荐，独立系统内）/ 合一本账
6. **推送时机**：说「推送」即推（当前本地未推 commit：v7.0.42 + 本设计文档）

## 10. 风险与对策

| 风险 | 对策 |
|---|---|
| 双 build 维护成本 | 技术栈一致，公共组件复制起步；独立是 Joel 明确要求的隔离代价 |
| Binance EU 封锁/限速变化 | 双源架构：OKX 备源同构，切主改 1 行配置；SG 出口最后备用 |
| K 线缺口 | 每小时回补 3h 窗口自愈；回填后行数核对 |
| 数据质量（插针/坏 bar） | 交叉校验偏差 >0.1% 告警；异常 bar 标记不删除（保原始） |
| cookie secret 共享 | 同一 .env 管理，与旧 9090/9091 机制相同，无新增暴露面 |
| 端口 9091 冲突 | 已验证空闲（旧 quant-tower 已删）；冲突则改 1 行配置换端口 |

## 附录：P0 实测证据（2026-11-11 08:50 UTC，本机 EU）

- 6/6 数据源 ping 200（Binance/OKX/Bybit/Kraken/Gate/CoinGecko）
- Binance `klines?symbol=BTCUSDT&interval=1h`：最新 3 根正常（close 73735.52）
- Binance `startTime=1546300800000`：首根 ts=1546300800000（2019-01-01，
  7 年全量历史可用）
- OKX `candles?instId=BTC-USDT&bar=1H`：数据正常，与 Binance 价差 <0.01%
- Bybit `kline?category=spot&symbol=BTCUSDT&interval=60`：数据正常
- 本机端口 9091 空闲（旧 quant-tower 服务已删，ss 验证）
