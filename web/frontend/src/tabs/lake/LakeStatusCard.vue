<script setup>
// LakeStatusCard —— 区块C「数据库状态 / 补齐进度」（移植 v6.0.5 lakeRenderSummary/
// lakeRenderTables/lakeRenderViews + lakeRenderTasksTable；v6.1 内嵌 SourcePoolPanel）。
// 数据全部来自 lakeStore.status（/status 自适应轮询快照）——本组件不发请求。
//
// 三态纪律对齐 vanilla loadLakeStatus：
//   - !installed → "数据湖未安装"占位（tasks 表 colspan=6 "数据湖不可用"）；
//   - initialized=false → 汇总条空态"库未初始化 —— 请先运行 backfill init"；
//   - locked/灌数中 → tables 缺省 → 占位行"灌数进行中 —— 表级状态完成后可见"、
//     views 区不显示（d.views 缺省）、SourcePoolPanel 整块降级。
import { computed } from "vue";
import { useLakeStore } from "../../stores/lakeStore.js";
import SyncControl from "./SyncControl.vue";
import SourcePoolPanel from "./SourcePoolPanel.vue";

const lake = useLakeStore();
const d = computed(() => lake.status);

// ---- 汇总条（lakeRenderSummary L1732-1753 逐字对齐）----
// 总体徽章：灌数中→⏳ / 任一 lagging→🕓有滞后 / 其余→✅正常；未初始化 → 空态文案
const summary = computed(() => {
  const s = d.value;
  if (!s) return null;   // 错误态：汇总条不渲染（红横幅已说明）
  if (s.initialized === false) {
    return { text: "库未初始化 —— 请先运行 <code>python scripts/lake_backfill.py init</code>", badge: null, uninit: true };
  }
  let badge;
  if (s.backfill_in_progress) badge = { cls: "lake-st-lagging", label: "⏳ 灌数中" };
  else if ((s.tables || []).some((t) => t.state === "lagging")) badge = { cls: "lake-st-lagging", label: "🕓 有滞后" };
  else badge = { cls: "lake-st-fresh", label: "✅ 正常" };
  const parts = [];
  const nTables = (s.tables || []).length, nViews = (s.views || []).length;
  if (nTables) parts.push(`${nTables} 表` + (nViews ? ` + ${nViews} 视图` : ""));
  if (s.db && s.db.size_mb != null) parts.push(`库 ${s.db.size_mb} MB`);
  parts.push(`DuckDB ${s.duckdb_version || "?"}`);
  const ts = (s.sync && s.sync.last_updated_at) || s.updated_at;
  parts.push(`最后同步 ${ts ? String(ts).slice(5, 16) : "—"}`);
  return { parts, badge };
});

// ---- state → 徽章（LAKE_STATE_BADGE L1712-1729 逐字对齐）----
const STATE_BADGE = {
  fresh: ["lake-st-fresh", "✅ 最新"],
  lagging: ["lake-st-lagging", "🕓 滞后"],
  pending: ["lake-st-pending", "⏳ 待补"],
  empty: ["lake-st-empty", "— 空"],
};
function stateCell(t) {
  const [cls, label] = STATE_BADGE[t.state] || ["lake-st-empty", String(t.state ?? "—")];
  let detail = t.state_detail || "";
  const word = label.replace(/^[^\s]+\s*/, "");   // 去 emoji 留状态词
  if (detail === word) detail = "";
  else if (word && detail.startsWith(word + " ")) detail = detail.slice(word.length).trim();
  return { cls, label, detail };
}

// ---- tasks 表（lakeRenderTasksTable L1199-1212）----
function taskPct(t) { return t.total ? Math.round((t.done / t.total) * 100) : 0; }

