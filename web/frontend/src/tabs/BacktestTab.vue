<script setup>
// 回测 tab（报告 §3 T-回测）：GET /api/backtest → ECharts line + metrics 行。
// 逐字移植 app.js loadBacktest（L86-128）语义：
// - 策略实线(width 2) + benchmarks 虚线(width 1 dashed)、dataZoom inside+slider(18)、
//   tooltip axis、legend、grid {left:50,right:20,top:40,bottom:60}、xAxis category、yAxis value scale；
// - metrics 行：窗口 start~end（n_days 交易日）· 全期年化/夏普/最大回撤 · 近1y 年化；
// - 404/加载失败 → "回测产物不存在：<msg>"（旧版对所有错误同文案，原样移植）；
// - 图表渲染失败 → "图表渲染失败：<msg>"。
// echarts = npm 依赖（锁 5.6.0），动态 import 独立 chunk（与 StockModal 同模式）。
// 报告 §2.1：GET /api/backtest → 按需+TTL60s（useLiveQuery refcount，切走页签即停）。
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { api } from "../api/client.js";
import { useLiveQuery } from "../live/useLiveQuery.js";
import { fmtNum } from "../utils/fmt.js";

const { data, error } = useLiveQuery("backtest", () => api("/api/backtest"), { ttl: 60000 });

const chartEl = ref(null);
let chart = null; // ECharts 实例（keep-alive 保活；unmount 时 dispose）
const chartError = ref(""); // 图表渲染失败提示（旧版 meta.textContent = "图表渲染失败：…"）

// 404/首载失败空态判定：仅"从未拿到数据"时显示（SWR 后台刷新失败 → 旧值照常展示，不串味）
const firstLoadError = computed(() => !!(error.value && !data.value));

// metrics 行（移植 loadBacktest 的 meta.textContent 拼接，字段以 API 返回为准）
const metricsText = computed(() => {
  const d = data.value;
  if (!d) return "";
  const m = d.metrics || {};
  const f = m.full || {};
  const s1 = (m.slices || {})["1y"] || {};
  return (
    `窗口 ${m.window ? m.window.start + " ~ " + m.window.end : "—"}（${m.window ? m.window.n_days : 0} 交易日）· ` +
    `全期年化 ${fmtNum(f.annual_return_pct, 2)}% / 夏普 ${fmtNum(f.sharpe, 2)} / 最大回撤 ${fmtNum(f.max_drawdown_pct, 2)}% · ` +
    `近1y 年化 ${fmtNum(s1.annual_return_pct, 2)}%`
  );
});

// 图表渲染（移植 loadBacktest try 块；setOption(..., true) notMerge）
async function renderChart(d) {
  if (!chartEl.value) return;
  const mod = await import("echarts");
  // Vite/rolldown CJS-ESM interop：echarts 无 .default（init 是命名导出）→ 命名空间兜底（与 StockModal 同模式）
  const echarts = mod.default ?? mod;
  if (!chart) chart = echarts.init(chartEl.value);
  const dates = d.equity_curve.map((r) => r.date);
  const series = [{ name: "策略", type: "line", showSymbol: false, data: d.equity_curve.map((r) => r.strategy), lineStyle: { width: 2 } }];
  for (const b of d.benchmarks || []) {
    series.push({ name: b, type: "line", showSymbol: false, data: d.equity_curve.map((r) => r[b]), lineStyle: { width: 1, type: "dashed" } });
  }
  chart.setOption({
    tooltip: { trigger: "axis" },
    legend: { data: series.map((s) => s.name) },
    grid: { left: 50, right: 20, top: 40, bottom: 60 },
    xAxis: { type: "category", data: dates },
    yAxis: { type: "value", scale: true },
    dataZoom: [{ type: "inside" }, { type: "slider", height: 18 }],
    series,
  }, true);
  chart.resize();
}

watch(data, async (d) => {
  if (!d || !d.equity_curve) return;
  chartError.value = "";
  try {
    await renderChart(d);
  } catch (e) {
    // 旧版：meta.textContent = "图表渲染失败：" + e.message（数据行保留，图表区清空）
    chartError.value = "图表渲染失败：" + e.message;
  }
});

onMounted(() => { /* 首帧由 watch(data) 驱动（SWR：缓存命中时 data 挂载即有值） */ });

onUnmounted(() => {
  if (chart) { try { chart.dispose(); } catch { /* ignore */ } chart = null; }
});
</script>

<template>
  <section class="tab-panel active">
    <div class="card">
      <h3>净值曲线（策略 vs 基准）</h3>
      <p class="muted small">数据源 output/backtest/（离线回测产物）；未跑回测时显示 404 提示。</p>
      <div v-if="firstLoadError" id="bt-metrics" class="msg-err">回测产物不存在：{{ error.message }}</div>
      <template v-else>
        <div ref="chartEl" id="bt-chart" style="width:100%;height:420px;"></div>
        <div id="bt-metrics" class="muted small">{{ chartError || metricsText }}</div>
      </template>
    </div>
  </section>
</template>
