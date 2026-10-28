<script setup>
// 应用根（v7.0.14）：登录门禁 —— 未登录渲染 LoginView，登录后渲染 AppShell。
//
// 流程：
// - 启动 → authStore.check()（GET /api/auth/me，cookie 有效则直接进主页）；
//   checked=false 期间显示加载态（防登录页一闪而过）。
// - 登录成功 → user 置位 → AppShell 挂载（页签首次发请求）；watch user 恢复
//   回测任务 SSE 监控（登录页期间不 restore，避免未登录 401 风暴）。
// - 运行中任何 API 401（会话过期）→ client.js 发 auth:401 事件 → 回登录页。
import { onBeforeUnmount, onMounted, watch } from "vue";
import AppShell from "./components/AppShell.vue";
import LoginView from "./views/LoginView.vue";
import SystemChoiceView from "./views/SystemChoiceView.vue";
import { useAuthStore } from "./stores/authStore.js";
import { useTaskStore } from "./stores/taskStore.js";

const auth = useAuthStore();
const task = useTaskStore();

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

// 登录（含刷新后 cookie 有效直进）→ 恢复活动回测任务监控
watch(() => auth.user, (u) => {
  if (u) task.restore();
});
</script>

<template>
  <div v-if="!auth.checked" class="auth-loading">加载中…</div>
  <LoginView v-else-if="!auth.user" />
  <SystemChoiceView v-else-if="auth.justLoggedIn" />
  <AppShell v-else />
</template>

<style>
.auth-loading {
  min-height: 100vh; display: flex; align-items: center; justify-content: center;
  color: #999; font-size: 14px; background: #f5f6fa;
}
</style>
