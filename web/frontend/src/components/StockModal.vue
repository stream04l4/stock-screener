// 个股弹框（移植 app.js openStockModal/closeStockModal/initStockModal）。
// ECharts = npm 依赖（锁 5.6.0）动态 import → Vite 独立 chunk（替代旧 vendor 注入 hack，报告 §3）。
// - 雷达图：四维 score_* max=3，缺失维度 → 0，值夹在 [-3,3]；
// - K线折线：close_af1 + MA20/MA60 + dataZoom（inside + slider）；
// - 关闭 dispose 全部实例（防内存泄漏）；Esc / 遮罩关闭（Modal.vue）。
<script setup>
import { onBeforeUnmount, ref, watch } from "vue";
import Modal from "./Modal.vue";
import { api } from "../api/client.js";
import { fmtNum } from "../utils/fmt.js";

const props = defineProps({
  code: { type: String, default: null }, // null = 关闭
  runDay: { type: String, default: null },
});
const emit = defineEmits(["close"]);

const bodyState = ref("loading"); // loading / error / ready
const errMsg = ref("");
const chartErr = ref("");
const d = ref(null); // /api/stocks/{code}/detail 响应
let charts = []; // 打开中的 ECharts 实例（关闭时 dispose，防内存泄漏）

function disposeAll() {
  for (const c of charts) { try { c.dispose(); } catch { /* ignore */ } }
  charts = [];
}

// 因子/得分明细表行（移植：factors fmtNum(3) + scores fmtNum(4)，顺序 factors→scores）
const factorRows = () => {
  const rows = [];
  if (!d.value) return rows;
  for (const k of Object.keys(d.value.factors || {})) {
    rows.push([k, d.value.factors[k] == null ? "—" : fmtNum(d.value.factors[k], 3)]);
  }
  for (const k of Object.keys(d.value.scores || {})) {
    rows.push([k, d.value.scores[k] == null ? "—" : fmtNum(d.value.scores[k], 4)]);
  }
  return rows;
};

async function load() {
  disposeAll();
  d.value = null;
  errMsg.value = "";
  if (!props.runDay) {
    bodyState.value = "error";
    errMsg.value = "未选择运行日期，无法定位结果行。";
    return;
  }
  bodyState.value = "loading";
  try {
    d.value = await api(`/api/stocks/${props.code}/detail?run_day=${encodeURIComponent(props.runDay)}`);
    bodyState.value = "ready";
  } catch (e) {
    bodyState.value = "error";
    errMsg.value = "加载失败: " + e.message;
    return;
  }
  // ---- ECharts：雷达图 + K线趋势图（动态 import 独立 chunk）----
  try {
    const mod = await import("echarts");
    // Vite/rolldown 的 CJS-ESM interop 下 default 可能缺失 → 命名空间兜底（两种形态都兼容）
    const echarts = mod.default ?? mod;

    // 雷达图：四维得分（score_*；缺失维度 → 0，夹 [-3,3]）
    const dims = [
      ["technical", "技术面"], ["dividend", "股息"],
      ["industry", "行业"], ["fundamental", "基本面"],
    ];
    const radarEl = document.getElementById("stock-radar-chart");
    if (radarEl) {
      const radarChart = echarts.init(radarEl);
      radarChart.setOption({
        tooltip: {},
        radar: {
          indicator: dims.map(([, label]) => ({ name: label, max: 3 })),
          radius: "65%",
        },
        series: [{
          type: "radar",
          data: [{
            value: dims.map(([k]) => {
              const v = d.value.scores && d.value.scores["score_" + k];
              return v == null ? 0 : Math.max(-3, Math.min(3, Number(v)));
            }),
            name: props.code,
          }],
        }],
      });
      charts.push(radarChart);
    }

    // K线趋势图：close_af1 + MA20/MA60（本地缓存重建，离线可用）
    const kl = d.value.kline || {};
    if (kl.dates && kl.dates.length) {
      const kEl = document.getElementById("stock-kline-chart");
      if (kEl) {
        const kChart = echarts.init(kEl);
        kChart.setOption({
          tooltip: { trigger: "axis" },
          legend: { data: ["收盘(af1)", "MA20", "MA60"] },
          grid: { left: 50, right: 20, top: 30, bottom: 60 },
          xAxis: { type: "category", data: kl.dates },
          yAxis: { type: "value", scale: true },
          dataZoom: [{ type: "inside" }, { type: "slider", height: 18, bottom: 12 }],
          series: [
            { name: "收盘(af1)", type: "line", data: kl.close_af1, showSymbol: false, connectNulls: false, lineStyle: { width: 1.5 } },
            { name: "MA20", type: "line", data: kl.ma20, showSymbol: false, connectNulls: false },
            { name: "MA60", type: "line", data: kl.ma60, showSymbol: false, connectNulls: false },
          ],
        });
        charts.push(kChart);
      }
    }
  } catch (e) {
    // 图表加载失败（如 echarts chunk 拉取失败）→ 与旧版一致：明细表已渲染，追加错误提示
    chartErr.value = "图表加载失败: " + e.message;
  }
}

watch(
  () => props.code,
  (c) => { if (c) { chartErr.value = ""; load(); } else disposeAll(); },
  { immediate: true }
);
onBeforeUnmount(disposeAll);
</script>

<template>
  <Modal :open="!!code" :title="code ? code + ' · 个股明细' : ''" @close="emit('close')">
    <p v-if="bodyState === 'loading'" class="placeholder">加载本地缓存数据…</p>
    <p v-else-if="bodyState === 'error'" class="msg-err">{{ errMsg }}</p>
    <template v-else-if="bodyState === 'ready' && d">
      <p v-if="d.name || d.industry" class="small muted">
        {{ d.name || "" }} · {{ d.industry || "无行业" }} · 运行日 {{ runDay }}
      </p>
      <template v-if="factorRows().length">
        <h4>四维原始因子值</h4>
        <div class="tbl-wrap">
          <table class="data">
            <thead><tr><th>字段</th><th>值</th></tr></thead>
            <tbody>
              <tr v-for="(r, i) in factorRows()" :key="i"><td>{{ r[0] }}</td><td>{{ r[1] }}</td></tr>
            </tbody>
          </table>
        </div>
      </template>
      <h4>四维得分雷达图</h4>
      <div id="stock-radar-chart" class="chart-box"></div>
      <template v-if="d.kline && d.kline.dates && d.kline.dates.length">
        <h4>K线趋势（后复权重建，{{ d.kline.dates.length }} 根，截至 {{ runDay }}）</h4>
        <div id="stock-kline-chart" class="chart-box chart-kline"></div>
      </template>
      <p v-else-if="d.kline && d.kline.error" class="msg-err">{{ d.kline.error }}</p>
      <p v-else class="muted small">本地缓存无该股票K线（未迁移/新股）。</p>
      <p v-if="chartErr" class="msg-err">{{ chartErr }}</p>
    </template>
  </Modal>
</template>
