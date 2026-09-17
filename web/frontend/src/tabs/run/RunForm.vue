// 运行表单（移植 index.html #tab-run .run-form + app.js initRunPage/startRun）。
// date 默认今天；开始按钮 → taskStore.startRun（POST /api/runs）；
// 409 → toast"已有运行任务…"（store 内统一处理，文案与旧版逐字一致）；
// 其他失败 → toast + 恢复按钮。SSE/监控逻辑全部在 taskStore，本组件零 SSE 代码。
<script setup>
import { onMounted, ref } from "vue";
import { useTaskStore } from "../../stores/taskStore.js";

const store = useTaskStore();

function today() {
  return new Date().toISOString().slice(0, 10); // 旧版 initRunPage 一致（UTC 日期，原样移植）
}

const date = ref(today());
const starting = ref(false);

onMounted(() => { store.restore(); }); // 页面刷新后恢复活动任务监控（幂等）

async function onStart() {
  if (starting.value) return;
  starting.value = true;
  try {
    await store.startRun(date.value);
    // 成功后按钮由 store.runFinished 控制：禁用直到任务终态（旧版 btn.disabled=true，finishRun 恢复）
  } catch {
    starting.value = false; // 409/其他失败 → 恢复可点（旧版语义原样）
  }
}
</script>

<template>
  <div class="run-form card">
    <h3>触发新筛选</h3>
    <label for="run-date">筛选日期（YYYY-MM-DD，默认今天；非交易日自动回退到最近交易日）</label>
    <div class="run-row">
      <input type="date" id="run-date" v-model="date">
      <button id="btn-run-start" class="primary-btn" :disabled="starting || (store.hasTask && !store.runFinished)" @click="onStart">▶ 开始筛选</button>
    </div>
    <p class="muted small">v2 增量缓存：命中稳定键时秒级完成；新股/新除权事件按需拉取。同一时间只允许一个运行任务。</p>
  </div>
</template>
