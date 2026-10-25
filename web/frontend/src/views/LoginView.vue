// 登录页（v7.0.14）：用户名 + 密码 → POST /api/login → cookie 会话 → 跳回主页（AppShell）。
// - 失败 → 行内错误文案（401 用户名或密码错误 / 429 锁定提示）；
// - 样式自包含（.login-*），移动端 320px 可用。
<script setup>
import { reactive, ref } from "vue";
import { useAuthStore } from "../stores/authStore.js";

const auth = useAuthStore();
const form = reactive({ username: "", password: "" });
const busy = ref(false);

async function doLogin() {
  if (!form.username.trim() || !form.password) {
    auth.loginErr = "请输入用户名和密码";
    return;
  }
  busy.value = true;
  try {
    await auth.login(form.username.trim(), form.password);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="login-wrap">
    <form class="login-card" @submit.prevent="doLogin">
      <h1 class="login-title">📉 深回撤金字塔加仓策略</h1>
      <p class="login-sub">A股深回撤加仓单策略 · 数据湖驱动 · 登录后查看</p>
      <label class="login-field">
        <span>用户名</span>
        <input v-model="form.username" autocomplete="username" placeholder="admin" />
      </label>
      <label class="login-field">
        <span>密码</span>
        <input v-model="form.password" type="password" autocomplete="current-password"
               placeholder="••••••••" @keyup.enter="doLogin" />
      </label>
      <p v-if="auth.loginErr" class="login-err">{{ auth.loginErr }}</p>
      <button type="submit" class="login-btn" :disabled="busy">
        {{ busy ? "登录中…" : "登 录" }}
      </button>
    </form>
  </div>
</template>

<style scoped>
.login-wrap {
  min-height: 100vh; display: flex; align-items: center; justify-content: center;
  padding: 16px; background: #f5f6fa;
}
.login-card {
  width: 100%; max-width: 340px; box-sizing: border-box;
  background: #fff; border: 1px solid var(--line, #d8d8e0); border-radius: 12px;
  padding: 28px 24px; box-shadow: 0 4px 16px rgba(0, 0, 0, 0.06);
}
.login-title { font-size: 18px; margin: 0 0 4px; }
.login-sub { color: #888; font-size: 12px; margin: 0 0 20px; }
.login-field { display: block; font-size: 13px; color: #555; margin-bottom: 14px; }
.login-field input {
  display: block; width: 100%; box-sizing: border-box; margin-top: 6px;
  padding: 8px 10px; font-size: 14px;
  border: 1px solid var(--line, #d8d8e0); border-radius: 8px;
}
.login-err { color: #b42318; font-size: 13px; margin: 0 0 10px; }
.login-btn {
  width: 100%; padding: 9px 0; font-size: 14px; letter-spacing: 4px;
  border: none; border-radius: 8px; background: #14783c; color: #fff; cursor: pointer;
}
.login-btn:hover:not(:disabled) { background: #0f5c2e; }
.login-btn:disabled { opacity: 0.6; cursor: default; }
</style>
