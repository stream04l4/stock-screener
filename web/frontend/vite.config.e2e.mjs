// D2 E2E 专用 vite 配置：/api 代理到 mock 反代 :9091（除 mock 任务端点外透传 :9090）。
// 端口 5174，与默认 dev server（:5173 → :9090）并存；测试完即 kill。
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  base: "/",
  plugins: [vue()],
  server: {
    port: 5174,
    strictPort: true,
    proxy: {
      "/api": "http://127.0.0.1:9091",
    },
  },
});
