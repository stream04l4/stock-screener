# 源池总体清理：策略表主备 / 非策略表仅主源（2026-11-27 定案记录）

**状态**: ✅ 已执行（四方一致：source_pool.py / config.py / 测试 / 本文档）
**决策链（Joel 2026-11-27）**: ① 总体原则：「总体评估一下，按照当前的策略，哪些表是
需要的。后续总体设计原则如下，**策略需要的要主备源设计，策略不需要的只需要主源即可，
其他源删除掉**」→ TL 逐表评估（策略代码 grep + source_pool 声明 + 湖内真实行分布
三方核对）→ ②「全部按照推荐执行」（4 个决策点全按推荐）：
- ① T2 = **sina 主源 + EM 备源**（EM 批量快速通道，连续 2-4 周稳定后可再晋主）
- ② T4 = **接受 EM 单族**（em_datacenter 日增 + em_local_static 冻结历史基线，
  无非 EM 备源；分红低频，af 重建有 K 线除权交叉校验兜底）
- ③ T6 EM 备源 = **实测验证一次** → 通过，收为备源（见 §3）
- ④ T7 = **tencent 唯一主源**，tdx T7 条目移除（观察期 17 天 0 写入、未晋主）

## 1. 策略需要性判定（martingale 策略代码表引用实测）

| 表 | 策略引用处数 | 判定 | 定案形态 |
|---|---|---|---|
| T2 kline_daily | 21（含 hfq/qfq 视图） | 策略表 | sina 主 + EM 备（+tdx 交叉校验） |
| T6 holders_snapshot | 12 | 策略表 | sina 主 + EM 备（实测收编） |
| T3 valuation_daily | 11 | 策略表 | tencent 主 + EM 备（批量快速通道） |
| T1 stock_master | 9 | 策略表 | sina 主 + EM 备（t1-delist 独立路径，11-27 已定案） |
| T4 dividend_events | 4 | 策略表 | EM 单族（Joel 接受，见 ②） |
| T5 fundamentals_quarterly | 0（前端/factors 用） | 非策略 | adata_f10 主源单源（11-27 已定案） |
| T7 index_daily | 0 | 非策略 | tencent 唯一主源（tdx 观察期终止） |
| T8 factor_snapshot | 0 | 非策略 | 本地派生，无源（不进池） |
| T9 macro_rf | 0 | 非策略 | te_local 日更主源（不进池） |

## 2. 湖内真实写入者（11-27 只读核对，data/lake/lake.duckdb）

- **T2**（近 60 日按 source）：sina 287,749 / tencent 9,363（v6 时代历史兜底残留）/
  em_batch 5,217 / tdx 78。近 5 日：sina 每日 ~5,200 行主写；11-27 = em_batch 5,217 行
  + tdx 5 + sina 2。**11-11..11-25 EM 连续 8 日 502 限频，全部由 sina 逐只兜底写满**
  → sina=已验证主源锚点、EM=不稳定快速通道（降备依据）。
- **T3**：tencent 77,410 + em_batch 5,246（均→11-27 双写）；百度历史回填 119,658+63,738
  （10-23 冻结，v6.3.6 定案，不占主备槽）。
- **T4**：em_local_static 53,134（→09-18 冻结基线）+ em_datacenter 4,517（→11-27 日增）
  + web_verified 2（手工修复）。EM 单一族，无非 EM 备源。
- **T5**：adata_f10 324,531 单源。
- **T6**：sina_f10 2,685,023 唯一写入者；EM 备源 0 行（声明「待验证」）；fuyao 0 行。
- **T7**：tencent 1,308 唯一写入者（→11-27）；tdx 0 行（观察期 17 天未晋主）。
- **T1**：sina_spot 5,220 唯一实际写入者（baostock 340/tushare 6 为历史残留行）。
- **T9**：te_local 22（→11-27 日更）+ em_local_static 6,187（→10-23 冻结历史 CSV）。

## 3. T6 EM 备源实测验证（决策点 ③，scripts/t6_em_verify.py）

**端点**: `emweb.securities.eastmoney.com/PC_HSF10/ShareholderResearch/PageAjax?code=sh600519`
（EU 直连可达；⚠️ code 必须带交易所前缀，纯 6 位→「股票代码不合法」）。`sdgd` 键=
前十大股东（与新浪 vCI 页同口径——实测 sh.600519 rank1 茅台集团 54.5% = 湖内 54.499%），
字段 HOLDER_RANK/HOLDER_NAME/HOLD_NUM/HOLD_NUM_RATIO(%)。

**方法**: 20 股随机样本 × 湖内最新 as_of_date（多数=2026-09-30）vs EM 同 END_DATE 期，
rank 逐行比：名称归一化（全角括号/破折号→半角）+ 比例容差 0.05pp（严格）/ 相对 3.5%
（分母口径容差）。

**结果**: 20/20 股端点全覆盖；200 行 → ok 86 + basis_diff 2 + ratio_diff 40 +
list_diff 63 + em_no_data 9。逐类归因：

