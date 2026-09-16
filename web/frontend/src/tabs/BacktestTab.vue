// 回测 tab —— D1 占位（D2 批次实现：ECharts 净值曲线 + metrics，报告 §3 T-回测）。
<template>
  <section class="tab-panel active">
    <div class="card">
      <h3>净值曲线（策略 vs 基准）</h3>
      <p class="muted small">数据源 output/backtest/（离线回测产物）；未跑回测时显示 404 提示。</p>
      <p class="placeholder">← 回测图表（GET /api/backtest）在 D2 批次迁移</p>
    </div>
  </section>
</template>
