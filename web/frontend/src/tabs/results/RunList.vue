// 左栏运行记录（移植 app.js loadRuns：liveQuery('runs')，failed 红 badge，默认选中最新）。
// 列表项文案逐字对齐旧版：失败 → "✗ 运行失败"；成功 → "入选 N · M 候选" + KPI 后缀。
<script setup>
import { computed, watch } from "vue";
import { api } from "../../api/client.js";
import { useLiveQuery } from "../../live/useLiveQuery.js";
import { fmtNum } from "../../utils/fmt.js";

const props = defineProps({ selected: { type: String, default: null } });
const emit = defineEmits(["select"]);

// 报告 §2.1：GET /api/runs → 事件触发+TTL30s（run 完成 invalidate；切页 stale-while-revalidate）
const { data, loading, error } = useLiveQuery("runs", () => api("/api/runs"), { ttl: 30000 });

const runs = computed(() => (data.value && data.value.runs) || []);

// 默认选中最新（移植 loadRuns 尾部：列表加载完成后无 active → selectRun(runsData[0].date)）。
// watch(immediate) 覆盖两种时序：数据已缓存（挂载即有）/ 首拉异步到达。用户手动选择后保持。
watch(
  runs,
  (list) => { if (!props.selected && list.length) emit("select", list[0].date); },
  { immediate: true }
);

function metaOf(r) {
  const isFailed = r.status === "failed";
  if (isFailed) return "✗ 运行失败";
  let meta = `入选 ${r.selected_count} · ${r.total_candidates} 候选`;
  const kpiMeta = !isFailed && (r.avg_ttm_yield_pct != null || r.avg_roe_pct != null)
    ? ` · TTM息率 ${fmtNum(r.avg_ttm_yield_pct, 2)}% · ROE ${fmtNum(r.avg_roe_pct, 1)}%` : "";
  return meta + kpiMeta;
}
</script>

<template>
  <ul class="run-list">
    <li v-if="loading && !runs.length" class="muted">加载中…</li>
    <li v-else-if="error" class="msg-err">加载失败: {{ error.message }}</li>
    <li v-else-if="!runs.length" class="muted">暂无运行结果</li>
    <li
      v-for="r in runs"
      :key="r.date"
      :class="{ active: r.date === selected }"
      @click="emit('select', r.date)"
    >
      <span class="r-date">{{ r.date }}</span>
      <template v-if="r.status === 'failed'">
        <span class="run-badge run-badge-failed">失败</span>
        <span class="r-meta r-meta-failed">{{ metaOf(r) }}</span>
      </template>
      <span v-else class="r-meta">{{ metaOf(r) }}</span>
    </li>
  </ul>
</template>
