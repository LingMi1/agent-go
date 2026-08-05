import { defineConfig, devices } from "@playwright/test";

// E2E 配置（docs/11 §4）：被测栈 = FAKE 全家桶 + 独立 my_agent_test 库 + 单端口 :18080
// （Go WEB_DIR 托管 build 后的 dist，prod 形态顺带被测）。dev 联调可
// `BASE_URL=http://localhost:5173 npx playwright test` 指向 Vite（webServer 会因 url 已活而复用）。
const BASE_URL = process.env.BASE_URL || "http://localhost:18080";

export default defineConfig({
  testDir: "./e2e", // 避开 vite.config.ts 的 vitest glob（src/**/*.test.ts）
  outputDir: "./e2e/.results",
  // 共库（PG/MinIO/Qdrant 复用 dev 容器）+ fake 模型是进程级脚本状态 → 串行最稳（登记为取舍：
  // 数据按一次性 e2e-<随机> userId 天然隔离，但 SSE/信号量/scheduler 是共享面，稳态优先）。
  fullyParallel: false,
  workers: 1,
  // retry=1 只为暴露 flake（报告会标记 "flaky"，两遍全绿才算过）并在重试时抓 trace 定位；
  // 不用它掩盖不稳用例——用例本身一律 web-first assertion + expect.poll，零 hardcoded sleep。
  retries: 1,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  use: {
    baseURL: BASE_URL,
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      // 视口固定 1440×900：产物 dock/Files 面板在 <lg(1024px) 隐藏（docs/11 §4.3 风险③）。
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
    },
  ],
  webServer: {
    command: "bash e2e/stack.sh",
    url: `${BASE_URL}/healthz`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    stdout: "ignore",
    stderr: "pipe",
  },
});
