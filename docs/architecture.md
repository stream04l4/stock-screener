# 项目地图（双系统架构，v7.0.43 起）

> 本文是目录架构的**单一权威说明**：新代码放哪里、什么不能动、两个系统的
> 边界在哪。加密货币系统的设计细节另见 `docs/crypto_design.md`。

## 1. 总览：一个仓库，两个独立系统

**访问入口（v7.0.44，443 单域名 HTTPS）：**
- `https://stream04l4.duckdns.org/` → 股票系统
- `https://stream04l4.duckdns.org/crypto/` → 加密货币系统
- nginx（443，Let's Encrypt 证书）按路径分流到 127.0.0.1:9090 / :9091；
  9090/9091 仅本机可达（公网建议只放通 443，9090/9091 在路由层关闭）
- 同域 cookie（ssc_session）天然跨系统共享 → 双端互免登（不再有跨端口问题）
- 证书续期：acme.sh ALPN-01（仅 443 可用，80 未开）+ pre/post 钩子自动腾挪 443
  （/usr/local/sbin/ssl443-pre|post）+ 5 分钟看门狗兜底；nginx 站点配置在
  /etc/nginx/sites-available/stream04l4（权威副本 docs/nginx-stream04l4.conf）

| | 股票系统（A股） | 加密货币系统 |
|---|---|---|
| 入口路径 | / | /crypto/ |
| 端口 | :9090 | :9091 |
| 服务 | stock-screener-web.service | crypto-web.service + **crypto-gateway.service**（实时 WS 网关，纯内存态） |
| 后端 | `web/app.py`（web/auth.py 共享登录模块） | `crypto/web/app.py`（import web.auth；对外挂 /crypto 前缀 + 外层 auth 门） |
| 前端 | `web/frontend/`（独立 SPA + build） | `crypto/web/frontend/`（独立 SPA + build） |
| 代码包 | `lake/` `martingale/` `screener/` `af_rebase.py` | `crypto/`（sources/sync/models/web/positions/strategy/backtest） |
| 配置 | `config/strategy.yaml` | `crypto/config/crypto.yaml` |
| 数据 | `data/lake/` `data/raw/` `data/canonical/` `data/alerts/` `data/observation/` `data/strategies/` `data/*.json` | **`data/crypto/`（独占，湖 = data/crypto/crypto.duckdb）** |
| 脚本 | `scripts/`（run_*.sh + 同步/告警 py） | `python -m crypto.sync`（cron 直接调）+ `python -m crypto.gateway`（systemd 常驻） |
| 定时 | crontab 5 条业务行（lake-sync 11:30 / gate1-latency / signal-touch 12:05 / t1-delist 周日 / t6 季度） | crontab 1 条：crypto-sync 每小时 :05 |
| 测试 | `tests/`（665 用例，含 v7 防护契约） | 后端并入 tests/（test_crypto_* 前缀）+ 前端 crypto/web/frontend 自测 |

**共享基础（仅有的跨系统耦合，刻意保持最小）：**
1. `.env`（WEB_ADMIN_USER/WEB_ADMIN_PASS/WEB_SESSION_SECRET）——同账号密码，
   ssc_session cookie 同域共享 → 双端互免登
2. `web/auth.py`——加密货币后端直接 import（登录机制单一定义点，不复制）
3. nginx 443 入口（/ 与 /crypto/ 路径分流）——两系统共享同一 TLS 证书与域名
4. 后续 P2：PushPlus 推送（密钥同 .env）

**隔离保证（硬指标，已有回归验证）：** 停 crypto-web.service 后 :9090 全功能
正常；两湖物理独立（DuckDB 单写锁，禁共库）；两前端独立 node_modules/build。

## 2. 顶层目录职责

