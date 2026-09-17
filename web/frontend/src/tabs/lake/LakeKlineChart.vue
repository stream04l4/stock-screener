<script setup>
// LakeKlineChart —— 个股日K线（**ECharts candlestick**，报告 §3 推荐方案；删 SVG 手绘实现）。
//
// - series=[candlestick 红涨绿跌(itemStyle --lake-up/down) + bar 成交量同色弱化]
//   + dataZoom inside+slider + tooltip 开高低收量（option 组装在 klineOption.js 纯函数，
//   单测 mock echarts.init 断言 option）。
// - 区间按钮 60/120/250/all → days 参数重取（emit range-change；数据由父组件经
//   useLiveQuery(`lake:kline:${ts}:${range}`) 提供——缓存键 ts_code+range，切股作废旧响应）。
// - 空态"该股暂无K线数据" / 409 灌数中占位（与 vanilla 文案一致，不白屏）。
import { computed, onMounted, onUnmounted, ref, watch } from "vue";

const props = defineProps({
  rows: { type: Array, default: () => [] },   // /kline rows（date 升序）
  tsCode: { type: String, default: "" },
  range: { type: String, default: "250" },
  adjust: { type: String, default: "qfq" },    // v6.1.2 P1-A：none|qfq|hfq（默认前复权）
  adjustNote: { type: String, default: "" },   // v6.1.2 P1-A：降级提示（adj 未灌完 → 后端带 note）
  loading: { type: Boolean, default: false },
  error: { type: Object, default: null },     // Error|null（409 backfill → 灌数中占位）
});
const emit = defineEmits(["range-change", "adjust-change"]);

// 区间按钮（与 vanilla LAKE_KLINE_RANGES 一致：60/120/250/all；"全部"= days=all）
const RANGES = [
  { key: "60", label: "60" },
  { key: "120", label: "120" },
  { key: "250", label: "250" },
  { key: "all", label: "全部" },
];
// v6.1.2 P1-A：复权三态（原始/前复权/后复权）——策略刚需：原始价在除权日有假跳空。
const ADJUSTS = [
  { key: "none", label: "原始" },
  { key: "qfq", label: "前复权(qfq)" },
  { key: "hfq", label: "后复权(hfq)" },
];

const chartEl = ref(null);
let chart = null; // ECharts 实例（keep-alive 保活；unmount dispose）

// 409 lake_backfill_in_progress → 灌数中占位（vanilla loadLakeKline catch 分支文案）
const isBackfillErr = computed(() => !!(props.error && props.error.status === 409
  && props.error.body && props.error.body.error === "lake_backfill_in_progress"));

const stateText = computed(() => {
  if (isBackfillErr.value) return "⏳ 数据灌入中，K线暂不可用——稍后刷新";
  if (props.error && !props.rows.length) return "K线加载失败——稍后刷新";
  if (!props.rows.length) return props.loading ? "加载中…" : "该股暂无K线数据";
  return "";
});

async function renderChart() {
  if (!chartEl.value || !props.rows.length) return;
  const mod = await import("echarts");
  const echarts = mod.default ?? mod;   // Vite/rolldown CJS-ESM interop（与 BacktestTab 同模式）
  if (!chart) chart = echarts.init(chartEl.value);
  const { buildKlineOption } = await import("./klineOption.js");
  chart.setOption(buildKlineOption(props.rows), true);   // notMerge：切区间/切股整体替换
  chart.resize();
}

watch(() => [props.rows, props.tsCode], () => { renderChart(); });

onMounted(() => { renderChart(); });
onUnmounted(() => {
  if (chart) { try { chart.dispose(); } catch { /* ignore */ } chart = null; }
});

function onRange(key) {
  if (key === props.range) return;
  emit("range-change", key);   // 切换即重取（父组件换 useLiveQuery key → 新请求）
}
function onAdjust(key) {
  if (key === props.adjust) return;
  emit("adjust-change", key);  // v6.1.2 P1-A：切复权态 → 父组件换 key（含 adjust）重取
}
</script>

<template>
  <div class="lake-kline-wrap">
    <div class="lake-kline-bar">
      <button v-for="r in RANGES" :key="r.key"
              class="lake-kline-range" :class="{ cur: r.key === range }" @click="onRange(r.key)">
        {{ r.label }}
      </button>
      <!-- v6.1.2 P1-A：复权三态切换（区间按钮组旁；默认前复权 qfq） -->
      <span class="lake-kline-sep"></span>
      <button v-for="a in ADJUSTS" :key="a.key" :id="'lake-adj-' + a.key"
              class="lake-kline-range lake-kline-adjust" :class="{ cur: a.key === adjust }" @click="onAdjust(a.key)">
        {{ a.label }}
      </button>
    </div>
    <!-- v6.1.2 P1-A：复权降级提示（adj_factor 未灌完 → 后端带 adjust_note，图表上方小字 muted） -->
    <p v-if="adjustNote" class="lake-kline-adjust-note muted small">{{ adjustNote }}</p>
    <p v-if="stateText" class="lake-kline-empty">{{ stateText }}</p>
    <div v-show="rows.length" ref="chartEl" id="lake-kline-chart" style="width:100%;height:420px;"></div>
  </div>
</template>
