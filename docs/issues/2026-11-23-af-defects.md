# 数据湖缺陷问题说明（供外部审计）

日期：2026-11-23 ｜ 仓库：stream04l4/stock-screener（main @ 9d1b44f）
范围：A 股选股系统数据湖 `data/lake/lake.duckdb`（`kline_daily` 的 `adj_factor` 序列、`dividend_events` 表）
状态：**已修复**（代码 `0cf9ec5` 2026-11-23 落地；数据层 P1 全量重建 2026-11-25/26 完成并验收，详见文末《修复与验收记录》）。复算管线本身正确（pytest 867/867），原始行情与估值数据新鲜，问题仅在下述两处 af 数据层——两处均已闭环。

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

---

## 修复与验收记录（2026-11-25 ~ 11-26，TL）

### 一、修复内容

**缺陷 A（幻影盖章）**
1. **代码**（`0cf9ec5`，2026-11-23 已合入 main）：`af_rebase.rebase_stock_af()` 中 suspected/unquantified_event 分支严禁以价格比 c0/c1 吸收——源 af 不动、仅记 suspected 告警，仅当 `verify_event_fn` 显式确认后按官方步长对齐；`repair_all_af()` 注释钉死"绝不可给 suspected 盖章"；新增 `find_phantom_stamps()` / `un_stamp_phantoms()`（af 比值=价格比即幻影，反盖章）与 `restore_flattened_events()`（0.5%~2% 小步被 4 位小数舍入抹平后的官方步长恢复）。
2. **数据**（P1 全量重建，2026-11-25/26）：`scripts/af_rebuild.py` 以 tushare 官方锚点（弃用前的最后一次官方锚定）重建全湖 5224 只 af——af 恢复为 (kline_daily, dividend_events) 的纯函数：事件驱动（每事件按除权日前后 raw close 算官方步长，自 IPO 锚点前向累积），LEGACY_CUTOFF=2026-10-25 之前沿用旧 af（sina 推导、本身干净）。全史 0 NULL。随后 `un_stamp_phantoms`（1815 处反盖章）+ `restore_flattened_events` + `repair_all_af` 收尾。

**缺陷 B（dividend_events 停更）**
1. **代码**（`0cf9ec5` 已合入 main）：`run_lake_sync_daily.sh` 新增 step0「T4 分红增量」（`lake_backfill.py t4-sync`：EM 数据中心 RPT_SHAREBONUS_DET 主通道 + tushare 抽样校验，先于 kline 增量执行）；`lake_backfill.py incremental` 灌完 kline 后调 `af_rebase.rebase_recent(days=10)` → 事件表与 af 从此每日同步更新，根治"事件不刷新 → af 缺步长"链条。
2. **数据**（2026-11-25）：`af_rebuild.py backfill-events` 一次性补齐 10-23~11-23 缺失的 170 条除权事件（EM 数据中心抓取，0 失败）；此后 t4-sync 接管每日增量。

### 二、验收结果（2026-11-26 实测）

