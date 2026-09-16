// 数据源健康度异常卡（移植 app.js renderRunDetail 的 data_health.has_anomaly 分支）。
// v5.2 Phase 1：仅异常时显示；正常日/旧运行 data_health=null → 不渲染（零视觉回归）。
<script setup>
defineProps({ d: { type: Object, required: true } });
</script>

<template>
  <div v-if="d.data_health && d.data_health.has_anomaly" class="card health-anomaly-card">
    <h3><span class="run-badge run-badge-failed">数据源异常</span> · {{ d.date }}</h3>
    <p v-for="(a, i) in d.data_health.anomalies" :key="i" class="muted small">⚠️ {{ a }}</p>
    <p class="muted small">详见报告「数据源健康度」段（report_*.md）</p>
  </div>
</template>
