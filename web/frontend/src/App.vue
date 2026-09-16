<script setup>
// 应用根：AppShell（5 页签壳）+ 全局 StockModal（行点击弹框，跨 tab 复用）。
import { ref } from "vue";
import AppShell from "./components/AppShell.vue";
import StockModal from "./components/StockModal.vue";

const modalCode = ref(null);
const modalRunDay = ref(null);

function onOpenStock(code, runDay) {
  modalCode.value = code;
  modalRunDay.value = runDay;
}
</script>

<template>
  <AppShell @open-stock="onOpenStock" />
  <StockModal :code="modalCode" :run-day="modalRunDay" @close="modalCode = null" />
</template>