1. **ratio_diff（40 行，同名不同比例）**：两型——
   a. 系统性分母差：sina 比例分母=流通股本（页面时点）、EM=报告期总股本，湖内系统性
      偏高 3%~27%（如 sz.002978 成都紫东 46.04 vs 36.02）——口径差，非质量缺陷；
   b. 湖内陈旧：个别股东 EM 值 3~4× 于湖内（sz.002182 梅小明 12.34 vs 3.41、
      sz.002775 李从文 12.82 vs 3.55），湖内该股东两期同值=新浪页未更新。
2. **list_diff（63 行，名单不一致）**：多为湖内**少一名/多一名**的名单漂移
   （如 sz.300163 湖内缺卢先锋、名单整体后移一位，共有股东比例两源一致）+ 湖内
   整行陈旧。
3. **陈旧实锤（sz.002978）**：湖内 2026-09-30 行与 2026-06-30 行**逐位相同**
   （46.04/9.21/7.04/1.75/1.72 全同）→ 新浪 F10 页 Q3 未更新、湖内按页标注期日
   落了陈旧数据；EM 为真实 Q3 值（罗阳勇 9.21%→28.81% 增持）。**这正是备源的
   存在价值——EM 可暴露主源陈旧行。**
4. 7/20 股 10/10 全 ok（湖内新鲜时两源几乎完全一致）→ EM 数据质量与主源同级。

**判定**: ✅ **收为备源**。source_pool 角色=`备源(2026-11-27 实测通过；限最新一期+
分母口径差)`。限制（备源启用灌数前必须处理）：
- **仅返最新一期**（sdgd 无历史期查询能力 → 不能补历史期，只能恢复最新期；PIT 弱项）；
- **分母口径差**：比例入库前须归一湖内口径（流通股本分母），否则与存量行基准不一。

## 4. 代码改动清单（本次 commit）

| 文件 | 改动 |
|---|---|
| `lake/ingest/source_pool.py` | SOURCE_CAPABILITIES：sina T2 晋主（观察期条款终止）、sina T2 adj 改「推导(手工路径)」；tencent 删 T1 条目 + T7 改唯一主源；tdx 删 T7 条目；eastmoney T2/T3 降备（批量快速通道）+ T6 改实测收编 + 删 T9 条目；fuyao T6 条目移除（能力表清空） |
| `lake/config.py` | source_priority：T2 ohlcv [sina,tencent,tdx]→[sina,tdx]（tencent 退出逐只链）；T7 [tencent,tdx]+amount[tdx]→[tencent]（观察期终止）；tdx_enabled 注释更新 |
| `scripts/lake_backfill.py` | 注释同步：v7.0.33 通道块（EM=快速通道/备源角色，sina=主源保底；通道序≠角色序）、run_t7 tdx amount 补填=质量门、incremental/p0 docstring 链路描述 |
| `scripts/run_lake_sync_daily.sh` | T4 注释：EM 单族主通道（tushare 抽样校验随 11-26 弃用移除） |
| `scripts/t6_em_verify.py` | 新增：T6 EM 备源验证脚本（只读，可复跑；`--n/--seed/--out`） |
| `tests/test_lake_v613_source_pool.py` | 断言同步：sina T2 晋主角色、tencent 无 T1、tdx 无 T7、EM T2/T3 备源 + T6 实测角色、EM 无 T9、fuyao 能力表空 |

**行为变更说明**：① T2 逐只链 tencent 退出（sina 失败→直接 tdx fallback，不再经
tencent——tencent 9,363 行为历史残留、声明中从未列 T2）；② T7 tdx fallback 退出
（tencent 不可用时不再有 tdx 指数兜底——tdx 观察期 17 天 0 写入，兜底从未生效过）；
③ T2 日更通道序**不变**（EM 批量先试加速、失败回退 sina 逐只——8 日 502 期间的
兜底形态就是主源保底形态，不重构已验证的 v7.0.33 管道）。

## 5. 遗留项（开放，待 Joel 拍板或后续排期）

1. **T6 湖内陈旧行**（本次验证暴露）：至少 sz.002978 整行陈旧（09-30=06-30 复制）+
   多个个股别股东未更新（梅小明/李从文型）。修复=对陈旧股重取（EM 最新期可直接覆盖，
   但须先解决分母口径归一）→ **建议单独立项**，不在本次清理范围。
2. **EM T2 晋主观察窗**：11-11..11-25 连续 8 日 502 限频 → 现降备；连续稳定 2-4 周
   后评估再晋主（届时改 source_pool 角色 + config 注释 + 测试三处）。
3. **fuyao 现为无角色展示源**：/status 面板仍探活（_probe_fuyao）+ lake_observation
   仍跑 Fuyao PIT（T5/T6 均已单源/主备定案，PIT 对标对象已变）——是否停掉 fuyao
   探活/观察调用省配额，待拍板。
4. **T9 em_local_static 6,187 行冻结数据**：source 值保留（历史基线+对账），不占
   声明；如未来 T9 需要历史重灌再议。
5. **T1 baostock 340 + tushare 6 行历史残留**（数据级）：与源池声明无关，可另议清洗。
