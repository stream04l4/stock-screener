// 运行 tab（lake-source brief E 节：运行页+结果页合并为单一"运行"tab）。
// 上下结构：上 = RunForm + TaskProgress（原 RunTab），下 = ResultsTab 整块搬入
// （左 RunList | 右 RunDetail，内部零改动复用——queryRegistry 'runs'/'run:${day}'
// 缓存键、taskStore SSE 链路、StockModal open-stock emit 全部保持）。
// SSE/轮询逻辑全部在 taskStore（跨页签核心：切走再切回任务状态仍在）。
<script setup>
import RunForm from "./run/RunForm.vue";
import TaskProgress from "./run/TaskProgress.vue";
import ResultsTab from "./results/ResultsTab.vue";

const emit = defineEmits(["open-stock"]);
</script>

<template>
  <section class="tab-panel active">
    <RunForm />
    <TaskProgress />
    <div class="run-results-divider" role="separator"></div>
    <ResultsTab @open-stock="(code, day) => emit('open-stock', code, day)" />
  </section>
</template>
