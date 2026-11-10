// 回测 tab（v7.0）：参数表单（start/end/codes|池）+ 触发按钮 + TaskProgress（SSE 复用 taskStore）
// + 结果区（个股表 + 净值曲线 ECharts + 买入时间表 + summary.md）。
//
// 链路：POST /api/bt/runs（taskStore.startBtRun；409 → toast）→ SSE /api/bt/runs/{id}/events
// → done → GET /api/bt/results/{id}（taskStore.fetchResults，缓存 lastResults 供查询页复用）。
// echarts = npm 依赖（锁 5.6.0），动态 import 独立 chunk。
<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { api } from "../api/client.js";
import { useToastStore } from "../stores/toastStore.js";
import { useTaskStore } from "../stores/taskStore.js";
import SimpleTable from "../components/SimpleTable.vue";
import MarkdownView from "../components/MarkdownView.vue";
import TaskProgress from "./backtest/TaskProgress.vue";

const toast = useToastStore();
const store = useTaskStore();

// ---------------------------------------------------------------------------
// 参数表单
// ---------------------------------------------------------------------------
const form = ref({ start: "", end: "", mode: "pool", codes: "" });
const starting = ref(false);

// mode: "pool"（股票池，默认）| "codes"（指定标的，逗号分隔）
// 策略固定 = 当前策略 config/strategy.yaml（v7.0.29：策略库退役，不再可选快照）

async function onRun() {
  if (starting.value) return;
  starting.value = true;
  try {
    const params = {};
    if (form.value.start) params.start = form.value.start;
    if (form.value.end) params.end = form.value.end;
    if (form.value.mode === "codes") {
      const codes = form.value.codes.split(",").map((s) => s.trim()).filter(Boolean);
      if (!codes.length) { toast.toast("codes 为空：请输入 sh.601088,sz.000651 形式", false); return; }
      params.codes = codes.join(",");
    } else {
      params.pool = true;
    }
    await store.startBtRun(params);
  } catch (e) {
    // 409/参数 400 → taskStore 已 toast；这里不再重复
  } finally {
    starting.value = false;
  }
}

// ---------------------------------------------------------------------------
// 结果区（done 后拉一次 /api/bt/results/{id}）
// ---------------------------------------------------------------------------
const results = ref(null); // taskStore.lastResults.data
const resultsError = ref("");
const fetchedFor = ref(""); // 已拉取的 taskId（防重复拉取）

watch(
  () => [store.state, store.activeTaskId],
  async ([state, tid]) => {
    if (state !== "done" || !tid || fetchedFor.value === tid) return;
    fetchedFor.value = tid;
    resultsError.value = "";
    try {
      results.value = await store.fetchResults(tid);
      loadedFromHistory.value = false; // 新回测产物覆盖历史视图
    } catch (e) {
      resultsError.value = "结果加载失败：" + e.message;
    }
  },
  { immediate: true }
);

// ---------------------------------------------------------------------------
// 表格列（per_stock / buys）
// v7.0.33：数值展示收敛小数位（Joel 反馈「小数点后位数太多」）——引擎产物是
// 全精度 float（如 887013.0240999996 / 3.743816971766769），展示层截断：
// 金额/价格 2 位、百分比 2 位、日期列截 ISO 的 T 后部分。排序/搜索仍用原值。
// ---------------------------------------------------------------------------
const fmt2 = (v) => {
  const n = typeof v === "number" ? v : parseFloat(v);
  return isNaN(n) ? String(v) : n.toFixed(2);
};
const fmtDate = (v) => {
  const s = String(v);
  const i = s.indexOf("T");
  return i > 0 ? s.slice(0, i) : s;
};
const PER_STOCK_COLS = [
  ["ts_code", "代码"], ["name", "名称"], ["n_bars", "bar数", true],
  ["n_rounds", "轮次", true], ["n_rearms", "再武装", true], ["n_buys", "买入笔数", true],
  ["total_invested", "总投入", true, fmt2], ["final_assets", "期末资产", true, fmt2],
  ["dividends", "累计分红", true, fmt2], ["total_pnl", "总盈亏", true, fmt2],
  ["pnl_pct", "盈亏%", true, fmt2], ["min_unreal_pct", "最低浮盈%", true, fmt2],
  ["first_buy_date", "首买日", false, fmtDate], ["last_buy_date", "末买日", false, fmtDate],
  ["last_status", "末状态"], ["end_date", "期末", false, fmtDate],
];

