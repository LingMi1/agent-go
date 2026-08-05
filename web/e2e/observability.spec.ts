// observability.spec（docs/11 §4.3）：/metrics 冒烟——跑一轮对话后指标可见且计数>0，
// 与 Prometheus 特性互证（run 指标埋在 dispatch.Run 单收口，HTTP 计数在 api 中间件）。
import { expect, test } from "@playwright/test";
import { CALC_QUESTION, freshUserId, gotoAsUser, sendMessage, waitTurnDone } from "./helpers";

/** 汇总某指标名（含 label 组合）在 exposition 文本中的样本值之和。 */
function sumMetric(text: string, name: string): number {
  const re = new RegExp(`^${name}(?:\\{[^}]*\\})?\\s+([0-9.eE+-]+)$`, "gm");
  let sum = 0;
  for (const m of text.matchAll(re)) sum += Number(m[1]);
  return sum;
}

test("跑一轮对话后 /metrics 含 runs_total 与 http_requests_total 且计数>0", async ({ page }) => {
  await gotoAsUser(page, freshUserId());
  await sendMessage(page, CALC_QUESTION);
  await waitTurnDone(page, 1);

  // FinishRun 落账与 SSE 收尾同批发生，用 expect.poll 兜残余竞态（不 sleep）
  await expect
    .poll(
      async () => {
        const res = await page.request.get("/metrics");
        if (!res.ok()) return -1;
        const text = await res.text();
        // 终态计数：至少 1 个 SUCCESS run（本测试刚跑完的这轮）
        const success = new RegExp(
          '^myagent_runs_total\\{[^}]*status="SUCCESS"[^}]*\\}\\s+([0-9.]+)$',
          "m",
        ).exec(text);
        return success ? Number(success[1]) : 0;
      },
      { timeout: 15_000 },
    )
    .toBeGreaterThan(0);

  const res = await page.request.get("/metrics");
  expect(res.ok()).toBe(true);
  const text = await res.text();
  expect(text).toContain("myagent_runs_total");
  expect(text).toContain("myagent_http_requests_total");
  expect(sumMetric(text, "myagent_runs_total")).toBeGreaterThan(0);
  expect(sumMetric(text, "myagent_http_requests_total")).toBeGreaterThan(0);
});
