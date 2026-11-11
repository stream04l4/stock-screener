// authStore（加密货币系统）—— 与股票系统同款登录态机制。
// ssc_session cookie 按 hostname 跨端口共享：股票 :9090 登录后直达本站免登。
import { defineStore } from "pinia";
import { api } from "../api/client.js";

export const useAuthStore = defineStore("auth", {
  state: () => ({
    user: null,
    checked: false,
    loginErr: "",
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
      try { await api("/api/logout", { method: "POST" }); } catch { /* 客户端照样退出 */ }
      this.user = null;
    },
    onUnauthorized() { this.user = null; },
  },
});
