// 运行 tab —— D1 占位（D2 批次实现：taskStore + SSE 全局化，报告 §3 T-运行）。
<template>
  <section class="tab-panel active">
    <div class="run-form card">
      <h3>触发新筛选</h3>
      <p class="muted small">v2 增量缓存：命中稳定键时秒级完成；新股/新除权事件按需拉取。同一时间只允许一个运行任务。</p>
      <p class="placeholder">← 运行表单 + SSE 实时日志（POST /api/runs, /api/runs/{id}/events）在 D2 批次迁移</p>
    </div>
  </section>
</template>
