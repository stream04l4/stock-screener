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
//
// v6.2.1 S2 — 页面归属调整：backtest + reinvest 两段从 StrategyTab 移入本页。
// - "回测参数"区复用 StrategyCards/FieldEditor 组件（groups=BACKTEST_GROUPS、lsKey 独立）；
// - 保存模式 = 与 StrategyTab 完全相同的「load full json → 编辑子集 → PUT full」：
//   draft 以 GET /api/strategy 的完整 JSON 深拷贝为底，FieldEditor 只改 backtest/reinvest
//   段字段，PUT 提交**整份 payload**（保证其他段一个 key 都不丢）；
// - 逐字段校验复用 StrategyTab onSave 同款逻辑（FIELD_TYPE 分支），任一非法 → msg-box 不提交；
// - PUT 成功 → toast + invalidate("strategy")（StrategyTab 若挂载会同步重拉，语义一致）。
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { api } from "../api/client.js";
import { useLiveQuery } from "../live/useLiveQuery.js";
import { invalidate } from "../live/queryRegistry.js";
import { useToastStore } from "../stores/toastStore.js";
import { FIELD_TYPE, BACKTEST_GROUPS } from "../data/strategyMeta.js";
import StrategyCards from "./strategy/StrategyCards.vue";
import { fmtNum } from "../utils/fmt.js";

const toast = useToastStore();

// ---- 回测产物（净值曲线 + metrics）----
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

// ===========================================================================
// v6.2.1 S2 — 回测参数编辑区（backtest + reinvest 两段；与 StrategyTab 同保存模式）
//   数据源 = GET /api/strategy（完整配置 JSON）；只读态展示 + 编辑态 FieldEditor。
//   注意：与上方 useLiveQuery("backtest")（回测**产物**）是两个不同 key，互不串味。
// ===========================================================================
const { data: cfgData, loading: cfgLoading } = useLiveQuery("strategy", () => api("/api/strategy"), {});

const strategyJson = ref(null); // GET /api/strategy 成功后的 d.json（只读底 + PUT payload 底）
const editing = ref(false);     // 本 tab 编辑态标志（独立于 StrategyTab；D-W02 同款纪律：
const draft = ref(null);        // 编辑草稿（完整 JSON 深拷贝为底，FieldEditor 只改子集段）
const saving = ref(false);      // PUT 在途
const msg = ref({ text: "", kind: null });

// D-W02 同款：唯一入口（进入/退出编辑态的所有路径都经过它，按钮状态只读绑定 editing）
function setEditing(on) { editing.value = !!on; }

watch(
  () => cfgData.value,
  (d) => {
    if (!d || !d.json) return;
    strategyJson.value = d.json;
    setEditing(false); // 加载成功 → 统一回到只读态（幂等）
  },
  { immediate: true }
);

function onEdit() {
  if (!strategyJson.value) return;
  draft.value = JSON.parse(JSON.stringify(strategyJson.value)); // 完整深拷贝为底（PUT full 保证其他段不丢）
  setEditing(true);
  msg.value = { text: "", kind: null };
}

// FieldEditor 变更 → 合并进 draft（与 StrategyTab onDraftUpdate 同语义）
function onDraftUpdate(u) {
  if (!draft.value) return;
  draft.value[u.section] = draft.value[u.section] || {};
  draft.value[u.section][u.field] = u.value;
}

