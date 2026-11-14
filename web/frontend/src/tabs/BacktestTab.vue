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
// 结束日默认 = 今天（Joel 2026-11-13 定案）；快捷「近 20/10/5 年」一键设窗口
// （start = 今天−N 年，end = 今天）；start/end 均留空 = 各股数据起点 → 数据
// 终点（旧语义保留，手动清空即回退）。
// ---------------------------------------------------------------------------
function _pad2(x) { return String(x).padStart(2, "0"); }
function _dateStr(d) {
  return `${d.getFullYear()}-${_pad2(d.getMonth() + 1)}-${_pad2(d.getDate())}`;
}
function quickWindow(years) {
  const s = new Date();
  s.setFullYear(s.getFullYear() - years);
  form.value.start = _dateStr(s);
  form.value.end = _dateStr(new Date());
}
const form = ref({ start: "", end: _dateStr(new Date()), mode: "pool", codes: "" });
const starting = ref(false);

// ---------------------------------------------------------------------------
// 指定标的模糊搜索（v8.0.9，与「我的持仓」/手动池录入条同口径）
// 只对**最后一个逗号段**做候选；选中回写 ts_code + 逗号（续填下一只）；
// include_delisted=1（历史回测可含退市股）；503 lake_locked → 静默降级仍可手输。
// ---------------------------------------------------------------------------
const sug = ref([]);
const sugOpen = ref(false);
const sugHi = ref(-1);
let sugTimer = null;
let sugSeq = 0;

function lastSegment(v) {
  const i = (v || "").lastIndexOf(",");
  return i === -1 ? (v || "") : v.slice(i + 1);
}

function codeSearchable(v) {
  const s = (v || "").trim();
  if (!s) return false;
  if (/^(?:sh|sz|bj)?\.?\d+$/i.test(s)) {   // 代码/纯数字：≥3 位才查（服务端同规则）
    return s.replace(/^[a-z]+\.?/i, "").length >= 3;
  }
  return true; // 中文 / 拼音：1 位起查
}

async function onCodesInput() {
  if (sugTimer) clearTimeout(sugTimer);
  const seg = lastSegment(form.value.codes).trim();
  if (!codeSearchable(seg)) { closeSug(); return; }
  sugTimer = setTimeout(async () => {
    const seq = ++sugSeq;
    try {
      const d = await api("/api/stocks/search?q=" + encodeURIComponent(seg)
        + "&include_delisted=1");
      if (seq !== sugSeq) return; // 过期响应丢弃
      sug.value = d.results || [];
      sugHi.value = -1;
      sugOpen.value = sug.value.length > 0;
    } catch {
      if (seq === sugSeq) { sug.value = []; sugOpen.value = false; }
    }
  }, 250);
}

function closeSug() {
  if (sugTimer) clearTimeout(sugTimer);
  sugSeq++;
  sug.value = []; sugOpen.value = false; sugHi.value = -1;
}

function pickSug(r) {
  const v = form.value.codes || "";
  const i = v.lastIndexOf(",");
  const prefix = i === -1 ? "" : v.slice(0, i + 1);
  form.value.codes = prefix + r.ts_code + ",";
  closeSug();
}

function onCodesKeydown(e) {
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    if (!sugOpen.value || !sug.value.length) return;
    e.preventDefault();
    const n = sug.value.length;
    sugHi.value = e.key === "ArrowDown"
      ? (sugHi.value + 1) % n : (sugHi.value - 1 + n) % n;
    return;
  }
  if (e.key === "Enter" && sugOpen.value) {
    e.preventDefault();
    pickSug(sug.value[Math.max(0, sugHi.value)]);
    return;
  }
  if (e.key === "Escape") closeSug();
}

