// 加密货币前端 Vite 配置：base:'/crypto/'（443 单域名路径分流，见 docs/architecture.md），
// 产物 ../dist（不入库）；dev :5175 经 proxy 打 :9091（/crypto 前缀透传，后端原生带前缀）。
// 与股票前端（web/frontend，:5173→:9090）完全独立构建。
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  base: "/crypto/",
  plugins: [vue()],
  build: {
    outDir: "../dist",
    emptyOutDir: true,
  },
  server: {
    port: 5175,
    proxy: {
      "/crypto": "http://127.0.0.1:9091",
    },
  },
  test: {
    environment: "happy-dom",
  },
});
