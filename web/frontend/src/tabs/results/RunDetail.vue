// 运行详情（移植 app.js selectRun + renderRunDetail；liveQuery(`run:${day}`)）。
// - status=failed → FailedBanner（不渲染其余区块）；
// - data_health.has_anomaly → HealthAnomalyCard（严格区分"0入选"——那种 status=ok 正常渲染）；
// - KPI 卡（v2 报告才有；旧运行 kpi 全空 → 不显示）→ 漏斗卡 → 入选表 + 候选表 → 缺失与异常 → 报告原文。
<script setup>
import { computed } from "vue";
import { api } from "../../api/client.js";
import { useLiveQuery } from "../../live/useLiveQuery.js";
import FailedBanner from "./FailedBanner.vue";
import HealthAnomalyCard from "./HealthAnomalyCard.vue";
import KpiCards from "./KpiCards.vue";
import FunnelBars from "./FunnelBars.vue";
import DataTable from "../../components/DataTable.vue";
import MissingNotes from "./MissingNotes.vue";
import MarkdownView from "../../components/MarkdownView.vue";

const props = defineProps({ day: { type: String, required: true } });
const emit = defineEmits(["open-stock"]);

// 报告 §2.1：GET /api/runs/{day} → 按需+会话缓存（键 day）；无 ttl，同 key 重进不重发。
const { data, loading, error } = useLiveQuery(
  () => `run:${props.day}`,
  () => api("/api/runs/" + props.day)
);

// 入选股票表（v2：top_n_selected=1；legacy：pass_all=是）——移植 renderRunDetail 的 selRows 逻辑
const selRows = computed(() => {
  const d = data.value;
  if (!d) return [];
  return (d.selected && d.selected.length) ? d.selected
    : (d.survivors || []).filter((r) => String(r.top_n_selected ?? "").trim() === "1");
});

// KPI 卡显示条件（移植：kpi && (selected != null || avg_ttm_yield_pct != null)）
const showKpi = computed(() => {
  const k = data.value && data.value.kpi;
  return !!(k && (k.selected != null || k.avg_ttm_yield_pct != null));
});
</script>

<template>
  <div v-if="loading && !data" class="run-detail-inner">
    <p class="placeholder">加载中…</p>
  </div>
  <div v-else-if="error" class="run-detail-inner">
    <p class="msg-err">加载失败: {{ error.message }}</p>
  </div>
  <template v-else-if="data">
    <!-- 失败运行：只渲染红色横幅（移植 renderRunDetail 的 early return） -->
    <FailedBanner v-if="data.status === 'failed'" :d="data" />
    <template v-else>
      <HealthAnomalyCard :d="data" />
      <KpiCards v-if="showKpi" :kpi="data.kpi" />
      <div class="card">
        <h3>运行 {{ data.date }}<span class="muted small"> · 生成于 {{ data.generated_at }}</span></h3>
        <FunnelBars :funnel="data.funnel || []" />
      </div>
      <div class="card">
        <h3>最终入选（{{ selRows.length }} 只，按综合得分/CSV 顺序）</h3>
        <DataTable :rows="selRows" :badges="data.badges" @row-click="(code) => emit('open-stock', code, data.date)" />
      </div>
      <div class="card">
        <h3>全部候选（{{ (data.survivors || []).length }} 行，可搜索 / 点表头排序；点行查看个股雷达图+K线）</h3>
        <DataTable :rows="data.survivors || []" :badges="data.badges" @row-click="(code) => emit('open-stock', code, data.date)" />
      </div>
      <MissingNotes :d="data" />
      <div class="card">
        <h3>报告 Markdown 原文</h3>
        <MarkdownView v-if="data.report_md" :md="data.report_md" />
        <p v-else class="muted">无报告文件</p>
      </div>
    </template>
  </template>
</template>
