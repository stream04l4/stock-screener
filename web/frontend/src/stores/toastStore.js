// toastStore —— 顶栏全局状态条（现 #global-status 的 Pinia 化）。
// 移植 app.js toast()：msg + ok 标志，6s 后自动清空。
import { defineStore } from "pinia";

export const useToastStore = defineStore("toast", {
  state: () => ({
    msg: "",
    ok: true,
    _timer: null,
  }),
  actions: {
    toast(msg, ok = true) {
      this.msg = (ok ? "✓ " : "✗ ") + msg;
      this.ok = !!ok;
      if (this._timer) clearTimeout(this._timer);
      this._timer = setTimeout(() => { this.msg = ""; }, 6000);
    },
  },
});
