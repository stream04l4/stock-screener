// 候选股票池成员列表（v7.0.12，策略页签「股票池」卡片内）。
// - 数据：GET /api/pool（候选池 = 基础池 ∩ 十年回测≥门槛 ∩ 股息率≥门槛
//   ∩ 价格区间 ∪ 手动池；读预计算表 O(1)，服务端已按 10 年回测降序）。
// - 展示全部候选股（v8.0.8 起 = 全行业高息央国企，控股强度门保证无伪国企）；
//   SimpleTable 自带搜索 + 点表头排序。
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

// v8.0.11：展示层格式化（引擎/预计算产物全精度，只在此截断）——
// 顺修「总市值(亿) 2165.50348228 小数太多」（Joel 2026-11-12）。
// 服务端 bt_*_pct/share 已是百分数数值（44.04 = 44.04%）→ fmtPct2 加 % 号；
// None（总盈亏≤0 / 旧预计算表无占比列）→ SimpleTable 显示 —。
const fmt2 = (v) => {
  const n = typeof v === "number" ? v : parseFloat(v);
  return isNaN(n) ? String(v) : n.toFixed(2);
};
const fmtPct2 = (v) => {
  const n = typeof v === "number" ? v : parseFloat(v);
  return isNaN(n) ? String(v) : n.toFixed(2) + "%";
};

const funnelLine = computed(() => {
  const f = data.value && data.value.funnel;
  if (!f) return "";
  return `全市场 ${f.total} → 基础池 ${f.div} → 十年回测≥门槛 ${f.backtest} → 股息率≥门槛 ${f.yield} → 价格区间 ${f.price} → 候选池 ${f.final}（含手动 ${f.manual}）`;
});

const rows = computed(() => {
  const ms = (data.value && data.value.members) || [];
  return ms.map((m) => ({ ...m, src: m.is_manual ? "手动" : "自动" }));
});

const COLS = [
  ["ts_code", "代码"],
  ["name", "名称"],
  ["bt_pnl_pct", "10年回测%", true, fmtPct2],
  // v8.0.11（Joel：低波红利策略——分红占比更大更符合预期）：
  // 十年回测的分红占比 vs 上涨占比拆解（口径同回测 per_stock 表/summary 拆分行）
  ["bt_div_share", "分红占比", true, fmtPct2],
  ["bt_price_share", "上涨占比", true, fmtPct2],
  ["bt_div_pool", "分红余额", true, fmt2],
  ["bt_price_pnl", "价差盈亏", true, fmt2],
  ["ttm_yield_pct", "股息率%", true, fmtPct2],
  ["price_latest", "最新价", true, fmt2],
  ["total_mv", "总市值(亿)", true, fmt2],
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