// 保存（与 StrategyTab onSave 同款逐字段校验 → PUT full → 成功重拉 / 400 保持编辑态）
async function onSave() {
  if (!draft.value) return;
  const payload = JSON.parse(JSON.stringify(draft.value)); // 深拷贝提交（完整 payload，其他段原样带回）
  let bad = false;

  for (const [section, fields] of Object.entries(payload)) {
    if (!fields || typeof fields !== "object") continue;
    for (const [field, value] of Object.entries(fields)) {
      const t = FIELD_TYPE[field] || "str";
      let v;
      if (t === "weights_dict") {
        for (const [dim, wv] of Object.entries(value || {})) {
          const n = Number(wv);
          if (isNaN(n) || n < 0 || n > 1) { setMsg(`✗ ${section}.${field}.${dim} 必须在 0–1`, "err"); bad = true; break; }
        }
      } else if (t === "sub_weights_dict") {
        for (const [dim, parsed] of Object.entries(value || {})) {
          if (!parsed) { setMsg(`✗ ${section}.${field}.${dim} 格式应为 key:值,key:值`, "err"); bad = true; break; }
        }
      } else if (t === "int") {
        v = Number.isInteger(Number(value)) ? parseInt(value, 10) : NaN;
        if (isNaN(v)) { setMsg(`✗ ${section}.${field} 必须是整数`, "err"); bad = true; continue; }
      } else if (t === "float") {
        v = parseFloat(value);
        if (isNaN(v)) { setMsg(`✗ ${section}.${field} 必须是数值`, "err"); bad = true; continue; }
      } else if (t === "bool") {
        v = value === "true" || value === true;
      } else if (t === "list") {
        v = String(value).split(",").map((s) => s.trim()).filter(Boolean);
        if (!v.length) { setMsg(`✗ ${section}.${field} 不能为空`, "err"); bad = true; continue; }
      } else {
        v = value; // str / enum2：原样（enum 值域由后端校验，400 回显）
      }
      if (t !== "weights_dict" && t !== "sub_weights_dict") payload[section][field] = v;
    }
    if (bad) break;
  }
  if (bad) return; // 本地校验失败 → 不提交

  saving.value = true;
  try {
    const res = await api("/api/strategy", { method: "PUT", body: JSON.stringify(payload) });
    setMsg(`✓ 回测参数已保存（备份: ${String(res.backup).split("/").pop()}）`, "ok");
    setEditing(false); // 唯一入口退出编辑态
    toast.toast("回测参数已更新");
    invalidate("strategy"); // 事件触发重拉 GET（StrategyTab 若挂载同步刷新，语义一致）
  } catch (e) {
    // 400 拒绝：保持编辑态，用户可改正后重试
    const errs = e.body && e.body.detail && e.body.detail.errors ? e.body.detail.errors.join("\n") : e.message;
    setMsg("✗ 保存被拒绝（400）：\n" + errs, "err");
    toast.toast("回测参数保存失败", false);
  } finally {
    saving.value = false;
  }
}

// 取消（恢复工具栏 + 重拉只读视图，与 StrategyTab onCancel 同语义）
function onCancel() {
  setEditing(false);
  invalidate("strategy");
}

function setMsg(text, kind) { msg.value = { text, kind }; }

onMounted(() => { /* 首帧由 watch(data)/watch(cfgData) 驱动（SWR：缓存命中时挂载即有值） */ });

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

    <!-- v6.2.1 S2：回测参数（backtest + reinvest 两段；从 StrategyTab 移入） -->
    <div class="card bt-params">
      <div class="bt-params-head">
        <h3>🧪 回测参数（backtest / reinvest）</h3>
        <span class="muted small">改动写入 config/strategy.yaml（PUT full，其他段不受影响；写前自动备份 .bak）</span>
      </div>
      <div class="bt-params-toolbar strategy-toolbar">
        <button id="btn-bt-edit" class="primary-btn" :class="{ hidden: editing }" @click="onEdit">✏️ 编辑模式</button>
        <button id="btn-bt-save" class="primary-btn" :class="{ hidden: !editing }" :disabled="saving" @click="onSave">💾 保存策略</button>
        <button id="btn-bt-cancel" class="ghost-btn" :class="{ hidden: !editing }" @click="onCancel">取消</button>
      </div>
      <p v-if="cfgLoading && !strategyJson" class="placeholder">加载中…</p>
      <StrategyCards v-else-if="strategyJson" :json="strategyJson" :draft="draft" :editing="editing"
                     :groups="BACKTEST_GROUPS" ls-key="backtest_group_collapsed_v1" strict @update="onDraftUpdate" />
      <div class="msg-box">
        <div v-if="msg.text" :class="msg.kind === 'err' ? 'msg-err' : 'msg-ok'">{{ msg.text }}</div>
      </div>
    </div>
  </section>
</template>
