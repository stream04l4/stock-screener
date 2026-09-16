// 数据湖 tab —— D1 占位（D3 批次实现：全量 /api/lake/* + lakeStore 自适应轮询，报告 §3 T-数据湖）。
<template>
  <section class="tab-panel active">
    <div class="card">
      <h3>数据湖</h3>
      <p class="muted small">独立分析层（DuckDB）：/api/lake/*。</p>
      <p class="placeholder">← 搜索/个股全景/K线/全市场浏览/状态与同步控制（GET /api/lake/*）在 D3 批次迁移</p>
    </div>
  </section>
</template>
