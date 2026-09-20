<script setup>
// 应用根：AppShell（4 页签壳，运行/结果已合并）+ 全局 StockModal（行点击弹框，跨 tab 复用）。
import { onMounted, ref } from "vue";
import AppShell from "./components/AppShell.vue";
import StockModal from "./components/StockModal.vue";
import { useTaskStore } from "./stores/taskStore.js";

const modalCode = ref(null);
const modalRunDay = ref(null);

function onOpenStock(code, runDay) {
  modalCode.value = code;
  modalRunDay.value = runDay;
}

// D2 跨页签核心：SSE 生命周期归 taskStore（全局），页面加载即恢复活动任务监控——
// 用户刷新/打开时若任务在跑（锁文件在）→ 默认落在结果 tab 也能续接监控，
// 完成时 toast + invalidate('runs') 无需先切到运行页。
onMounted(() => { useTaskStore().restore(); });
</script>

<template>
  <AppShell @open-stock="onOpenStock" />
  <StockModal :code="modalCode" :run-day="modalRunDay" @close="modalCode = null" />
</template>
