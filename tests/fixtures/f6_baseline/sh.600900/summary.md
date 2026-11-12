# 马丁格尔深回撤加仓回测 summary

- 参数: lot_size=100, first_lots=1, add_lots_step=1, first_drawdown_pct=15%, add_step_pct=5%, rearm_mode=bounce, rearm_pct=15%, accounting=cash, comm=3bps, stamp=5bps
- 数据: f6_v8.1.0_fixture
- 窗口: 2016-01-04T00:00:00.000000000 ~ 2026-10-21T00:00:00.000000000 (2604 交易日), 股票数: 1
- 口径: 信号=后复权; cash：原始价成交(人民币)+现金分红单列, 股价增长=总盈亏-分红
- 偏差: 湖内无退市股（幸存者偏差）；--start 若晚于上市日则高点为窗口内高点

## 触发
- 触发过建仓的股票: 0/1 (0.0%)

## 收益（触发过的股票，pnl_pct = 总盈亏/外部净投入；v7.0.36 分红余额优先出资）

## 组合账本（每日累计，equity_curve.csv）
- 期末: 未平仓 0 只, 成本 0, 市值 0, 浮盈 0 (N/A)

## 基准对照（各股窗口内 hfq 首末收益，等权平均，买持）
- mean=231.3%, median=231.3%, p1=231.3%, p99=231.3%

## Top / Bottom 10（按 pnl_pct，仅触发过的股票）
耗时 0.7s
