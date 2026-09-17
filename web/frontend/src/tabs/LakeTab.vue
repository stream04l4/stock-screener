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

// 红横幅文案（vanilla lakeSetError："数据湖不可用：msg"）
const errMsg = computed(() => (lake.lastError ? "数据湖不可用：" + lake.lastError : ""));
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
      <div class="tbl-wrap">
        <table class="data" id="lake-backfill-tasks">
          <thead><tr><th>表</th><th>层级</th><th>状态</th><th class="num">进度</th><th class="num">配额(今日)</th><th class="num">ETA(min)</th></tr></thead>
          <tbody>
            <tr v-if="!lake.backfillView.tasks || !lake.backfillView.tasks.length">
              <td colspan="6" class="placeholder">暂无后台补齐任务</td>
            </tr>
            <tr v-for="(t, i) in lake.backfillView.tasks || []" :key="i">
              <td>{{ t.table }}</td><td>{{ t.tier }}</td>
              <td><span class="badge" :class="t.state || 'idle'">{{ t.state || "idle" }}</span></td>
              <td class="num lake-task-progress">
                <div class="progress-track" style="margin:0"><div class="progress-bar" :style="{ width: (t.total ? Math.round((t.done / t.total) * 100) : 0) + '%' }"></div></div>
                {{ (t.done ?? 0) + "/" + (t.total ?? 0) }}
              </td>
              <td class="num">{{ (t.quota_used_today ?? 0) + "/" + (t.quota_budget ?? "—") }}</td>
              <td class="num">{{ t.eta_min ?? "—" }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 搜索（防抖 250ms + 下拉；409 backfill → 中性处理不弹红横幅） -->
    <div class="lake-toolbar card">
      <LakeSearchBar @select="onSelect" @error="(m) => (lake.lastError = '数据湖不可用：' + m)" />
    </div>

    <!-- 个股全景（未选股 → 占位；搜索/行点击联动） -->
    <StockPanoramaCard ref="stockCardEl" :code="stockCode" @error="(m) => (lake.lastError = '数据湖不可用：' + m)" />

    <!-- 全市场浏览（服务端分页+筛选；行点击→全景卡+scrollIntoView） -->
    <MarketTable @select="onSelect" @error="(m) => (lake.lastError = '数据湖不可用：' + m)" />

    <!-- 数据库状态 / 补齐进度（SyncControl + SummaryBar + SourcePoolPanel + 9表 + 视图 + tasks） -->
    <LakeStatusCard />
  </section>
</template>
