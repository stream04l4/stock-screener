// 任务进度面板（移植 index.html #run-progress + app.js showProgress/updateProgressBar/appendLogLine）。
// **只读绑定 taskStore**——SSE/轮询逻辑全在 store（跨页签核心：组件销毁不影响监控）。
// - state badge：running→"运行中…" / done→"已完成" / failed→"失败"（badge 类名与旧版一致）；
// - progress bar：width=pct，title=`${stage}: ${done}/${total}`，label=进度 [stage] done/total（p%）；
// - log-tail：logTail join("\n")，新行到达自动滚底（watch logTail 长度）。
<script setup>
import { computed, ref, watch } from "vue";
import { useTaskStore } from "../../stores/taskStore.js";

const store = useTaskStore();
const logEl = ref(null);

// 旧版 showProgress：state=running→"运行中…"、done→"已完成"、failed→"失败"
const badgeText = computed(() =>
  store.state === "running" ? "运行中…" : store.state === "done" ? "已完成" : "失败"
);

// 旧版 updateProgressBar：bar.parentElement.title = `${stage}: ${done}/${total}`
const trackTitle = computed(
  () => `${store.progress.stage}: ${store.progress.done}/${store.progress.total}`
);

// 新日志行到达 → 滚底（旧版 appendLogLine：lt.scrollTop = lt.scrollHeight）
watch(
  () => store.logTail.length,
  () => {
    if (logEl.value) logEl.value.scrollTop = logEl.value.scrollHeight;
  }
);
</script>

<template>
  <div v-if="store.hasTask" id="run-progress" class="card">
    <h3>任务状态 <span :id="'task-state-badge'" class="badge" :class="store.state">{{ badgeText }}</span></h3>
    <div id="task-meta" class="muted small">{{ store.taskMeta }}</div>
    <div class="progress-track" :title="trackTitle">
      <div id="task-progress-bar" class="progress-bar" :style="{ width: store.pct.toFixed(1) + '%' }"></div>
    </div>
    <div id="task-progress-label" class="muted small">{{ store.progressLabel }}</div>
    <pre id="log-tail" ref="logEl" class="log-tail">{{ store.logTail.join("\n") }}</pre>
  </div>
</template>
