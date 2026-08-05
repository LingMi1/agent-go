// sessions.spec（docs/11 §4.3）：刷新恢复 / 续聊 / 两步删除。
// reload 后 App 自动进入最近会话并回放整段历史（timeline 仍 2 轮），Composer 可直接续聊；
// 删除是两步确认（3s 窗口内连点，Sidebar 风险⑤），删的是自己 owner 的数据。
import { expect, test } from "@playwright/test";
import { CALC_QUESTION, composer, freshUserId, gotoAsUser, sendMessage, waitTurnDone } from "./helpers";

test("刷新恢复 2 轮历史并可续聊，两步删除后会话消失", async ({ page }) => {
  await gotoAsUser(page, freshUserId());

  // 先造 2 轮历史
  await sendMessage(page, CALC_QUESTION);
  await waitTurnDone(page, 1);
  const q2 = "第二轮：换个说法再算一次";
  await sendMessage(page, q2);
  await waitTurnDone(page, 2);

  // —— 刷新恢复：自动进入最近会话，timeline 回放 2 轮 ——
  await page.reload();
  await expect(page.getByText("结论", { exact: true })).toHaveCount(2, { timeout: 30_000 });
  await expect(page.getByText(CALC_QUESTION, { exact: true }).first()).toBeVisible();
  await expect(page.getByText(q2, { exact: true })).toBeVisible();

  // —— 续聊：载入完成后 Composer 可输入，第三轮正常走完 ——
  const q3 = "第三轮：刷新后继续对话";
  await sendMessage(page, q3);
  await waitTurnDone(page, 3);
  await expect(page.getByText("3 轮")).toBeVisible(); // 侧栏轮数徽标同步

  // —— 两步删除：第一次点进入确认态，3s 窗口内再点才真删 ——
  await expect(page.getByTitle("删除会话")).toHaveCount(1);
  await page.getByText(CALC_QUESTION, { exact: true }).first().hover(); // 悬停会话区（气泡即可）
  const row = page.getByTitle("删除会话");
  await row.hover();
  await row.click();
  const confirmBtn = page.getByTitle("再点一次确认删除");
  await expect(confirmBtn).toBeVisible();
  await confirmBtn.click();

  // 会话消失，视图退回空态
  await expect(page.getByText("已删除会话")).toBeVisible();
  await expect(page.getByTitle("删除会话")).toHaveCount(0);
  await expect(page.getByText("有什么可以帮上忙？")).toBeVisible();
  await expect(composer(page)).toBeEnabled();
});