```
stock-screener/
├── README.md            # 项目说明 + changelog（版本号单一记录点）
├── pyproject.toml       # packages = ["screener", "lake", "martingale"]（形状受 v7 防护契约锁定）
├── af_rebase.py         # 仓库根共享模块（lake↔martingale 共用，v7.0.21 提升；位置有契约约束）
├── .env                 # 凭据（gitignore，禁入 git/日志）
│
├── config/              # 【股票】strategy.yaml（回测/持仓/信号锁定配置）
├── lake/                # 【股票】数据湖包（ingest/ vendor/；⚠️ 有基线防护，见 §4）
├── martingale/          # 【股票】策略包（状态机/时机/选股）
├── screener/            # 【股票】选股包
├── scripts/             # 【股票】运维脚本（run_*.sh + lake_*.py + position_alert.py）
├── web/                 # 【股票】Web 后端 + 前端（:9090）
│
├── crypto/              # 【加密货币】完整独立单元（内部结构见 §3）
├── data/                # 运行时数据（整体 gitignore，首次运行自动拉取）
│   ├── lake/            # 【股票】lake.duckdb（1.8G）+ 湖侧伴生 json/log
│   ├── raw/             # 【股票】交易所原始数据（2.8G，sina 为主）
│   ├── canonical/       # 【股票】派生 Schema CSV
│   ├── alerts/          # 【股票】告警状态（pushplus/signal_touch/timing）
│   ├── observation/     # 【股票】Gate1 观察期记录
│   ├── strategies/      # 【股票】本机策略库快照
│   ├── user_positions.json / my_trades.json   # 【股票】用户持仓/账本
│   └── crypto/          # 【加密货币】独占：crypto.duckdb + sync_state.json
│                        #    + gateway_state.json（实时网关状态）
│                        #    + vision_cache/（月度快照沙盒缓存，不入库）
│                        #    + backtest_report.md（策略回测报告，v7.0.47）
├── output/              # 回测产物（gitignore）；v7 现行 = backtest_martingale/
├── cache/               # 拉取缓存（gitignore）
├── logs/                # 运行日志（gitignore）
├── docs/                # 架构与专项设计（本文件 + crypto_design.md）
├── tests/              # 全量测试（694；v7 防护契约 test_v7_guards.py）
└── stages/              # 已删（v6 团队流水线副产物）
```

## 3. crypto/ 内部结构（后续扩展落点）

```
crypto/
├── config/crypto.yaml   # 唯一配置入口（币种/数据源/端口/湖路径/vision 段）
├── sources/             # 数据源适配（binance REST 主源 / okx 备源 /
│                        #   binance_vision 月度快照全量历史源，纯 stdlib）
├── sync.py              # python -m crypto.sync [--days N|--full]（幂等）
│                        #   --full = vision 历史段 + REST 当月尾巴（v7.0.45）
├── models.py            # 湖表 DDL + upsert + stats（路径由配置传入，不读配置）
├── gateway/             # 实时 K 线网关（WS 组合流，**纯内存态不入湖**，
│   │                    #   systemd crypto-gateway.service；120s 看门狗 +
│   │                    #   指数退避重连 + 状态文件 gateway_state.json）
├── web/
│   ├── app.py           # 独立 FastAPI（:9091；login 三件套 + lake/coins/kline/
│   │                    #   gateway 实体端点 + positions/strategy/backtest 骨架端点）
│   └── frontend/        # 独立 Vue SPA（双入口登录 + 四页签；dist 不入库）
├── positions/           # P2 我的持仓（预留；__init__ 内写明规划）
├── strategy/            # P3 策略（v7.0.47 实体化）：engine.py 马丁格尔
│                        #   状态机（StrategyParams 单一参数点 + run_coin 纯函数）
└── backtest/            # P3 回测（v7.0.47 实体化）：runner.py 湖读取 +
                         #   python -m crypto.backtest CLI，报告落
                         #   data/crypto/backtest_report.md
```

## 4. 扩展约定（新东西放哪里）

**股票系统新功能** → 按现有分层：策略逻辑进 `martingale/`（单一定义点）、
数据管道进 `lake/`、Web 端点进 `web/app.py`、页面进 `web/frontend/src/tabs/`。
**不改顶层布局**——股票侧是已验证的稳定区（防护契约锁定，见 §5）。

**加密货币系统新功能** → 一律落 `crypto/` 内：
- 新数据源 → `crypto/sources/<exchange>.py`（同构接口：klines/ticker）
- 新湖表 → `crypto/models.py`（DDL 集中一处）
- **实时/离线边界（v7.0.45 Joel 定案）**：数据湖 = 离线数据（cron 同步刷新，
  最多落后约一天）；`crypto/gateway/` = 实时底座（WS 内存态，**不入湖**），
  服务后续实时买点判断。新实时功能读 gateway 状态（内存/状态文件），
  新历史功能读湖——两者不互写。
