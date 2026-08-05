// chat.spec（docs/11 §4.3）：流式对话 react + 同会话多轮累积。
// FAKE 确定性锚点：示例问题（SAMPLE_QUESTIONS[0]，空态一键触发）→ fake 脚本必出
// calculator 工具卡 → “答案是 14。” + “结论”标签；第二轮后侧栏仍 1 个会话、timeline 2 轮。
import { expect, test } from "@playwright/test";
import { CALC_QUESTION, freshUserId, gotoAsUser, sendMessage, waitTurnDone } from "./helpers";

test("示例问题走完流式一轮：calculator 工具卡 + 答案 + 结论", async ({ page }) => {
  await gotoAsUser(page, freshUserId());

  // 空态 Hero 文案在，然后直接输入发送（建议 chips 已换成展示题，不再含 CALC）
  await expect(page.getByText("有什么可以帮上忙？")).toBeVisible();
  await sendMessage(page, CALC_QUESTION);

  // 流式过程与终态（FAKE 确定性）：紧凑工具行（mono chip 含工具名原文）→ 结论卡
  await expect(page.getByTestId("tool-row").filter({ hasText: "calculator" }).first()).toBeVisible({
    timeout: 30_000,
  });
  await waitTurnDone(page, 1);
  await expect(page.getByText("答案是 14。").first()).toBeVisible();
});

test("同会话第二轮：侧栏仍 1 个会话，timeline 累积 2 轮", async ({ page }) => {
  await gotoAsUser(page, freshUserId());
  await sendMessage(page, CALC_QUESTION);
  await waitTurnDone(page, 1);

  // 第二轮（fake 脚本对任意问题给同一答案，用不同文案区分两个用户气泡）
  const q2 = "第二轮：请再算一次";
  await sendMessage(page, q2);
  await waitTurnDone(page, 2);

  // timeline 2 轮：两个用户气泡都在
  await expect(page.getByText(CALC_QUESTION, { exact: true }).first()).toBeVisible();
  await expect(page.getByText(q2, { exact: true })).toBeVisible();

  // 侧栏仍只有 1 个会话（以“删除会话”按钮计数会话行），轮数徽标为 2 轮
  await expect(page.getByTitle("删除会话")).toHaveCount(1);
  await expect(page.getByText("2 轮")).toBeVisible();
});