// v6.1.4 O3：T1~T9 ↔ tasks 对应关系（"对应表"列——让 Joel 一眼看懂 pending 三表 =
// T2/T3/T7 的每日增量部分（P3），与 kline_history（T2 全史，已完成）是同一张库表的
// 不同灌数阶段）。未收录的 table → 原样显示（不猜映射）。
const TASK_TABLE_CN = {
  stock_master: "T1 股票主档",
  kline_history: "= T2 全史（kline_daily）",
  kline_daily: "T2 增量（kline_daily）",
  valuation_daily: "T3 估值日线",
  fundamentals_quarterly: "T5 季度基本面",
  holders_snapshot: "T6 前十大股东",
  index_daily: "T7 指数日线",
};
function taskTableCn(t) { return TASK_TABLE_CN[t.table] || t.table; }

// v6.1.4 O3：tasks 状态徽章。v6.1.7 起 holders_snapshot（T6）接入 full 阶段 4，
// 不再是 no_source——由真实任务态（pending/running/done/error）走下方通用渲染
// （STATE_BADGE 命中 pending→"⏳ 待补"，其余回退原始态字符串）。旧的 no_source
// 灰 badge"暂无数据源"分支随数据源接入移除（brief §B：硬编码 no_source 文案清理）。
function taskStateCell(t) {
  const [cls, label] = STATE_BADGE[t.state] || ["lake-st-empty", String(t.state ?? "idle")];
  return { cls, label };
}

const views = computed(() => (d.value && d.value.views) || []);
const afPct = computed(() => {
  const p = d.value && d.value.adj_factor_coverage_pct;
  return p == null ? null : Math.max(0, Math.min(100, Number(p)));
});

const tables = computed(() => (d.value && d.value.tables) || []);
</script>

