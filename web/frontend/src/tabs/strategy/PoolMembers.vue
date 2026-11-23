// 股票池全集列表（v8.2.0 补，Joel 2026-11-23：「基础池一直在，其中若干只
// 轮番上阵候选」）。
// - 数据：GET /api/pool —— 展示全集 = 基础池（81）全部 + 不在基础池的候选
//   （手动池可含池外码）；服务端已排序：今日候选在前（候选池序 = 10 年回测
//   降序），非候选按基础池序；每行 is_candidate = 今日候选（G7~G9∪手动）。
// - 候选行浅绿高亮置顶；基础池行常规底色。
// - 展示全部成员；SimpleTable 自带搜索 + 点表头排序。
// - 候选池 ≠ 买入信号：入选 ≠ 该买；买不买看价格是否到策略触发线
//   （「我的持仓」页信号 / 微信提醒，同一口径）。
// - 手动添加/移除后 ManualPoolBar 会 invalidate("pool") 刷新本表。
// - 防御：加载失败 → 显示一行错误（不拖累策略页主体）。
<script setup>
import { computed, ref } from "vue";
import { api } from "../../api/client.js";
import { useLiveQuery } from "../../live/useLiveQuery.js";
import SimpleTable from "../../components/SimpleTable.vue";
// v8.2.2（Joel 2026-11-23：「点击股票池行 → 弹窗展示 K 线图和基础信息」）：
// SimpleTable 自带 row-click（行 .clickable）但此前无人监听 → 点击无反应。
// 现挂 Modal + StockPanoramaCard（湖页签同件：日K candlestick + 基础/估值/
// 因子/分红/季报/前十大股东；组件自取 /api/lake/stock + /api/lake/kline，
// TTL60s；关闭 → v-if 卸载 → 图表 dispose + 订阅自动丢弃）。
import Modal from "../../components/Modal.vue";
import StockPanoramaCard from "../lake/StockPanoramaCard.vue";

const { data, loading, error } = useLiveQuery("pool", () => api("/api/pool"), {});
// v8.2.2：弹窗目标行（null = 关闭；保留整行供标题展示 name+code）
const selected = ref(null);
const selectedTitle = computed(() => (selected.value
  ? `${selected.value.name || ""} ${selected.value.ts_code} · 个股全景`
  : ""));

// v8.0.11：展示层格式化（引擎/预计算产物全精度，只在此截断）——
// 顺修「总市值(亿) 2165.50348228 小数太多」（Joel 2026-11-12）。
// 服务端 bt_*_pct/share 已是百分数数值（44.04 = 44.04%）→ fmtPct2 加 % 号；
// None（总盈亏≤0 / 旧预计算表无占比列）→ SimpleTable 显示 —。
const fmt2 = (v) => {
  const n = typeof v === "number" ? v : parseFloat(v);
  return isNaN(n) ? String(v) : n.toFixed(2);
};
// v8.2.0 补（Joel 2026-11-20 全局格式统一）：百分比 1 位小数
const fmtPct2 = (v) => {
  const n = typeof v === "number" ? v : parseFloat(v);
  return isNaN(n) ? String(v) : n.toFixed(1) + "%";
};

const funnelLine = computed(() => {
  const f = data.value && data.value.funnel;
  if (!f) return "";
  return `全市场 ${f.total} → 基础池 ${f.div} → 十年回测≥门槛 ${f.backtest} → 股息率≥门槛 ${f.yield} → 价格区间 ${f.price} → 候选池 ${f.final}（含手动 ${f.manual}）`;
});

const rows = computed(() => {
  const ms = (data.value && data.value.members) || [];
  return ms.map((m) => ({
    ...m,
    // v8.2.0 补：状态列（替代旧「来源」手动/自动二分）
    status: m.is_candidate
      ? (m.is_manual ? "候选·手动" : "候选")
      : (m.is_manual ? "手动" : "基础池"),
  }));
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
  // v8.0.12（Joel：余额 2301 vs 股息率 4.12% 对不上 → 加累计分红透明展示；
  // 余额=累计−再投，占比口径不变）
  ["bt_dividends", "累计分红", true, fmt2],
  ["bt_price_pnl", "价差盈亏", true, fmt2],
  ["ttm_yield_pct", "股息率%", true, fmtPct2],
  ["price_latest", "最新价", true, fmt2],
  // v8.2.1（Joel 2026-11-23：「股票池里添加一列5年价格分位」）：
  // 服务端已 round 2（0-100，同 selection_timing 口径）→ 裸显示，null → —
  ["pctile_window", "5年分位", true],
  ["total_mv", "总市值(亿)", true, fmt2],
  ["status", "状态"],
];
</script>

<template>
  <div class="pm">
    <div class="pm-head">
      <span class="pm-title">股票池（{{ data ? data.n_base : "…" }}）· 今日候选（{{ data ? data.n_candidates : "…" }}）</span>
      <span v-if="loading" class="pm-muted">加载中…</span>
      <span v-if="funnelLine" class="pm-funnel">{{ funnelLine }}</span>
    </div>
    <p class="pm-note">
      列表展示全部基础池，今日候选（浅绿行）置顶；候选 ≠ 买入信号——是否买入看价格是否到策略触发线（「我的持仓」页信号 / 微信提醒）。
    </p>
    <p v-if="error" class="pm-err">股票池加载失败：{{ error }}</p>
    <SimpleTable
      v-else
      :rows="rows"
      :cols="COLS"
      :row-class="(r) => (r.is_candidate ? 'row-candidate' : '')"
      empty="（基础池为空——等待每日选股预计算，或下方手动添加）"
      @row-click="(r) => (selected = r)"
    />
    <!-- v8.2.2：行点击弹窗——个股全景（K线 + 基础信息，湖页签同件） -->
    <Modal :open="!!selected" :title="selectedTitle" @close="selected = null">
      <StockPanoramaCard v-if="selected" :code="selected.ts_code" />
    </Modal>
  </div>
</template>

<style scoped>
.pm { margin-top: 12px; padding-top: 10px; border-top: 1px dashed var(--line); }
.pm-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.pm-title { font-size: 14px; font-weight: 600; }
.pm-muted { color: var(--muted); font-size: 12px; }
.pm-funnel { color: var(--muted); font-size: 12px; }
.pm-note { color: var(--muted); font-size: 12px; margin: 4px 0 8px; }
.pm-err { color: var(--bad); font-size: 13px; margin: 6px 0; }
/* v8.2.0 补：今日候选行浅绿高亮（:deep 穿透 SimpleTable 作用域） */
.pm :deep(tr.row-candidate) { background: #eefaf0; }
.pm :deep(tr.row-candidate td) { border-top-color: #d3f0da; }
.pm :deep(tr.row-candidate):hover { background: #e2f6e6; }
</style>
