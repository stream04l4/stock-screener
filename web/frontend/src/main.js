// SPA 入口：createApp + Pinia；全局样式 = 旧 style.css 原样迁入（src/assets/app.css）。
import { createApp } from "vue";
import { createPinia } from "pinia";
import App from "./App.vue";
import "./assets/app.css";

const app = createApp(App);
app.use(createPinia());
app.mount("#app");
