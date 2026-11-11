# 加密货币板块设计 v0.1（待批准，未开始实现）

- 日期：2026-11-11（Joel 定案：加密货币在 stock-screener 项目内构建，同一 GitHub
  仓库分 A 股 + 加密货币两部分）
- 状态：**设计阶段**——P0 数据源已实测验证，其余待 Joel 拍板后实施
- 纪律：A 股部分代码零改动；push 按 Joel 逐次指示

## 0. 结论摘要

- 加密货币板块 = stock-screener 第二模块：**新顶层包 `crypto/` + 独立 DuckDB 湖
  `data/lake/crypto.duckdb` + 同服务 `/api/crypto/*`（:9090）+ 新前端「🪙 加密货币」
  tab**，同一仓库、同一 Web 服务、**同一登录**（不建第二登录通道）。
- 数据源（2026-11-11 本机 EU 直连实测，6/6 全 200）：**Binance 主源**（7 年 1h
  全量历史，2019-01-01 起）+ **OKX 备源交叉校验**；Bybit/Kraken/Gate/CoinGecko
  实测可用但不用（保留为后备）。
- 三阶段：**P0 数据底座 → P1 Web 行情 → P2 持仓/告警（可选，待拍板）**。
- **明确不做**：ML 预测/信号/回测（方向已第 5 次带功效关闭）、自动交易/下单。

## 1. 背景

1. 原 Quant iTransformer（:9091 独立系统）2026-11-11 已完全删除：本机仓库
   （1.8G）/quant 服务 ×3/joint_project 4 目录全删，stock-screener 前端登录解耦
   （v7.0.42，commit `21c4532`，本地已上线 :9090）。
2. Joel 观察到的「两个登录通道」= 旧 Quant 入口，本地已移除；**GitHub 远端仍是
   旧版本（未推送）**——如看到双通道是远端/浏览器缓存。新设计下加密货币部分
   无独立登录：一个登录覆盖 A 股 + 加密货币两个 tab。
3. 原 GitHub 项目 `stream04l4/quant_iTransformer` 不再引用（远端仓库未动，处置
   待 Joel 指示）。

## 2. 总体架构

```
GitHub 仓库 stream04l4/stock-screener（同一仓库）
│
├── A 股部分（现有，零改动）
│     lake/lake.duckdb (16.7M 行)  ← lake-sync 工作日 11:30
│     martingale/ backtest_pipeline/ config/strategy.yaml
│
└── 加密货币部分（新增）
      data/lake/crypto.duckdb      ← crypto-sync 每小时 :05
      crypto/（数据源/同步/配置，独立顶层包）

Web：同一 FastAPI 服务 :9090
  ├── /api/*         现有 A 股路由（web/app.py，零改动）
  └── /api/crypto/*  新路由（web/crypto_api.py，挂 2 行；登录门禁自动覆盖）

前端：同一 Vue 应用（ECharts 5.6 已在依赖中）
  ├── 现有 4 个 tab（策略/回测/数据湖/我的持仓）
  └── 新「🪙 加密货币」tab（市场表 + K 线，懒加载隔离）

推送：复用 PushPlus（仅 P2，可选）
```

**独立湖的理由**：DuckDB 单进程写锁——A 股 lake-sync（11:30 增量 15min）与加密
小时级同步若共库必撞锁；两库规模/更新频率/备份策略也完全不同。独立文件 = 零
冲突、A 股侧完全隔离。

## 3. 仓库结构（新增文件）

```
stock-screener/
  crypto/                          # 新增顶层包
    __init__.py
    config/crypto.yaml             # 币种列表/数据源/回补窗口（A 股 yaml 同款风格）
    sources/
      __init__.py
      binance.py                   # 主源：klines + 现货报价（REST，无 key）
      okx.py                       # 备源：candles 交叉校验
    sync.py                        # 同步入口：python -m crypto.sync [--backfill]
    models.py                      # 表 DDL + upsert（幂等）
  data/lake/crypto.duckdb          # 独立湖（gitignore，同 A 股湖惯例）
  web/crypto_api.py                # APIRouter(prefix="/api/crypto")
  web/frontend/src/tabs/CryptoTab.vue
  web/frontend/src/tabs/crypto/{CoinTable.vue, KlineChart.vue}
  web/frontend/src/api/crypto.js
  docs/crypto_design.md            # 本文件
```

