// 结果 tab（报告 §3 T-结果）：左栏 RunList + 右栏 RunDetail。
// - liveQuery('runs') TTL 30s；手动 ⟳ = invalidate('runs')（事件触发刷新）。
// - 行点击 → 选中运行日 → RunDetail 按 `run:${day}` 会话缓存拉取。
<script setup>
import { ref } from "vue";
import { invalidate } from "../../live/queryRegistry.js";
import RunList from "./RunList.vue";
import RunDetail from "./RunDetail.vue";

const emit = defineEmits(["open-stock"]);

const selected = ref(null); // 当前选中运行日（ISO）；null = 尚未加载/无结果

function onSelect(day) {
  if (day && day !== selected.value) selected.value = day;
}

function onRefresh() {
  invalidate("runs"); // ⟳ 手动刷新（报告 §2.1 事件触发）
}
</script>

<template>
  <div class="results-layout">
    <aside class="run-list-box">
      <h3>运行记录 <button id="btn-refresh-runs" class="mini-btn" title="刷新" @click="onRefresh">⟳</button></h3>
      <RunList :selected="selected" @select="onSelect" />
    </aside>
    <div class="run-detail">
      <RunDetail v-if="selected" :day="selected" @open-stock="(code, day) => emit('open-stock', code, day)" />
      <p v-else class="placeholder">← 从左侧选择一次运行查看详情</p>
    </div>
  </div>
</template>
