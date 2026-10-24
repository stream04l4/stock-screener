// 候选股票池成员列表（v7.0.12，策略页签「股票池」卡片内）。
// - 数据：GET /api/pool（候选池 = 基础 5 条件 ∩ 十年回测为正 ∩ 股息率≥3%
//   ∩ 价格 3~100 元 ∪ 手动池；读预计算表 O(1)，服务端已按 10 年回测降序）。
// - 展示全部候选股（2026-10-24 生产 = 59 只）；SimpleTable 自带搜索 + 点表头排序。
// - 候选池 ≠ 买入信号：入选 ≠ 该买；买不买看价格是否到策略触发线
//   （「我的持仓」页信号 / 微信提醒，同一口径）。
// - 手动添加/移除后 ManualPoolBar 会 invalidate("pool") 刷新本表。
// - 防御：加载失败 → 显示一行错误（不拖累策略页主体）。
<script setup>
import { computed } from "vue";
import { api } from "../../api/client.js";
import { useLiveQuery } from "../../live/useLiveQuery.js";
import SimpleTable from "../../components/SimpleTable.vue";

const { data, loading, error } = useLiveQuery("pool", () => api("/api/pool"), {});

const funnelLine = computed(() => {
  const f = data.value && data.value.funnel;
  if (!f) return "";
  return `全市场 ${f.total} → 基础5条件 ${f.div} → 十年回测为正 ${f.backtest} → 股息率 ${f.yield} → 价格区间 ${f.price} → 候选池 ${f.final}（含手动 ${f.manual}）`;
});

const rows = computed(() => {
  const ms = (data.value && data.value.members) || [];
  return ms.map((m) => ({ ...m, src: m.is_manual ? "手动" : "自动" }));
});

const COLS = [
  ["ts_code", "代码"],
  ["name", "名称"],
  ["bt_pnl_pct", "10年回测%", true],
  ["ttm_yield_pct", "股息率%", true],
  ["price_latest", "最新价", true],
  ["total_mv", "总市值(亿)", true],
  ["src", "来源"],
];
</script>

<template>
  <div class="pm">
    <div class="pm-head">
      <span class="pm-title">候选股票（{{ data ? data.n_members : "…" }}）</span>
      <span v-if="loading" class="pm-muted">加载中…</span>
      <span v-if="funnelLine" class="pm-funnel">{{ funnelLine }}</span>
    </div>
    <p class="pm-note">
      入选 ≠ 买入信号：是否买入看价格是否到策略触发线（「我的持仓」页信号 / 微信提醒）。
    </p>
    <p v-if="error" class="pm-err">候选池加载失败：{{ error }}</p>
    <SimpleTable
      v-else
      :rows="rows"
      :cols="COLS"
      empty="（候选池为空——等待每日选股预计算，或下方手动添加）"
    />
  </div>
</template>

<style scoped>
.pm { margin-top: 12px; padding-top: 10px; border-top: 1px dashed var(--line, #d8d8e0); }
.pm-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.pm-title { font-size: 14px; font-weight: 600; }
.pm-muted { color: #999; font-size: 12px; }
.pm-funnel { color: #777; font-size: 12px; }
.pm-note { color: #888; font-size: 12px; margin: 4px 0 8px; }
.pm-err { color: #b42318; font-size: 13px; margin: 6px 0; }
</style>
