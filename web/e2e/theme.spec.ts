// theme.spec：双主题切换与持久化。浅色为默认（html 无 .dark）；
// 侧栏 theme-toggle 切换 → html.dark 挂上 → localStorage 持久化 → 刷新后仍暗色 → 再切回。
import { expect, test } from "@playwright/test";
import { freshUserId, gotoAsUser } from "./helpers";

test("默认浅色；切换暗色持久化，刷新不丢；可切回浅色", async ({ page }) => {
  await gotoAsUser(page, freshUserId());

  const html = page.locator("html");
  await expect(html).not.toHaveClass(/dark/);

  await page.getByTestId("theme-toggle").click();
  await expect(html).toHaveClass(/dark/);

  // 刷新：index.html 防闪烁脚本按 my-agent.ui 预挂 class
  await page.reload();
  await expect(page.getByRole("button", { name: "新对话" })).toBeVisible();
  await expect(html).toHaveClass(/dark/);

  await page.getByTestId("theme-toggle").click();
  await expect(html).not.toHaveClass(/dark/);
});
