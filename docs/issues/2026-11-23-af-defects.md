# 数据湖缺陷问题说明（供外部审计）

日期：2026-11-23 ｜ 仓库：stream04l4/stock-screener（main @ 9d1b44f）
范围：A 股选股系统数据湖 `data/lake/lake.duckdb`（`kline_daily` 的 `adj_factor` 序列、`dividend_events` 表）
状态：**未修复**，待审计处理。复算管线本身正确（pytest 867/867），原始行情与估值数据新鲜，问题仅在下述两处 af 数据层。

---

## 缺陷 A：af"幻影盖章"——rebase 的 suspected 分支把暴跌日误判为除权日

### 问题说明
`af_rebase.py` 的 `classify_boundary()`（L51 `DEFAULT_TOL=0.02`、L53 `LARGE_DROP_RATIO=1.08`）三分支中，
`suspected` 分支（L80-83）：当某边界日**收盘跌幅 ≥8%**（price_ratio=c0/c1 ≥ 1.08）且**无已知除权事件**时，
判定条件 `spurious = |log(af_move/price_ratio)|>tol and |log(af_move)|>tol` 成立即视为"假基准跳变"，
随后 `rebase_stock_af()` 的修正（L184 附近："suspected 或无金额占位事件：回退价格吸收公式"，
`scale = scale * (c0*af0)/(c1*af1)`）会**强制把该边界的 af 步长写成当日价格比**——
即把一次普通的市场大跌当作"未记录的除权事件"吸收进复权因子。

### 后果
- 被盖章后 qfq 序列在暴跌日变得平坦（跌幅被抹掉），该股票**之后所有日期的复权价失真**，
  10 年回测 pnl（G7）与 5 年分位（G9）被**系统性虚高**；
- 盖章结果是检测器的不动点（af:=price_ratio 后下次扫描偏差=0，`find_spurious_jumps` 不再报），**无法自愈**；
- 每次 `repair_all_af` 全史运行都会重放此行为。

### 证据（已实锤）
1. tushare 官方锚点 sz.301219：2026-10-20→11-23 adj_factor 恒为 3.4404（无步长），
   湖里 10-30（当日 -13.2%）af 恰跳 1.152 = 当日价格比（4 位小数一致）；
2. EM 数据中心 2026 全年除权清单（5241 条，`RPT_SHAREBONUS_DET`）核对：
   2026 年"af 比≈价格比且 ≥7.5%"的 1932 个边界中 **1815 个当日无任何除权事件**，
   集中在 11-20（全市场跌 7%+，单日约 500 只）、10-26~30、11-16 等暴跌日；
3. 2016-2025 同类签名无单日聚集（最多单日 4 只）→ 历史部分多为真分红未入库，影响轻微。

### 规模与生产影响
- 2026：**1815 处边界 / 1177 只股票**；
- 现 84 基础池中 3 只受影响：sh.600392、sh.601857、sh.601995；
- 其中 **sh.601857 中国石油在现 17 候选池（5y 分位 99.1）**，11-05 一处幻影 +8.6%，分位虚高；
- 另有 2 只真实 10送4（官方步长 1.4138）被覆盖为价格比（1.1758/1.1680），失真约 20%（sz.300873、sz.300857，不在池内）;
- 当前实盘持仓 4 只、今日选股（中信特钢）经核对**不受影响**。

### 修复要求
1. **代码**：suspected 分支（单日跌 ≥8% 且无除权记录）不得以价格比为修正目标。
   建议：不动源 af，仅记告警/标记待核（如需吸收应先用 EM 除权接口在线核实确为未记录事件）；
2. **数据**：对 1815 处幻影边界做代数反盖章（后段 af 除以价格比，恢复平坦），
   2 只被覆盖 10送4 的按官方步长恢复；
3. **验收**：抽样 tushare `adj_factor` 核对边界平坦性；`find_spurious_jumps` 全史重扫；
   重算 `selection_backtest_10y`/`selection_timing` 并输出池成员与分位前后 diff。

---

## 缺陷 B：`dividend_events` 表停更（2026-10-23 后无增量）

### 问题说明
`dividend_events` 最大 ex_date = 2026-10-23（末次系统全量拉取 2026-09-15，56973 行），
10-20→11-23 共 **147 条除权事件缺失**。该表是 `find_spurious_jumps` 的 `has_ev` 事件源：
事件缺失时，真实除权边界落入 `no_event` 分支，其官方步长被 rebase **抹平为 1.0**。

### 后果（对 147 条缺失事件逐一核对）
- **9 只官方步长 >2% 被抹**（qfq 自除权日起整体偏低一个步长）：
  上海银行 1.0324、云铝股份 1.0353、爱慕股份 1.0233、景津装备 1.0380、
  常熟银行 1.0207、江苏国泰 1.0268、桃李面包 1.0305、渤海轮渡 1.0442、**中信银行 1.0217**；
  其中**中信银行 sh.601998 在现 17 候选池**（5y 分位 100.0），常熟银行在 84 基础池；
- 51 只步长 0.5-2% 被抹、41 只 <0.5%（影响可忽略）；
- G8（TTM 股息率）读 `valuation_daily`（新鲜至 11-23），**不受影响**。

### 根因
`scripts/run_lake_sync_daily.sh` 无分红增量环节；tushare `dividend` 接口免费档限频 1 次/小时，
09-15 全量拉取后再未更新。

### 修复要求
1. `dividend_events` 补齐 10-23 后全部事件（可用 EM 数据中心
   `datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_SHAREBONUS_DET` 分页拉取，已验证可用）；
2. 日同步脚本增加分红增量（EM 为主、tushare 交叉校验），避免再停更；
3. 补齐后重跑 rebase，恢复 9+51 只的真实步长；
4. **验收**：`dividend_events` 最大 ex_date = 最新交易日；9 只 >2% 步长股的 af 边界比 ≈ 官方步长；
   重算选股并输出 diff。

---

## 已验证正确的部分（审计可跳过）
- `kline_daily` 原始行情：新鲜至 2026-11-23，16,848,460 行，per-code MAX(date) 行组统计一致；
- `valuation_daily`：新鲜至 2026-11-23（5222 只），G8 输入正确；
- `martingale.timing` 重算幂等；全量回归 **pytest 867 passed**（425.86s）；
- cron：`lake-sync-daily` 11:30（工作日）、`signal-touch-daily` 12:05 正常执行。

## 关键文件
`af_rebase.py`（classify_boundary L62-84、修正逻辑 L184 附近）、`scripts/run_lake_sync_daily.sh`、
`data/lake/lake.duckdb`（kline_daily / dividend_events / valuation_daily / selection_backtest_10y / selection_timing）、
`data/user_positions.json`。
