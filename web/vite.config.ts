import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// 控制面地址：默认 :8080，可用 VITE_BACKEND 覆盖。
const backend = process.env.VITE_BACKEND || "http://localhost:8080";

// 把面向后端的路径代理到控制面 → 前端全用相对路径，浏览器视角同源，零 CORS。
// SSE 走流式透传（Vite 默认不缓冲）。
const proxy = {
  "/runs": { target: backend, changeOrigin: true },
  "/sessions": { target: backend, changeOrigin: true },
  "/uploads": { target: backend, changeOrigin: true },
  "/kb": { target: backend, changeOrigin: true },
  "/stats": { target: backend, changeOrigin: true },
  "/schedules": { target: backend, changeOrigin: true },
  "/artifacts": { target: backend, changeOrigin: true },
  "/healthz": { target: backend, changeOrigin: true },
};

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // shadcn copy-in 组件用 @/* 引用；与 tsconfig paths 保持一致（vitest 共用本配置）。
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  // echarts 只被 EChartsPreview 懒加载：单独分包，主包体积不受影响、图表包可长缓存。
  build: { rollupOptions: { output: { manualChunks: { echarts: ["echarts"] } } } },
  server: { port: 5173, proxy },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});
