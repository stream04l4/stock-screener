// 失败运行横幅（移植 app.js renderRunDetail 的 status==="failed" 分支）。
// 生产路径失败守卫：数据源级失败 → 顶部红色横幅 + 错误摘要，不渲染 KPI/漏斗/榜单；
// 与合法"0 只入选"严格区分——那种 status=ok，正常走空结果渲染。
<script setup>
defineProps({ d: { type: Object, required: true } });
</script>

<template>
  <div class="card run-failed-card">
    <h3><span class="run-badge run-badge-failed">运行失败</span> · {{ d.date }}</h3>
    <p class="muted small">该日筛选因数据源级失败中止，未产出结果（不是"0 只入选"）。</p>
    <pre class="msg-err">{{ (d.error_type ? d.error_type + ": " : "") + (d.error || "未知错误") }}</pre>
    <p class="muted small">失败时间: {{ d.generated_at || "—" }} · 主数据源 BaoStock（封禁/降级/空股票池等）</p>
  </div>
</template>
