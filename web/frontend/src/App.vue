<script setup>
// 应用根（v7.0）：AppShell（4 页签壳）。
// SSE 生命周期归 taskStore（全局）：页面加载即恢复活动回测任务监控——
// 用户刷新/打开时若任务在跑（锁文件在）→ 续接监控，完成时 toast 无需先切到回测页。
import { onMounted } from "vue";
import AppShell from "./components/AppShell.vue";
import { useTaskStore } from "./stores/taskStore.js";

onMounted(() => { useTaskStore().restore(); });
</script>

<template>
  <AppShell />
</template>
