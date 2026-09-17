// 运行 tab（报告 §3 T-运行）：RunForm + TaskProgress。
// SSE/轮询逻辑全部在 taskStore（跨页签核心：切走再切回任务状态仍在）。
<script setup>
import RunForm from "./run/RunForm.vue";
import TaskProgress from "./run/TaskProgress.vue";
</script>

<template>
  <section class="tab-panel active">
    <RunForm />
    <TaskProgress />
  </section>
</template>