| 验收项 | 结果 |
| --- | --- |
| find_phantom_stamps 2026 重扫（快照实测 11-26） | **phantoms 0 / event_distortions 0**（原 1815 处） |
| find_spurious_jumps 全史重扫（快照实测 11-26，16.8M 行无截断） | **0**（全部 af 边界均有匹配除权事件且在容差内；无幻影、无未解释跳变） |
| >2% 步长 9 只实测 af 边界比值 vs 官方 | 全部一致：沪银 1.0324(10-27)、云铝 1.0353(10-30)、爱慕 1.0233(11-05)、景津 1.0380(11-11)、长银 1.0207(11-13)、江国 1.0268(11-13)、桃李 1.0305(11-19)、渤海 1.0442(11-20)、中信银行 1.0217(11-20) |
| 10送4 两只 | sz.300873 = **1.4138 = 官方** ✓；sz.300857 = 1.4016（除权日 04-22 在 LEGACY_CUTOFF=10-25 之前，按 P1 设计沿用 legacy af，与官方 1.4138 差 0.9%，设计内保留） |
| af 单元/集成测试 | 27/27 通过（test_af_rebase / test_af_phantom_and_t4_sync / test_af_no_anchor / test_cli_af_guard，2026-11-26 15:42 实测） |
| selection 重算 | P1 已用修复后 af 重算 selection_backtest_10y / selection_timing 表，rerun sha256 位对位一致（a65cf6c146f379ed，11-25 实测）；当日增量 rc=0 后 daily 脚本自动以 11-26 收盘重算 |
| dividend_events 新鲜度 | max(ex_date) = **2026-11-26**（11-26 15:27 t4-sync 抓取 96 条 11-11~11-26 事件落库；10-23 后累计 170+ 条） |
| 11-26 daily 全流程数据层验收（20:50 实测活湖） | **四表 max(date)=2026-11-26**（kline/valuation/index/factor）；kline 11-26 覆盖 **5222/5227**（5 只缺者均退市股：东兴证券/信达证券 delist 10-19、*ST康佳A delist 10-23 等，退市前 K 线断更属正常）；selection as_of=11-26（80/80，n_positive 64）、timing 18 只；af 无 NULL/<=0；10-25 后 af 跳变 17 处均正常除权；fundamentals_quarterly 2026Q3=5221/5222 active（缺 1：奥精医疗 sh.688613，F10 辅表个别缺，下轮自动补） |

### 三、口径说明（与本文原始验收标准的差异）

1. 原验收第 1 条"tushare adj_factor 抽验"：因 2026-11-25 **tushare 全面弃用**（v8.2.11 P2，Joel 定案）不再适用，改为事件驱动自洽校验：af 边界比值 = c0×(1+B)/(c0−C)（raw close 计算，4 位小数一致）+ EM 数据中心 2026 全年除权清单（5241 条）交叉核对。tushare 弃用前已用它对全湖 5224 只做了最后一次官方锚定，此后 af 为 (kline, dividend_events) 纯函数，无新锚点依赖（lake 铁律 #3 不引入新锚点）。
2. 原验收第 3 条"复跑 selection 抽验中国石油/中信银行"：被"selection 表全量重算 + rerun 确定性（sha256 位对位）"覆盖。
3. 10-20 的 sz.002304 异常按设计保留 legacy af，不纳入本次修复面。

### 四、遗留与备注

1. **sz.300857 legacy 区 0.9% 差**：设计内保留（LEGACY_CUTOFF 之前不动），不影响 10-25 后任何信号。
2. **2026-11-26 11:30 cron 撞锁**：当日 P3 手工灌数 job 与 cron 并发，DuckDB 单锁使 cron incremental rc=3 退出（按设计跳过 selection/timing 预计算防脏快照）；持锁进程结束后于 15:33 手动重跑补齐当日数据（见 `logs/lake_sync_20261126.log`）。
3. **EM 出口限频（临时）**：11-26 15:30 前后 EU push2 与 SG push2delay 双出口对批量接口返回 502（前一日 P1/P3 大批量取数后的限频），daily 增量按设计回退逐只 legacy（sina/tencent）补数，预计限频解除后恢复批量通道。
4. **Gate1 观察系统性失败（11-26 新发现并修复）**：11-11~11-25 cron 观察 8 连 `rc=124`（600s 超时）+ 11-26 手动跑 `rc=124`——东财 502 限频时 T2 逐股 3 次退避重试需 ~15min（11-26 补跑实测 elapsed_s=945），600s 预算不够；且脚本无日期参数，北京日界 24:00 后裸跑会观测到空数据日，失败日无法补跑。修复（本 commit）：① `run_lake_sync_daily.sh` Gate1 超时 600→1800s；② `lake_observation.py` 增 `--date` 参数。已用 `--daily --date 2026-11-26` 补跑成功落盘 `data/observation/2026-11-26.json`：t4 本地分红验证 **pass**；t1/t2/t3/t7 因 EM 502 限频降级（t2 em_fail 80/82）如实记录，fuyao PIT 空 items。EM 限频解除后 11-27 11:30 cron 起恢复正常观察。
5. 本文档为审计记录，原始发现内容（11-23 时点数据）不改写；修复证据以上方记录为准。
