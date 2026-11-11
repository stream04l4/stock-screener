// 回测（框架占位，v7.0.43）：后端 /api/backtest ready=false → 空态卡片。
// 后续：基于湖内 1h K 线的策略回测（引擎/报告），参照股票侧回测页签。
<script setup>
import { onMounted, ref } from "vue";
import { api } from "../api/client.js";

const info = ref(null);
const err = ref("");
onMounted(async () => {
  try {
    info.value = await api("/api/backtest");
  } catch (e) {
    err.value = String((e && e.message) || e);
  }
});
</script>

<template>
  <div class="card">
    <h2>回测</h2>
    <p v-if="err" class="err">{{ err }}</p>
    <div v-else-if="info && info.ready === false" class="empty">
      <p class="empty-icon">🪙📊</p>
      <p class="empty-title">框架已搭，功能待完善</p>
      <p class="empty-sub">{{ info.detail }}</p>
    </div>
  </div>
</template>

<style scoped>
.card { background: #fff; border: 1px solid #d8d8e0; border-radius: 12px; padding: 20px; }
h2 { margin: 0 0 12px; font-size: 16px; }
.err { color: #b42318; font-size: 13px; }
.empty { text-align: center; padding: 40px 0; color: #999; }
.empty-icon { font-size: 32px; margin: 0; }
.empty-title { font-size: 14px; color: #666; margin: 10px 0 4px; }
.empty-sub { font-size: 12px; margin: 0; }
</style>