**对现有代码的最小触点**（共 3 处，均 1-2 行）：
- `web/app.py`：`include_router(crypto_api.router)`（+ import，2 行）
- 前端 tab 注册处：+1 个 tab 入口
- crontab：+1 行

A 股策略/数学/推送/数据/测试：**零改动**。

## 4. 数据层（P0）

### 4.1 表设计（crypto.duckdb）

```sql
coin_master(symbol PK, name, exchange, is_active, added_at);
kline_1h(exchange, symbol, ts, open, high, low, close,
         volume, quote_volume,
         PK(exchange, symbol, ts));      -- 索引 (symbol, ts)
-- P2（可选）：
crypto_positions(symbol PK, cost_price, qty, note, updated_at);
```

1d/4h K 线**不建表**：由 1h 本地聚合（ECharts 数据侧或 SQL 窗口聚合），
避免多粒度双写不一致。

### 4.2 数据源（2026-11-11 08:50 UTC 本机实测）

| 源 | 端点 | 实测 | 角色 |
|---|---|---|---|
| **Binance** | api.binance.com/api/v3/klines | 200；1h 全量回溯至 **2019-01-01**（首根 ts=1546300800000）；BTC 最新 73735.52 | **主源** |
| **OKX** | www.okx.com/api/v5/market/candles | 200；1H 与 Binance 价差 <0.01% | **备源/交叉校验** |
| Bybit | api.bybit.com/v5/market/kline | 200 | 后备 |
| Kraken | api.kraken.com/0/public/Time | 200 | 后备 |
| Gate | api.gateio.ws/api/v4/spot/time | 200 | 后备 |
| CoinGecko | api.coingecko.com/api/v3/ping | 200（免费档历史浅，不用） | 弃用 |

- 全部公共端点，**无需 API key**；EU 直连全通，SG 出口（oci 中继）仅作最后
  备用，不进主链路。
- 限速余量：小时增量同步 <10 个请求 ≈ Binance 权重的 0.01%。
- 交叉校验：每次同步比对 Binance vs OKX 最近 24h 收盘价，偏差 >0.1% →
  日志告警（P2 起接 PushPlus）。

### 4.3 同步策略

- **首次回填**：手动 `python -m crypto.sync --backfill`，1h K 线 2019-01-01 起
  （20 币 ≈ 1.06M 行 ≈ <150MB，预计 <30min，限速内分批）。
- **日常**：cron 每小时 :05，幂等 upsert 回补最近 3 小时（自愈漏同步/交易所
  补发），已存在不重写。
- **实时报价**：不跑 cron——前端轮询（15s）+ 后端 5s TTL 缓存直连 Binance
  ticker（1 个请求取全部币种 24h 数据）。

## 5. Web 层（P1）

### 5.1 后端（web/crypto_api.py，APIRouter prefix=/api/crypto）

| 端点 | 用途 |
|---|---|
| `GET /api/crypto/coins` | 币列表 + 最新价 + 24h 涨跌/高低 + 7 日 sparkline |
| `GET /api/crypto/kline?symbol=&interval=1h\|4h\|1d&from=&to=` | K 线（ECharts candlestick） |
| `GET /api/crypto/quote?symbol=` | 单币实时报价（5s 缓存） |

- 登录：现有 `/api/*` 门禁自动覆盖（auth 中间件按路径前缀，无需改）。
- **故障隔离**：crypto router 内部异常自捕获返回 503 JSON，不抛出影响
  app.py 其他路由；前端 tab 懒加载（现有 tab 机制），加密货币部分加载失败
  不影响 A 股四个 tab。

### 5.2 前端

- 新 tab「🪙 加密货币」（顶栏，取代旧 ⚡Quant 的位）：
  - **市场表**（CoinTable.vue）：币种/最新价/24h 涨跌%/24h 高低/成交量/7 日
    走势（ECharts mini-line），行点击 → K 线
  - **K 线详情**（KlineChart.vue）：candlestick + MA(20/60) + 成交量副图，
    周期切换 1h/4h/1d（4h/1d 本地聚合）
