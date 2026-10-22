# f6 基线 fixture 说明

- 来源: /tmp/f6_*/ (Joel 2026-10-22 原跑产物，tmpfs 易失，已快照)
- 快照时间: 2026-10-22 (coder 阶段第一步，先于一切删码)
- 生成脚本: scripts/backtest_martingale.py (状态机移植入 martingale/engine.py 后删除)
- 参数 f6: initial=10000, first_amount=100000, drawdown=0.15, step=0.05, growth=1.1,
  ladder=high, rearm=bounce, rearm_pct=0.15, accounting=cash, comm=3bps, stamp=5bps
- 窗口: 2016-01-04 → 2026-10-21
- 已验收数字 (Joel 2026-10-22 验收; 逐位对齐 = per_stock.csv 数值字段 + summary.md
  买入时间表 日期/价格/金额 完全一致):
  - sh.601088 中国神华: 11 买, 210,510 → 660,997.72 (+214.0%)
  - sh.600036 招商银行: 22 买, 326,610 → 748,168.72 (+129.1%)
  - sz.000651 格力电器: 21 买, 326,102 → 540,050.34 (+65.6%)
  - sh.600900 长江电力: 1 买 (2022-11-03), 100,000 → 166,047.77 (+66.0%)
  - sh.601668 中国建筑: 18 买, 284,820 → 402,119.96 (+41.2%)
  - sh.600938 中海油: 10 买, 200,510 → 414,582.05 (+106.8%)
  - sz.000858 五粮液: 43 买, 605,355 → 576,613.88 (-4.7%)
- 每个子目录含: per_stock.csv (回测主结果) + summary.md (含逐笔买入时间表)
- summary.md 的"耗时 Xs"行不要求一致 (机器相关)。
- 重跑复现: .venv/bin/python -m martingale backtest --start 2016-01-04 --end 2026-10-21
  --codes <code> --out <tmpdir> (参数取 f6 值, 见 tests/test_f6_regression.py)
