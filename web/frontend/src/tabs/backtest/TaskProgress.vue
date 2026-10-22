// 任务进度面板（v7.0：回测任务；只读绑定 taskStore——SSE/轮询逻辑全在 store）。
// - state badge：running→"回测中…" / done→"已完成" / failed→"失败"；
// - progress bar：width=pct，label=进度 [stage] done/total（p%）；
// - log-tail：logTail join("\n")，新行到达自动滚底。
<script setup>
import { computed, ref, watch } from "vue";
import { useTaskStore } from "../../stores/taskStore.js";

const store = useTaskStore();
const logEl = ref(null);

const badgeText = computed(() =>
  store.state === "running" ? "回测中…" : store.state === "done" ? "已完成" : "失败"
);

const trackTitle = computed(
  () => `${store.progress.stage || "—"}: ${store.progress.done}/${store.progress.total || "—"}`
);

// 新日志行到达 → 滚底
watch(
  () => store.logTail.length,
  () => {
    if (logEl.value) logEl.value.scrollTop = logEl.value.scrollHeight;
  }
);
</script>

<template>
  <div v-if="store.hasTask" id="bt-progress" class="card">
    <h3>回测任务 <span :id="'task-state-badge'" class="badge" :class="store.state">{{ badgeText }}</span></h3>
    <div id="task-meta" class="muted small">{{ store.taskMeta }}</div>
    <div class="progress-track" :title="trackTitle">
      <div id="task-progress-bar" class="progress-bar" :style="{ width: store.pct.toFixed(1) + '%' }"></div>
    </div>
    <div id="task-progress-label" class="muted small">{{ store.progressLabel }}</div>
    <pre id="log-tail" ref="logEl" class="log-tail">{{ store.logTail.join("\n") }}</pre>
  </div>
</template>