<template>
  <div class="card" id="lake-status-card">
    <h3>数据湖状态 / 补齐进度
      <button class="mini-btn" title="刷新" @click="lake.refreshAll()">⟳</button>
    </h3>

    <!-- v6.0.9/v6.0.10 同步控制（状态机原样移植） -->
    <SyncControl />

    <template v-if="d">
      <!-- 未安装占位（vanilla loadLakeStatus !installed 分支） -->
      <div v-if="!d.installed" class="lake-summary"><span class="muted">数据湖未安装</span></div>

      <template v-else>
        <!-- 汇总条 -->
        <div id="lake-summary" class="lake-summary">
          <template v-if="summary && summary.uninit">
            <span class="muted" v-html="summary.text"></span>
          </template>
          <template v-else-if="summary">
            <template v-for="(p, i) in summary.parts" :key="i">
              <span>{{ p }}</span><span v-if="i < summary.parts.length - 1" class="lake-summary-sep">·</span>
            </template>
            <span class="badge" :class="summary.badge.cls">{{ summary.badge.label }}</span>
          </template>
        </div>

        <!-- v6.1 多源资源池面板（数据 = /status.source_pool；backfill 中整块降级） -->
        <SourcePoolPanel :pool="d.source_pool || null" :backfill="!!d.backfill_in_progress" />

        <!-- 9 表清单（固定顺序由后端保证；零数据 muted） -->
        <div class="tbl-wrap lake-tables-wrap">
          <table class="data" id="lake-tables">
            <thead><tr>
              <th>表</th><th>说明</th><th class="num">行数</th><th class="num">股票数</th>
              <th>数据区间</th><th>状态</th><th>最后同步</th>
            </tr></thead>
            <tbody>
              <tr v-if="!tables.length">
                <td colspan="7" class="placeholder">
                  {{ d.backfill_in_progress ? "灌数进行中 —— 表级状态完成后可见" : "暂无表级状态" }}
                </td>
              </tr>
              <tr v-for="t in tables" :key="t.key" class="lake-table-row" :class="{ 'lake-row-muted': t.rows <= 0 }">
                <td><div>{{ t.name_cn }}</div><div class="muted small mono">{{ (t.tier || "") + " · " + t.key }}</div></td>
                <td class="lake-t-desc"><span class="muted small">{{ t.desc || "—" }}</span></td>
                <td class="num">{{ t.rows != null ? t.rows.toLocaleString("en-US") : "—" }}</td>
                <td class="num">{{ t.codes != null ? t.codes.toLocaleString("en-US") : "—" }}</td>
                <td class="mono small">{{ (t.date_min && t.date_max) ? t.date_min + " ~ " + t.date_max : "—" }}</td>
                <td class="lake-t-state">
                  <span class="badge" :class="stateCell(t).cls">{{ stateCell(t).label }}</span>
                  <span v-if="stateCell(t).detail" class="muted small">{{ stateCell(t).detail }}</span>
                </td>
                <td class="muted small mono">{{ t.last_sync_at ? String(t.last_sync_at).slice(5, 16) : "—" }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- 视图区（locked/降级态 views 缺省 → 不显示） -->
        <div v-if="views.length" id="lake-views" class="lake-views">
          <div class="lake-views-title muted small">派生视图</div>
          <div v-for="v in views" :key="v.key" class="lake-view-row">
            <span class="mono small">{{ v.key }}</span> <b>{{ v.name_cn }}</b>
            <span class="muted small">—— {{ v.desc || "" }}</span>
          </div>
          <div v-if="afPct != null" class="lake-af-line">
            <span class="muted small">复权因子覆盖 {{ afPct.toFixed(1) }}%</span>
            <span class="progress-track lake-af-track"><span class="progress-bar" :style="{ width: afPct + '%' }"></span></span>
            <span v-if="afPct < 5" class="lake-af-hint muted small">history 补齐后 hfq/qfq 全量可用</span>
          </div>
        </div>

        <!-- tasks 表（v6.1.4 O3：**仅 backfill_in_progress=true 时显示**——非同步态整块隐藏；
             琥珀块 #lake-backfill-tasks 同条件出现，两表数据同源 progress.tasks。
             v6.1.2 P1-B 的"灌数中不渲染去冗余"被 O3 显隐规则取代：Joel 要的是
             "非同步态别看到一堆 pending"，同步态才需要看补齐进度。）
             v6.1.4 O3：加"对应表"列（T1~T9 ↔ tasks 映射；kline_history="= T2 全史"、
             kline_daily="T2 增量"…）+ no_source 灰 badge"暂无数据源"（T6）。 -->
        <div v-if="d.backfill_in_progress" class="tbl-wrap lake-tasks-wrap">
          <table class="data" id="lake-tasks-table">
            <thead><tr>
              <th>表</th><th>对应表</th><th>层级</th><th>状态</th><th class="num">进度</th>
              <th class="num">ETA(min)</th>
            </tr></thead>
            <tbody>
              <tr v-if="!d.tasks || !d.tasks.length"><td colspan="6" class="placeholder">暂无后台补齐任务</td></tr>
              <tr v-for="(t, i) in d.tasks || []" :key="i">
                <td>{{ t.table }}</td>
                <td class="muted small">{{ taskTableCn(t) }}</td><td>{{ t.tier }}</td>
                <td><span class="badge" :class="taskStateCell(t).cls">{{ taskStateCell(t).label }}</span></td>
                <td class="num lake-task-progress">
                  <div class="progress-track" style="margin:0"><div class="progress-bar" :style="{ width: taskPct(t) + '%' }"></div></div>
                  {{ (t.done ?? 0) + "/" + (t.total ?? 0) }}
                </td>
                <td class="num">{{ t.eta_min ?? "—" }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>

      <!-- !installed 时 tasks 占位（vanilla colspan=6 "数据湖不可用"） -->
      <div v-if="!d.installed" class="tbl-wrap lake-tasks-wrap">
        <table class="data"><tbody><tr><td colspan="6" class="placeholder">数据湖不可用</td></tr></tbody></table>
      </div>
    </template>

    <!-- 错误态（status=null）：降级占位不白屏（红横幅在页顶已说明原因） -->
    <p v-else class="placeholder">状态加载失败 —— 稍后自动重试</p>
  </div>
</template>
