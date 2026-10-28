// authStore —— 登录态（v7.0.14）。
//
// 机制：GET /api/auth/me 探活（cookie 有效 → user）；登录 = POST /api/login
// （成功写 HttpOnly cookie，浏览器自动携带）；任何 API 401 → App.vue 监听
// auth:401 事件调 onUnauthorized() 强制回登录页（会话过期兜底）。
import { defineStore } from "pinia";
import { api } from "../api/client.js";

export const useAuthStore = defineStore("auth", {
  state: () => ({
    user: null,   // 登录后的用户名；null = 未登录
    checked: false, // 启动探活完成前 App.vue 显示加载态
    loginErr: "",  // 登录失败文案（登录页显示）
  }),
  actions: {
    async check() {
      try {
        const d = await api("/api/auth/me");
        this.user = d.user;
      } catch {
        this.user = null;
      } finally {
        this.checked = true;
      }
    },
    async login(username, password) {
      this.loginErr = "";
      try {
        const d = await api("/api/login", {
          method: "POST",
          body: JSON.stringify({ username, password }),
        });
        this.user = d.user;
        return true;
      } catch (e) {
        this.loginErr = String((e && e.message) || e);
        return false;
      }
    },
    async logout() {
      try { await api("/api/logout", { method: "POST" }); } catch { /* 兜底：客户端照样退出 */ }
      this.user = null;
    },
    // 任何受保护 API 返回 401（会话过期/被踢）→ 回登录页
    onUnauthorized() { this.user = null; },
  },
});