const BUYS_COLS = [
  ["ts_code", "代码"], ["seq", "序", true], ["date", "日期", false, fmtDate],
  ["price", "成交价", true, fmt2], ["amount", "金额", true, fmt2],
];

// ---------------------------------------------------------------------------
// 净值曲线（equity_curve：date × open_value 组合持仓市值）
// ---------------------------------------------------------------------------
const chartEl = ref(null);
let chart = null;
const chartError = ref("");

async function renderChart(rows) {
  await nextTick(); // watch(flush pre) 触发时 v-if 结果区 DOM（含 chartEl ref）尚未挂载
  if (!chartEl.value || !rows.length) return;
  const mod = await import("echarts");
  // echarts v5 ESM 同时提供 named 与 default 导出；CJS-ESM interop 兜底
  const echarts = (mod && "default" in mod && mod.default) || mod;
  if (!chart) chart = echarts.init(chartEl.value);
  chart.setOption({
    tooltip: { trigger: "axis" },
    legend: { data: ["组合市值", "累计投入"] },
    grid: { left: 60, right: 20, top: 40, bottom: 60 },
    xAxis: { type: "category", data: rows.map((r) => r.date) },
    yAxis: { type: "value", scale: true },
    dataZoom: [{ type: "inside" }, { type: "slider", height: 18 }],
    series: [
      { name: "组合市值", type: "line", showSymbol: false, data: rows.map((r) => r.open_value), lineStyle: { width: 2 } },
      { name: "累计投入", type: "line", showSymbol: false, data: rows.map((r) => r.open_cost), lineStyle: { width: 1, type: "dashed" } },
    ],
  }, true);
  chart.resize();
}

watch(
  () => results.value && results.value.equity_curve,
  async (rows) => {
    if (!rows || !rows.length) return;
    chartError.value = "";
    try {
      await renderChart(rows);
    } catch (e) {
      chartError.value = "图表渲染失败：" + e.message;
    }
  },
  { immediate: true }
);

onUnmounted(() => {
  if (chart) { try { chart.dispose(); } catch { /* ignore */ } chart = null; }
});

// 元信息行（run_meta：窗口 / 标的数 / 耗时）
const metaText = computed(() => {
  const d = results.value;
  if (!d) return "";
  const m = d.run_meta || {};
  const w = m.window || {};
  return (
    (loadedFromHistory.value ? "【历史产物】" : "") +
    `窗口 ${w.start || "各股数据起点"} ~ ${w.end || "数据终点"} · ` +
    `标的 ${m.n_codes ?? (d.per_stock || []).length} 只 · ` +
    `策略 当前策略（strategy.yaml） · ` +
    (m.elapsed_s != null ? `耗时 ${m.elapsed_s}s · ` : "") +
    `产物 ${d.task_id}`
  );
});

// ---------------------------------------------------------------------------
// 历史回测结果（v7.0.34，Joel 2026-11-10）：GET /api/bt/runs 列表 + 选中加载
// GET /api/bt/results/{id} 填入同一结果区（与 SSE done 同形状，纯只读）。
// 不碰 taskStore（新回测完成仍按原链路覆盖结果区）。
// ---------------------------------------------------------------------------
const historyRuns = ref([]);
const historySel = ref("");
const historyErr = ref("");
const loadedFromHistory = ref(false);

function historyLabel(r) {
  const t = r.generated_at ? r.generated_at.replace("T", " ").slice(0, 16) : r.task_id;
  const w = `${r.window_start || "?"}~${r.window_end || "?"}`;
  const n = r.n_stocks != null ? `${r.n_stocks} 股` : "运行中/不完整";
  return `${t} · ${n} · ${w}`;
}

async function loadHistory() {
  historyErr.value = "";
  try {
    const d = await api("/api/bt/runs?limit=50");
    historyRuns.value = d.runs || [];
  } catch (e) {
    historyErr.value = "历史列表加载失败：" + e.message;
  }
}