// mode: "pool"（静态股票池，默认）| "dyn"（v8.0.13 动态池：月度重算 PIT）
//       | "codes"（指定标的，逗号分隔）
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
    } else if (form.value.mode === "dyn") {
      params.dynamic_pool = true;   // v8.0.13：池每月重算 + 出池冻结开仓/加仓
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
// v8.0.10：比率 → 百分比（1 位）。per_stock 的 pnl_pct / min_unreal_pct / 占比
// 均为比率（pnl_pct=2.14 实为 +214%），此前 fmt2 裸显示成「2.14」挂在 % 列名下
// ——与 summary.md 的 214.0% 对不上，Joel 看占比拆解时一并修正。
const fmtPct1 = (v) => {
  const n = typeof v === "number" ? v : parseFloat(v);
  return isNaN(n) ? String(v) : (n * 100).toFixed(1) + "%";
};
const fmtDate = (v) => {
  const s = String(v);
  const i = s.indexOf("T");
  return i > 0 ? s.slice(0, i) : s;
};
// v8.0.10（Joel：低波红利策略，回测要看分红占比 vs 上涨占比）：
// 前端预计算每行占比字段（SimpleTable 的 fmt 只接单值不接整行）。
// 口径与 summary.md「拆分」行一致：分红占比=分红余额(div_pool)/总盈亏、
// 上涨占比=价差盈亏(price_pnl)/总盈亏（恒等式 总盈亏≈价差盈亏+分红余额，
// 差额为手续费）。总盈亏≤0 或旧产物无 div_pool 列 → null（表内显示 —）。
const perStockRows = computed(() => {
  const rows = (results.value && results.value.per_stock) || [];
  return rows.map((r) => {
    const num = (x) => (typeof x === "number" ? x : parseFloat(x));
    const tp = num(r.total_pnl);
    const dp = num(r.div_pool);
    const pp = num(r.price_pnl);
    const ok = Number.isFinite(tp) && tp > 0 && Number.isFinite(dp) && Number.isFinite(pp);
    return ok
      ? { ...r, div_share: dp / tp, price_share: pp / tp }
      : { ...r, div_share: null, price_share: null };
  });
});
const PER_STOCK_COLS = [
  ["ts_code", "代码"], ["name", "名称"], ["n_bars", "bar数", true],
  ["n_rounds", "轮次", true], ["n_rearms", "再武装", true], ["n_buys", "买入笔数", true],
  ["total_invested", "总投入", true, fmt2], ["final_assets", "期末资产", true, fmt2],
  ["dividends", "累计分红", true, fmt2], ["div_pool", "分红余额", true, fmt2],
  ["price_pnl", "价差盈亏", true, fmt2], ["total_pnl", "总盈亏", true, fmt2],
  ["div_share", "分红占比", true, fmtPct1], ["price_share", "上涨占比", true, fmtPct1],
  ["pnl_pct", "盈亏%", true, fmtPct1], ["min_unreal_pct", "最低浮盈%", true, fmtPct1],
  // v8.0.13 动态池产物（静态回测无此键 → —）
  ["pool_months", "在池月数"], ["pool_first", "首次入池"], ["pool_last", "最后入池"],
  ["first_buy_date", "首买日", false, fmtDate], ["last_buy_date", "末买日", false, fmtDate],
  ["last_status", "末状态"], ["end_date", "期末", false, fmtDate],
];

const BUYS_COLS = [
  ["ts_code", "代码"], ["seq", "序", true], ["date", "日期", false, fmtDate],
  ["price", "成交价", true, fmt2], ["amount", "金额", true, fmt2],
];

// v8.0.13 动态池：月度池漂移时间线（pool_timeline.csv 透传；静态产物无此键）
const fmtList = (v) => (v && v.length > 60 ? v.slice(0, 60) + "…" : (v || ""));
const TIMELINE_COLS = [
  ["as_of_date", "重算日"], ["n_pool", "池规模", true],
  ["n_entered", "入池", true], ["n_exited", "出池", true],
  ["entered", "入池名单", false, fmtList],
  ["exited", "出池名单", false, fmtList],
];
const timelineRows = computed(
  () => (results.value && results.value.pool_timeline) || []
);

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

// ---------------------------------------------------------------------------
// 买入时间表折叠（v7.0.38，Joel 2026-11-10「回测报告里这个买入时间表太长了，
// 折叠一下」）：SimpleTable 无分页，池回测 200+ 行整屏铺开 → 卡片可折叠。
// 默认态按行数：≤30 行展开（小回测直接可见），>30 行折叠（大回测省版面）；
// 每次新结果加载重置默认态，标题按钮手动切换（v-show 保表内搜索/排序状态）。
// ---------------------------------------------------------------------------
const BUYS_COLLAPSE_THRESHOLD = 30;
const buysOpen = ref(true);
const buysN = computed(() => (results.value && results.value.buys) ? results.value.buys.length : 0);
watch(
  () => results.value && results.value.buys,
  (rows) => { buysOpen.value = !rows || rows.length <= BUYS_COLLAPSE_THRESHOLD; },
  { immediate: true }
);
function toggleBuys() { buysOpen.value = !buysOpen.value; }

onMounted(loadHistory);
</script>

<template>
  <section class="tab-panel active">
    <!-- 参数表单 + 触发 -->
    <div class="card bt-form">
      <h3>🧪 按需回测</h3>
      <p class="muted small">
        结束日默认今天；「近 20/10/5 年」一键设窗口（起始 = 今天 − N 年）。
        起止均留空 = 各股数据起点 → 数据终点。标的默认全股票池（5 条件全链）。
        回测为异步子进程，进度经 SSE 实时推送。
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
        <div class="f-row f-quick">
          <span>快捷</span>
          <button type="button" class="bt-quick" :disabled="starting || store.state === 'running'" @click="quickWindow(20)">近 20 年</button>
          <button type="button" class="bt-quick" :disabled="starting || store.state === 'running'" @click="quickWindow(10)">近 10 年</button>
          <button type="button" class="bt-quick" :disabled="starting || store.state === 'running'" @click="quickWindow(5)">近 5 年</button>
        </div>
        <div class="f-row f-scope">
          <span>标的</span>
          <label class="radio"><input type="radio" value="pool" v-model="form.mode" :disabled="starting || store.state === 'running'"> 静态股票池（默认，当前池固定回测）</label>
          <label class="radio" title="v8.0.13：池每月重算（全漏斗 point-in-time），出池冻结开仓/加仓、不强制平仓；十年窗口约 20~30 分钟"><input type="radio" value="dyn" v-model="form.mode" :disabled="starting || store.state === 'running'"> 动态股票池（月度重算，较慢）</label>
          <label class="radio"><input type="radio" value="codes" v-model="form.mode" :disabled="starting || store.state === 'running'"> 指定标的</label>
        </div>
        <label class="f-row" v-if="form.mode === 'codes'">
          <span>代码</span>
          <span class="bt-codes-wrap">
            <input type="text" v-model="form.codes"
                   placeholder="sh.601088,sz.000651（逗号分隔；代码/名称/拼音可搜）"
                   :disabled="starting || store.state === 'running'"
                   @input="onCodesInput" @keydown="onCodesKeydown"
                   @blur="() => setTimeout(closeSug, 150)">
            <ul v-if="sugOpen" class="bt-codes-sug">
              <li v-for="(r, i) in sug" :key="r.ts_code" :class="{ hi: i === sugHi }"
                  @mousedown.prevent="pickSug(r)">
                {{ r.ts_code }} {{ r.name }}<span class="bt-codes-ind">{{ r.industry }}</span>
                <span v-if="r.delisted" class="bt-codes-ind">（退市）</span>
              </li>
            </ul>
          </span>
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
        <button id="btn-bt-history-refresh" class="ghost-btn" @click="loadHistory" title="刷新列表">🔄 刷新</button>
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
        <SimpleTable :rows="perStockRows" :cols="PER_STOCK_COLS" empty="（无个股结果）" />
      </div>

      <!-- v8.0.13 动态池：月度池漂移时间线（静态产物无 pool_timeline 键 → 隐藏） -->
      <div v-if="timelineRows.length" class="card">
        <h3>池漂移时间线（动态池 · 每月重算）</h3>
        <SimpleTable :rows="timelineRows" :cols="TIMELINE_COLS" empty="（无时间线）" />
      </div>

      <div class="card bt-buys">
        <h3>
          <button id="btn-bt-buys-toggle" class="card-toggle" @click="toggleBuys">
            <span aria-hidden="true">{{ buysOpen ? "▾" : "▸" }}</span>
            买入时间表（buys）
            <span v-if="buysN" class="cnt">{{ buysN }} 行</span>
          </button>
        </h3>
        <SimpleTable v-show="buysOpen" :rows="results.buys || []" :cols="BUYS_COLS" empty="（无买入记录）" />
        <p v-if="!buysOpen" class="muted small">已折叠 · 点击标题展开（{{ buysN }} 行）</p>
      </div>

      <div class="card" v-if="results.summary">
        <h3>摘要（summary.md）</h3>
        <MarkdownView :md="results.summary" />
      </div>
    </div>
  </section>
</template>

<style scoped>
/* v8.2.0 补（Joel 2026-11-13）：回测窗口快捷按钮（近 20/10/5 年） */
.bt-quick {
  padding: 3px 10px; font-size: 12px; border: 1px solid #c7cdd8; border-radius: 6px;
  background: #fff; color: #374151; cursor: pointer; white-space: nowrap;
}
.bt-quick:hover:not(:disabled) { border-color: #7c8aa5; background: var(--bg-soft, #eef0f8); }
.bt-quick:disabled { opacity: .5; cursor: default; }
/* v8.0.9：指定标的代码模糊搜索下拉（与手动池录入条同款） */
.bt-codes-wrap { position: relative; display: inline-block; }
.bt-codes-wrap input { width: 320px; }
.bt-codes-sug {
  position: absolute; top: 100%; left: 0; right: 0; z-index: 30;
  margin: 2px 0 0; padding: 4px 0; list-style: none;
  background: var(--bg, #fff); border: 1px solid var(--line, #d8d8e0);
  border-radius: 8px; box-shadow: 0 6px 18px rgba(20, 20, 40, .12);
  max-height: 240px; overflow: auto;
}
.bt-codes-sug li { padding: 6px 12px; font-size: 13px; cursor: pointer; white-space: nowrap; }
.bt-codes-sug li.hi, .bt-codes-sug li:hover { background: var(--bg-soft, #eef0f8); }
.bt-codes-ind { color: #888; margin-left: 8px; font-size: 12px; }
</style>
