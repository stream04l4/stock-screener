<script setup>
// 应用根（加密货币系统）：登录门禁 —— 未登录渲染 LoginView（双入口），
// 登录后渲染 AppShell（四 tab 骨架）。
// 与股票系统（web/frontend/src/App.vue）同款机制，独立部署单元。
import { onBeforeUnmount, onMounted } from "vue";
import AppShell from "./components/AppShell.vue";
import LoginView from "./views/LoginView.vue";
import { useAuthStore } from "./stores/authStore.js";

const auth = useAuthStore();

function on401() {
  auth.onUnauthorized();
}

onMounted(() => {
  window.addEventListener("auth:401", on401);
  auth.check();
});
onBeforeUnmount(() => {
  window.removeEventListener("auth:401", on401);
});
</script>

<template>
  <div v-if="!auth.checked" class="auth-loading">加载中…</div>
  <LoginView v-else-if="!auth.user" />
  <AppShell v-else />
</template>

<style>
.auth-loading {
  min-height: 100vh; display: flex; align-items: center; justify-content: center;
  color: #999; font-size: 14px; background: #f5f6fa;
}
</style>
