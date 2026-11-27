# T5 备源 fuyao PIT 验证 → 质量不达标，T5 定为主源单源（2026-11-27 定案记录）

**状态**: ✅ 验证完成 → Joel 判定 vendor 质量不达标 → **T5 = adata_f10 主源单源，
不设备源**（fuyao 不接收为备源）

**决策链（Joel 2026-11-27）**: ① "去掉 em，只验证 fuyao，如果没问题就正式把主备固定
下来" → PIT 验证执行完毕（结果见下）；② 验证暴露 vendor 质量问题（2024Q4 重述漂移
6 股 + 1 处孤立陈旧单元格）后 Joel 改判："既然这个备源质量不高的话，就算了，不要放
fuyao 了，直接只有主源就行了" → T5 定为主源单源，source_pool 已按此落定。

## 范围与方法

- 对标样本：41 股（湖内 fundamentals_quarterly 随机 40 + sh.600546 补入）× 3 期
  （2026Q3 / 2025Q4 / 2024Q4）= **123 行**，fuyao 现值 vs 湖内 adata_f10 行。
- 工具：`scripts/t5_fuyao_verify.py`（结果 JSON `/tmp/fuyao_t5_verify.json`，
  41 股 349s，api_fail=3 为瞬时限频）。
- 字段映射（fuyao income-statements / financial-indicators → 湖内列）：

| 湖内列 | fuyao 字段 | 容差 |
|---|---|---|
| npi | parent_holder_net_profit | 相对 ±1% |
| yoy_pni | calculate_parent_holder_net_profit_yoy_growth_ratio | ±1.0pp |
| roe_weighted | index_weighted_avg_roe | ±0.05pp |
| gross_margin | sale_gross_margin | ±0.05pp |
| liability_pct | assets_debt_ratio | ±0.05pp |
| pub_date | report_date_ms | PIT 判定 |

（初版脚本 yoy 误用 `net_profit_yoy_growth_ratio` 键名 → 123 one_null；实际字段带
`calculate_` 前缀，已修正并重跑。npi 判定口径：湖内 npi = **归母**（parent 123/123
全一致；total 仅 60 匹配）。）

## 结果

### A. 数据对标（123 行）

| 指标 | ok | diff | one_null | both_null | 判读 |
|---|---|---|---|---|---|
| npi（归母） | **123** | 0 | 0 | 0 | 全一致 ✅ |
| yoy_pni | 117 | 3 | 3 | 0 | 异常全在 2024Q4 重述期 |
| roe | 115 | 3 | 3 | 2 | 异常全在 2024Q4 |
| gm | 115 | 2 | 3 | 3 | 异常全在 2024Q4 |
| liab | 116 | 4 | 3 | 0 | 6 处 2024Q4 + 1 处孤立 |

差异明细（18 处，除标注外全部为 2024Q4 重述期 vendor 间重述差异，涉 6 股：
sz.300087 / sz.002547 / sz.002878 / sh.600173 / sh.688598 / sz.300781）：

- 2024Q4 重述差异（历史期，预期内，不阻塞）：300087 roe Δ-0.87/liab Δ0.21；
  002547 roe/gm/liab fuyao 缺值；002878 roe Δ-4.81/gm Δ-1.61/liab Δ1.00；
  600173 与 688598 三项 fuyao 缺值；300781 roe Δ-0.57/gm Δ-7.83/liab Δ-9.62；
  yoy：300087 Δ-5.59pp、300781 Δ-8.83pp、002878 Δ-2145pp（负基数放大）、
  002547/600173/688598 fuyao 缺值。
- **孤立异常 1 处**：sh.600960 2025Q4 liab 湖内 55.896 vs fuyao 41.884（Δ14.0pp）。
  判定：adata_f10 **主源现值实时重拉 = 55.896365416（与湖内逐位一致）**，且湖内行
  2026-11-15 已刷新 → fuyao 该单元格为孤立陈旧值（≈其 2025Q3 水平），
  主源+湖内一致，不阻塞备源资格。

### B. PIT 检查（pub_date）

- **2025Q4 + 2026Q3：82/82 与湖内 pub_date 精确一致**（= 公告日，非刷新时间戳）。
- **2024Q4：41/41 fuyao report_date 晚于湖内 pub_date**（2026-03/04 重述公告日，
  湖内为 2025-03/04 原始公告日）→ **重述漂移**：fuyao 当前 2024Q4 行是 2026 年
  发布的重述值，不可挂主源 pub_date 使用；备源行如启用须以 fuyao 自身
  report_date 为 PIT 锚。
- **0 行早于湖内 pub_date → 无 lookahead**。

### 结论

- 主期（2025Q4/2026Q3）：npi 全一致 + pub_date 全精确 + yoy/roe/gm/liab 99% 内一致。
- **质量判读（决定不收为备源的原因）**：123 行样本中 18 处异常格集中在
  2024Q4 重述期（6 股 vendor 间重述漂移，最大 Δ：002878 yoy Δ-2145pp 负基数放大、
  300781 gm Δ-7.83pp/liab Δ-9.62pp）+ 1 处孤立陈旧单元格（600960 2025Q4 liab
  Δ14.0pp，主源现值裁决为 fuyao 陈旧）→ 重述期数据漂移 + 单元格陈旧，作为
  备源/PIT 锚不可靠（备源行若启用还需按 fuyao 自身 report_date 重挂锚点）。
- **Joel 定案**：质量不达标 → 不收 fuyao 备源，T5 只留主源 adata_f10。
  （T5 表本身不受影响：adata_f10 日增正常，主期数据 100% 可信。）

## 定案后的 T5 源结构（source_pool.py）

- **主源 = adata_f10**（唯一灌数 adapter，`config.py` ingest 优先级不变）。
- **无备源/交叉校验**：eastmoney T5 条目已移除；fuyao 能力表仅剩 T6 交叉校验
  （待验证），T5 条目不登记。
- fuyao 日常观察保留：`scripts/lake_observation.py::obs_fuyao`（income-statements
  单期探活 + report_date_ms 在场性）——供将来重新评估。

## 涉及文件

- `lake/ingest/source_pool.py`（EM T5 条目移除；fuyao T5 条目不登记，留注释）
- `lake/config.py`（T5 注释：主源单源定案记录）
- `scripts/t5_fuyao_verify.py`（yoy 字段名修正 + docstring 结论）
- `tests/test_lake_v613_source_pool.py`（fuyao 能力表=仅 T6；T1 sina 晋主断言补齐）
