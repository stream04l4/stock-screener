# -*- coding: utf-8 -*-
"""martingale —— 深回撤金字塔加仓策略（v7.0 唯一保留策略）。

模块布局（与 lake/ 平级的独立包，零 import lake）：
- config.py   参数加载与校验（strategy/universe 段；pct 字段 yaml 存整数 %，
              本层统一转小数供引擎比较）
- data.py     数据湖只读连接（read-only duckdb，路径可注入；锁/缺失/损坏错误
              归一，语义同 lake.conn 的读路径）
- universe.py 股票池（白名单∩SOE∩市值∩上市∩连续分红，全离线单库）
- engine.py   每股状态机（自 scripts/backtest_martingale.py 逐行移植；
              信号=kline_daily_hfq 收盘，成交=kline_daily 原始收盘，
              分红=dividend_events；cash 口径固定；不卖/不封顶/无熔断）
- report.py   产物写盘（per_stock.csv / buys.csv / equity_curve.csv /
              summary.md / status.json）
- __main__.py CLI（backtest / status / pool 三子命令；[PROGRESS] 标记喂 Web SSE）

边界纪律：
- 不 import lake（lake 亦不得 import 本包——test_lake_zero_import 双向守卫）；
- 不 import 任何 screener.*（screener 已退化为数据客户端库，本包只读 duckdb 湖）；
- 所有策略阈值来自 config/strategy.yaml，代码零硬编码策略阈值
  （comm/stamp 为引擎常量，TL 拍板不暴露——见 engine.py 注释）。
"""

__version__ = "7.0.0"