- 报价 15s 轮询（仅 tab 打开时），关闭 tab 停轮询。

## 6. 调度

```
crontab +1 行：
5 * * * *  cd ~/stock-screener && .venv/bin/python -m crypto.sync >> logs/crypto_sync.log 2>&1
```

（现有 3 行不动：lake-sync 11:30 / t1-delist 周日 / t6 季度。）

## 7. 明确不做（边界）

- ❌ ML 预测/信号/回测/仓位建议——方向已 5 次带统计功效关闭（11-08 横截面
  终审 FAIL 为定论），本板块是**数据 + 行情展示**，不是交易系统。
- ❌ 自动交易/下单/交易所 key 托管。
- ❌ A 股策略/数学/推送/数据湖任何改动。

## 8. 阶段与验收

| 阶段 | 内容 | 预估 | 验收标准 | 决策门 |
|---|---|---|---|---|
| **P0 数据底座** | crypto/ 包 + 表 + Binance/OKX 源 + 回填 + cron | 0.5d | 20 币 × 7 年 1h 行数与交易所一致；抽 3 根 K 线 Binance vs OKX 一致；A 股全量回归绿；:9090 无感 | **① 币种清单** |
| **P1 Web 行情** | /api/crypto/* + 🪙 tab（市场表 + K 线） | 0.5-1d | 前端测试全绿 + A 股 636 绿；页面实测 3 币数据与交易所一致；A 股 tab 无回归 | **② 是否上 P2** |
| **P2 持仓/告警**（可选） | crypto_positions 表 + 持仓 tab（成本/盈亏，仿 A 股手动登记）+ PushPlus 告警 | 0.5d | 同 P1 + 告警实测 | ③ 账本归属 |

每阶段：本地 commit（v7.0.42 起延续版本号 v7.0.43/44…）；**push 等 Joel
逐次指示**（与 v7.0.42 登录解耦一起推或分开推）。

## 9. 待 Joel 拍板清单

1. **币种范围**：top 20（推荐，主流全 + 体量可控）/ 仅 BTC+ETH / 自定义清单
2. **功能深度**：P0+P1 先上（推荐，P2 看到行情页后再定）/ 一步到位含 P2
3. **K 线粒度**：1h 基础 + 本地聚合 4h/1d（推荐）/ 另存 15min 粒度
4. **（若上 P2）**加密货币持仓与 A 股持仓：分账本（推荐，独立模块）/ 合一本账
5. **推送时机**：本设计 + v7.0.42 登录解耦，何时 push 到 GitHub（说「推送」
   即推）

## 10. 风险与对策

| 风险 | 对策 |
|---|---|
| Binance EU 封锁/限速变化 | 双源架构：OKX 备源代码同步实现，切主改 1 行配置；SG 出口最后备用 |
| K 线缺口 | 每次同步回补 3h 窗口自愈；回填后行数核对交易所 |
| 数据质量（插针/坏 bar） | 交叉校验偏差 >0.1% 告警；异常 bar 标记不删除（保原始） |
| 9090 共享应用稳定性 | router 级异常隔离 + tab 懒加载；A 股全量回归作为每阶段准入门槛 |
| DuckDB 写锁 | 独立 crypto.duckdb，与 A 股湖物理隔离 |

## 附录：P0 实测证据（2026-11-11 08:50 UTC，本机 EU）

- 6/6 数据源 ping 200（Binance/OKX/Bybit/Kraken/Gate/CoinGecko）
- Binance `klines?symbol=BTCUSDT&interval=1h`：最新 3 根正常（close 73735.52）
- Binance `startTime=1546300800000`：首根 ts=1546300800000（2019-01-01，全量历史可用）
- OKX `candles?instId=BTC-USDT&bar=1H`：数据正常，与 Binance 价差 <0.01%
- Bybit `kline?category=spot&symbol=BTCUSDT&interval=60`：数据正常
