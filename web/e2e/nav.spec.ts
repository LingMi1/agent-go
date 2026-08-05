// nav.spec（docs/11 §4.3）：侧栏导航中枢——五视图切换与高亮；ChatView hidden 不卸载。
// ChatView 永久挂载、非 chat 视图仅 CSS hidden（App.tsx），断言必须用 toBeVisible/toBeHidden
// 而非 toBeAttached（docs/11 §4.3 风险、地图风险④）。
import { expect, test } from "@playwright/test";
import { CALC_QUESTION, freshUserId, gotoAsUser, sendMessage, waitTurnDone } from "./helpers";

test("五导航切换与高亮；切走再切回对话历史仍在（hidden 不卸载）", async ({ page }) => {
  await gotoAsUser(page, freshUserId());

  // 先造 1 轮对话内容，供“切回仍在”断言
  await sendMessage(page, CALC_QUESTION);
  await waitTurnDone(page, 1);
  const conclusion = page.getByText("结论", { exact: true });

  const nav = (label: string) => page.getByRole("button", { name: label, exact: true });

  // 生图
  await nav("生图").click();
  await expect(page.getByRole("heading", { name: "生图工作区" })).toBeVisible();
  await expect(nav("生图")).toHaveClass(/bg-accent/);
  await expect(conclusion).toBeHidden(); // ChatView 被 hidden 盖住但未卸载

  // 产物
  await nav("产物").click();
  await expect(page.getByRole("heading", { name: "产物" })).toBeVisible();
  await expect(nav("产物")).toHaveClass(/bg-accent/);

  // 知识库
  await nav("知识库").click();
  await expect(page.getByText("个人知识库（跨会话可检索）")).toBeVisible();
  await expect(nav("知识库")).toHaveClass(/bg-accent/);

  // 定时任务
  await nav("定时任务").click();
  await expect(page.getByText("定时任务（Proactive）")).toBeVisible();
  await expect(nav("定时任务")).toHaveClass(/bg-accent/);

  // 切回对话：历史立即可见（未卸载、无需重新载入），会话列表也只在对话视图显示
  await nav("对话").click();
  await expect(nav("对话")).toHaveClass(/bg-accent/);
  await expect(conclusion).toBeVisible();
  await expect(page.getByText("答案是 14。").first()).toBeVisible();
  await expect(page.getByText("最近对话")).toBeVisible();
});
