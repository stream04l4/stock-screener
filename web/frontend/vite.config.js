// Vite 配置（D1）：base:'/'，产物 ../dist（不入库）；dev :5173 经 proxy 打生产 :9090。
// echarts 走 npm 依赖（锁 5.6.0 = 现 vendor 版本），StockModal 内动态 import → 独立 chunk。
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  base: "/",
  plugins: [vue()],
  build: {
    outDir: "../dist",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:9090",
    },
  },
  test: {
    environment: "happy-dom",
  },
});
