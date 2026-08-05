// workspace.spec：智能体工作区右 dock——手动打开、动态/文件双 tab、空态与关闭。
// 视口 1440×900（playwright.config 全局），dock 仅 lg+ 渲染。
import { expect, test } from "@playwright/test";
import { CALC_QUESTION, freshUserId, gotoAsUser, sendMessage, waitTurnDone } from "./helpers";

test("打开工作区：动态/文件双 tab 可切换，calculator 轮无产物显空态", async ({ page }) => {
  await gotoAsUser(page, freshUserId());
  await sendMessage(page, CALC_QUESTION);
  await waitTurnDone(page, 1);

  // calculator 无产物 → dock 不自动开，走右上角开关
  await page.getByRole("button", { name: "打开智能体工作区" }).click();
  await expect(page.getByTestId("workspace-tabs")).toBeVisible();
  await expect(page.getByText("智能体工作区")).toBeVisible();

  // 动态 tab 空态
  await expect(page.getByText("运行产物与搜索来源会实时出现在这里")).toBeVisible();

  // 文件 tab：无产物无上传 → 空态文案
  await page.getByRole("tab", { name: "文件" }).click();
  await expect(page.getByText("本对话的产物与上传的文件")).toBeVisible();

  // 切回动态
  await page.getByRole("tab", { name: "动态" }).click();
  await expect(page.getByText("运行产物与搜索来源会实时出现在这里")).toBeVisible();
});