async function onHistoryLoad() {
  const tid = historySel.value;
  if (!tid) return;
  historyErr.value = "";
  try {
    const d = await api("/api/bt/results/" + tid);
    results.value = d;
    loadedFromHistory.value = true;
    historySel.value = "";
    const el = document.querySelector(".bt-results");
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    historyErr.value = "历史结果加载失败：" + e.message;
  }
}

onMounted(loadHistory);
</script>

<template>
  <section class="tab-panel active">
    <!-- 参数表单 + 触发 -->
    <div class="card bt-form">
      <h3>🧪 按需回测</h3>
      <p class="muted small">
        窗口留空 = 各股数据起点 → 数据终点；标的默认全股票池（5 条件全链）。回测为异步子进程，进度经 SSE 实时推送。
      </p>
      <div class="bt-form-grid">
        <label class="f-row">
          <span>起始日</span>
          <input type="date" v-model="form.start" :disabled="starting || store.state === 'running'">
        </label>
        <label class="f-row">
          <span>结束日</span>
          <input type="date" v-model="form.end" :disabled="starting || store.state === 'running'">
        </label>
        <div class="f-row f-scope">
          <span>标的</span>
          <label class="radio"><input type="radio" value="pool" v-model="form.mode" :disabled="starting || store.state === 'running'"> 股票池（默认）</label>
          <label class="radio"><input type="radio" value="codes" v-model="form.mode" :disabled="starting || store.state === 'running'"> 指定标的</label>
        </div>
        <label class="f-row" v-if="form.mode === 'codes'">
          <span>代码</span>
          <input type="text" v-model="form.codes" placeholder="sh.601088,sz.000651（逗号分隔）"
                 :disabled="starting || store.state === 'running'">
        </label>
        <div class="f-row">
          <button id="btn-bt-run" class="primary-btn" :disabled="starting || store.state === 'running'"
                  @click="onRun">▶ 开始回测</button>
          <span class="muted small" v-if="store.state === 'running'">回测运行中…（完成后可再次触发）</span>
        </div>
      </div>
    </div>

    <!-- 进度（SSE 驱动；跨页签保活） -->
    <TaskProgress />

    <!-- 历史回测结果（v7.0.34）：只读加载，不碰运行中任务 -->
    <div class="card bt-history">
      <h3>📜 历史回测结果</h3>
      <p class="muted small">加载过往回测产物到下方结果区（只读；不影响运行中任务）。</p>
      <div class="f-row">
        <span>历史运行</span>
        <select id="bt-history-select" v-model="historySel" @change="onHistoryLoad"
                :disabled="historyRuns.length === 0">
          <option value="" disabled>选择一次过往回测…</option>
          <option v-for="r in historyRuns" :key="r.task_id" :value="r.task_id">
            {{ historyLabel(r) }}
          </option>
        </select>
        <button id="btn-bt-history-refresh" class="muted" @click="loadHistory" title="刷新列表">🔄</button>
      </div>
      <div v-if="historyErr" class="msg-err">{{ historyErr }}</div>
    </div>

    <!-- 结果区 -->
    <div v-if="resultsError" class="msg-err">{{ resultsError }}</div>
    <div v-if="results" class="bt-results">
      <div class="card">
        <h3>回测结果</h3>
        <p class="muted small">{{ metaText }}</p>
        <div ref="chartEl" id="bt-chart" style="width:100%;height:420px;"></div>
        <div class="muted small">{{ chartError || "组合市值（cash 口径：持仓按原始收盘 × 股数 + 累计现金分红；虚线=累计投入）" }}</div>
      </div>

      <div class="card">
        <h3>个股汇总（per_stock）</h3>
        <SimpleTable :rows="results.per_stock || []" :cols="PER_STOCK_COLS" empty="（无个股结果）" />
      </div>

      <div class="card">
        <h3>买入时间表（buys）</h3>
        <SimpleTable :rows="results.buys || []" :cols="BUYS_COLS" empty="（无买入记录）" />
      </div>

      <div class="card" v-if="results.summary">
        <h3>摘要（summary.md）</h3>
        <MarkdownView :md="results.summary" />
      </div>
    </div>
  </section>
</template>