- 新端点 → `crypto/web/app.py` 的 core_app（路由保持 /api/*，对外自动带 /crypto
  前缀；**新 API 必须走外层 auth 门覆盖范围**，公开端点加进 _AUTH_PUBLIC_PATHS）；
  新页面 → `crypto/web/frontend/src/tabs/`（vite base '/crypto/'，API 走 api() 单一入口）
- 持仓/策略/回测 → 各自预留子包（positions/strategy/backtest）
- 运行时数据 → `data/crypto/`（不碰 A 股 data/ 其余目录）
- 测试 → `tests/test_crypto_*.py`（并入主套件，随 665+ 全量回归跑）

**跨系统改动**（登录/凭据/共享模块）→ 必须两端同时验证：股票 665 全量 +
加密前端自测 + 跨端口免登实测（设计文档 §8 证据清单为模板）。

## 5. 不可动清单（防护契约锁定，改 = 先改 tests/test_v7_guards.py 契约）

1. `lake/` 相对基线 commit `7f7b2ba` 零 diff（test_lake_zero_diff_vs_baseline）
2. `pyproject.toml` 形状：version 7.0.0 + packages 含 martingale/lake（test_pyproject_v7_shape）
3. crontab 业务行恰 6 条（5 股票 + 1 crypto-sync，test_crontab_only_lake_sync_business_line）
4. `lake↔martingale` 零 import 耦合（af_rebase 提根的由来）
5. 登录机制单一定义点 `web/auth.py`（加密侧 import，不复制粘贴）
6. 重大版本先与 Joel 议字段（memory 纪律）

## 6. 变更记录

- **v7.0.47（2026-11-11）**：加密马丁格尔策略 + 全史回测。`crypto.yaml`
  新增 strategy 段（Joel 2026-11-11 规格 + 两决策门：建仓=一年高点×30%
  即跌 70%；止盈=建仓价×10 确认）；`crypto/strategy/engine.py` 状态机
  （滚动 8760h 一年高点、1% 固定网格加仓 10×1.05^k 不设上限、新高不刷新
  entry、10x 全卖重置）；`python -m crypto.backtest` 全史回测 →
  data/crypto/backtest_report.md（BTC/ETH 各了结 1 轮 2018/2020 建仓 →
  2021 10x 止盈 +1427%/+3022%；2022 轮均持仓中待 10x）；
  tests/test_crypto_strategy.py 13 用例（合成路径零网络）。全量 694 过。
- **v7.0.46b（2026-11-11）**：数据湖 tab 聚焦 3 币展示——stats() per_symbol
  加 active 标记（LEFT JOIN coin_master），LakeTab 主表只列 active 3 币、
  退役 17 币折叠展示（数据保留，仅显示层）。
- **v7.0.46（2026-11-11）**：回测湖依赖自包含化——test_martingale_web 13 例
  原依赖 /tmp/lake_bt_20261022.duckdb 外部快照（被 ops 清理丢失 → skip），
  改为自包含合成湖（生产 DDL + 4 股受控 K 线路径，bounce 再武装语义
  确定性推值），零外部依赖不随行情漂移。
- **v7.0.45（2026-11-11）**：数据湖三标的全量 + 实时网关。coins 聚焦
  BTCUSDT/ETHUSDT/SOLUSDT（top20 其余 17 币 is_active=FALSE 数据保留）；
  `crypto.sources.binance_vision`（官方月度快照全量历史，纯 stdlib，
  4 线程 + 沙盒缓存 + 403/429 退避）；`crypto.sync --full` = vision 历史段
  + REST 当月尾巴；`crypto.gateway/` 实时 K 线网关（WS 组合流 + 120s 看门狗
  + 指数退避重连，**纯内存态不入湖**——湖由 cron 离线刷新）；web 加
  /api/gateway 端点 + LakeTab 实时卡片；tests/test_crypto_data.py（15 用例）。
- **v7.0.44（2026-11-11）**：443 单域名 HTTPS 入口。nginx 按路径分流
  （/ → 股票 :9090，/crypto/ → 加密 :9091，前缀不剥）；Let's Encrypt
  证书（acme.sh ALPN-01 签发 + 自动续期钩子）；crypto 后端/前端统一 /crypto
  前缀（vite base + API 单一入口），跨系统跳转改同源路径，cookie 同域共享；
  修复 Mount 挂载下 auth 中间件不执行的问题（外层 auth 门兜底，探针验证 401）。
- **v7.0.43（2026-11-11）**：双系统架构落地。加密货币数据从 data/lake/ 迁入
  data/crypto/（独占目录）；crypto/ 预留 positions/strategy/backtest 子包；
  删除 v6 遗留（stages/ 37M、output/ 三个无引用旧目录）；本文建立。
