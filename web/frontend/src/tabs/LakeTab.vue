<script setup>
// 数据湖 tab（v6：DuckDB 独立分析层；/api/lake/*）—— D3 全量实现。
//
// 组件树（报告 §3 T-数据湖）：
//   ErrorBanner（红横幅 5xx，lake.lastError）/ BackfillBlock（琥珀灌数块，三态互不串味）
//   → LakeSearchBar（防抖250ms+下拉；409 backfill 中性处理）
//   → StockPanoramaCard（个股全景 + ECharts candlestick K线）
//   → MarketTable（服务端分页+筛选；行点击→全景卡+scrollIntoView）
//   → LakeStatusCard（SyncControl v6.0.10 状态机 + SummaryBar + SourcePoolPanel v6.1
//     + TableList 9表 + ViewsSection adj覆盖 + TasksTable）
//
// 生命周期：keep-alive onActivated/onDeactivated → lakeStore.activate()/deactivate()
// （/status 自适应轮询：激活3s / stopping 1s / 非激活但 running 10s / idle停；
// 切页先立即拉一次——报告 §2.3）。running→idle 跃迁的 toast+invalidate('lake:*')
// 归 lakeStore（跨页签感知，Joel 核心诉求）。
import { computed, onActivated, onDeactivated, ref } from "vue";
import { useLakeStore } from "../stores/lakeStore.js";
import LakeSearchBar from "./lake/LakeSearchBar.vue";
import StockPanoramaCard from "./lake/StockPanoramaCard.vue";
import MarketTable from "./lake/MarketTable.vue";
import LakeStatusCard from "./lake/LakeStatusCard.vue";

const lake = useLakeStore();
const stockCode = ref("");      // 当前选中股（""=未选 → 全景卡占位）

// v6.3.1 R4-a：tasks 状态中文映射（现状 raw 英文渲染，class 与文字同串）。
// 返回 [cssClass, 中文文字]——class 复用既有 .badge.running/.done/.error/.idle
// （stopped_by_signal→idle 灰色，语义"已停止"不是失败；未知 state 回退 raw，
// 文字=raw 原文，CSS 无该 class 时退回 .badge 基础样式，不崩）。
const TASK_STATE = {
  running: ["running", "运行中"],
  done: ["done", "已完成"],
  error: ["error", "失败"],
  stopped_by_signal: ["idle", "已停止"],
  stopping: ["running", "停止中"],
  pending: ["idle", "待处理"],
  blocked_quota: ["running", "配额到顶"],
  hang_watchdog: ["error", "看门狗中止"],
  idle: ["idle", "空闲"],
};
function taskState(t) {
  const s = (t && t.state) || "idle";
  return TASK_STATE[s] || [s, s];
}

// v6.1.2 P2-A：琥珀块总进度（纯前端，数据=lake.backfillView.tasks）。
// 所有 tasks 的 done 合计/total 合计 + 进度条 + 预计剩余（取各任务 eta_min 最大值，
// 换算"约 N 小时 M 分"；无 eta → "—"）。
const bfTasks = computed(() => (lake.backfillView && lake.backfillView.tasks) || []);
const totalProgress = computed(() => {
  const ts = bfTasks.value;
  let done = 0, total = 0, maxEta = null;
  for (const t of ts) {
    if (!t) continue;
    if (typeof t.done === "number") done += t.done;
    if (typeof t.total === "number") total += t.total;
    if (typeof t.eta_min === "number" && isFinite(t.eta_min)) {
      maxEta = maxEta == null ? t.eta_min : Math.max(maxEta, t.eta_min);
    }
  }
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  let etaText = "—";   // 无 eta → "—"（brief）
  if (maxEta != null) {
    const m = Math.max(0, Math.round(maxEta));
    const h = Math.floor(m / 60);
    const mm = m % 60;
    etaText = h > 0 ? `约 ${h} 小时 ${mm} 分` : `约 ${mm} 分`;
  }
  return { done, total, pct, etaText };
});

onActivated(() => lake.activate());
onDeactivated(() => lake.deactivate());

// 行点击/搜索选中 → 切全景卡 + scrollIntoView（brief：行点击→全景卡+scrollIntoView）。
// 组件 ref 拿到的是组件实例（script setup 单根组件）→ 用 $el / getElementById 取 DOM。
function onSelect(code) {
  stockCode.value = code;
  // 等 DOM 更新后滚动（v-if 分支切换 → nextTick 语义；rAF 双保险）
  requestAnimationFrame(() => {
    const el = document.getElementById("lake-stock-card");
    if (el && el.scrollIntoView) el.scrollIntoView({ behavior: "smooth", block: "start" });
  });
}

// 红横幅文案（vanilla lakeSetError："数据湖不可用：msg"）。
// **单一事实源（DEFECT-D3-1）**：lake.lastError 只存**裸 msg**（子组件 @error 直传、
// fetchStatus 的 duckdb 未安装/e.message 亦为裸值）——前缀**只在此处加一次**。
const errMsg = computed(() => (lake.lastError ? "数据湖不可用：" + lake.lastError : ""));

