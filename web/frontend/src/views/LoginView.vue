// 登录页（v7.0.43）：双系统统一登录入口 —— 同一账号密码，两个按钮分别登录进两个系统。
// - 📉 登录 A股4维选股：POST /api/login → cookie → 本站 AppShell（表单默认/回车触发）；
// - 🪙 登录 加密货币：同样 POST /api/login（ssc_session cookie 按 host 跨端口共享）
//   → 成功后整页跳转 :9091，加密货币系统凭同一 cookie 免登；
// - v7.0.42 曾解耦为单入口，v7.0.43 按 Joel 定案恢复双入口（加密货币系统重建）；
// - 失败 → 行内错误文案（401 用户名或密码错误 / 429 锁定提示）；
// - 样式自包含（.login-*），移动端 320px 可用。
<script setup>
import { reactive, ref } from "vue";
import { useAuthStore } from "../stores/authStore.js";

const auth = useAuthStore();
const form = reactive({ username: "", password: "" });
const busy = ref(false);
// 加密货币入口 = 同域名 /crypto/ 路径（443 单域名架构；同域 cookie 跳转免登）
const cryptoUrl = window.location.origin + "/crypto/";

async function doLogin(target) {
  if (!form.username.trim() || !form.password) {
    auth.loginErr = "请输入用户名和密码";
    return;
  }
  busy.value = true;
  try {
    const ok = await auth.login(form.username.trim(), form.password);
    if (ok && target === "crypto") {
      // cookie 已由 /api/login 写入浏览器 → 整页跳转，加密货币系统免登
      window.location.href = cryptoUrl;
    }
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="login-wrap">
    <form class="login-card" @submit.prevent="doLogin('stock')">
      <h1 class="login-title">📉🪙 双系统统一登录</h1>
      <p class="login-sub">A股4维选股 · 加密货币 · 同一账号密码，选择要进入的系统</p>
      <label class="login-field">
        <span>用户名</span>
        <input v-model="form.username" autocomplete="username" placeholder="admin" />
      </label>
      <label class="login-field">
        <span>密码</span>
        <input v-model="form.password" type="password" autocomplete="current-password"
               placeholder="••••••••" @keyup.enter="doLogin('stock')" />
      </label>
      <p v-if="auth.loginErr" class="login-err">{{ auth.loginErr }}</p>
      <button type="submit" class="login-btn login-btn-stock" :disabled="busy">
        📉 {{ busy ? "登录中…" : "登录 A股4维选股" }}
      </button>
      <button type="button" class="login-btn login-btn-crypto" :disabled="busy" @click="doLogin('crypto')">
        🪙 {{ busy ? "登录中…" : "登录 加密货币" }}
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
  width: 100%; padding: 9px 0; font-size: 14px; letter-spacing: 2px;
  border: none; border-radius: 8px; color: #fff; cursor: pointer;
}
.login-btn + .login-btn { margin-top: 8px; }
.login-btn-stock { background: #14783c; }
.login-btn-stock:hover:not(:disabled) { background: #0f5c2e; }
.login-btn-crypto { background: #9a6b00; }
.login-btn-crypto:hover:not(:disabled) { background: #7a5500; }
.login-btn:disabled { opacity: 0.6; cursor: default; }
</style>