// v6.3.1 R4-b：陈旧快照告警。progress 的 updated_at 是 UTC "YYYY-MM-DD HH:MM:SS"
// （save_progress 落盘口径，与 String(ts).slice(5,16) 展示口径一致）——距 Date.now()
// > 120s → 视为陈旧（浏览器后台标签 timer 节流，进程退出后快照冻结，本轮事故 Joel
// 截图 14:44:19 仍显示 14:41:05 的旧 locked 快照）。仅 backfillView 存在时判。
const staleSnapshot = computed(() => {
  const v = lake.backfillView;
  if (!v || !v.updated_at) return false;
  const ts = String(v.updated_at).trim();
  const d = new Date(ts.replace(" ", "T") + "Z");   // UTC 解析（progress 时间戳是 UTC）
  if (Number.isNaN(d.getTime())) return false;
  return (Date.now() - d.getTime()) > 120000;
});
</script>

<template>
  <section class="tab-panel active">
    <!-- 三态纪律（v6.0.4，互不串味）：① 红横幅=5xx/网络错误；② 琥珀块=灌数中；
         ③ 未初始化空态在 LakeStatusCard 汇总条内（initialized=false） -->

    <!-- ① 红色错误横幅（仅 5xx/网络/duckdb 未装；409 backfill 永不进此横幅） -->
    <div v-if="errMsg" id="lake-error" class="lake-error">{{ errMsg }}</div>

    <!-- ② 琥珀色灌数中块（backfill_in_progress=true；持锁 PID + 进度更新 + tasks 摘要） -->
    <div v-if="lake.backfillView" id="lake-backfill" class="lake-backfill">
      <div class="lake-backfill-head">
        ⏳ 数据灌入中
        <span class="muted small">
          持锁 PID {{ (lake.backfillView.lock_holder_pid ?? "未知") + " · 进度更新于 " + (lake.backfillView.updated_at || "—") }}
        </span>
      </div>
      <!-- v6.3.1 R4-b：陈旧快照告警（updated_at 距今 >120s——进程可能已退出、
           浏览器后台标签 timer 节流冻结在旧 locked 快照，本轮事故同因） -->
      <div v-if="staleSnapshot" class="muted small">
        ⚠ 进度更新已超时（&gt;2min）——进程可能已退出，点 ⟳ 刷新确认
      </div>
      <!-- v6.1.2 P2-A：总进度行（所有 tasks done/total 合计 + 进度条 + 预计剩余 max eta_min） -->
      <div class="lake-backfill-total" id="lake-backfill-total">
        <span class="muted small">总进度</span>
        <div class="progress-track lake-backfill-total-track">
          <!-- v6.1.2 D-1 修复：进度条必须是块级元素（<div>，与明细表行内/af-coverage
               两处既有 .progress-bar 口径一致）。<span> 为 display:inline，CSS width 对
               非替换 inline 元素不生效 → 渲染宽度恒 0，用户看不到进度填充。 -->
          <div class="progress-bar" :style="{ width: totalProgress.pct + '%' }"></div>
        </div>
        <span class="mono small">{{ totalProgress.done }}/{{ totalProgress.total }}</span>
        <span class="muted small">预计剩余 {{ totalProgress.etaText }}</span>
      </div>
      <div class="tbl-wrap">
        <table class="data" id="lake-backfill-tasks">
          <thead><tr><th>表</th><th>层级</th><th>状态</th><th class="num">进度</th><th class="num">ETA(min)</th></tr></thead>
          <tbody>
            <tr v-if="!lake.backfillView.tasks || !lake.backfillView.tasks.length">
              <td colspan="5" class="placeholder">暂无后台补齐任务</td>
            </tr>
            <tr v-for="(t, i) in lake.backfillView.tasks || []" :key="i">
              <td>{{ t.table }}</td><td>{{ t.tier }}</td>
              <!-- v6.3.1 R4-a：状态中文映射（class 复用既有 .badge.* 四色，文字中文） -->
              <td><span class="badge" :class="taskState(t)[0]">{{ taskState(t)[1] }}</span></td>
              <td class="num lake-task-progress">
                <div class="progress-track" style="margin:0"><div class="progress-bar" :style="{ width: (t.total ? Math.round((t.done / t.total) * 100) : 0) + '%' }"></div></div>
                {{ (t.done ?? 0) + "/" + (t.total ?? 0) }}
              </td>
              <td class="num">{{ t.eta_min ?? "—" }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 搜索（防抖 250ms + 下拉；409 backfill → 中性处理不弹红横幅） -->
    <div class="lake-toolbar card">
      <LakeSearchBar @select="onSelect" @error="(m) => (lake.lastError = m)" />
    </div>

    <!-- 个股全景（未选股 → 占位；搜索/行点击联动） -->
    <StockPanoramaCard ref="stockCardEl" :code="stockCode" @error="(m) => (lake.lastError = m)" />

    <!-- 全市场浏览（服务端分页+筛选；行点击→全景卡+scrollIntoView） -->
    <MarketTable @select="onSelect" @error="(m) => (lake.lastError = m)" />

    <!-- 数据库状态 / 补齐进度（SyncControl + SummaryBar + SourcePoolPanel + 9表 + 视图 + tasks） -->
    <LakeStatusCard />
  </section>
</template>
