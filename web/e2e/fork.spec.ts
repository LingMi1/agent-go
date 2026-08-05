// fork.spec（docs/11 §4.3 + docs/14）：会话分叉（时间旅行）。
// 2 轮对话 → 第 1 轮 hover 出“从此轮分叉”（data-testid=fork-turn，仅已终态轮）→
// 新会话：侧栏出现分叉标记（title=分叉会话）→ timeline 含“继承”角标 + fork-divider
// 分界线，且只继承到分叉点（第 2 轮不在）→ 续聊成功（fork 播种后的新轮正常走完）。
import { expect, test } from "@playwright/test";
import { CALC_QUESTION, freshUserId, gotoAsUser, sendMessage, waitTurnDone } from "./helpers";

test("从第 1 轮分叉出新会话：继承投影 + 分界线 + 可续聊", async ({ page }) => {
  await gotoAsUser(page, freshUserId());

  // 造 2 轮历史
  await sendMessage(page, CALC_QUESTION);
  await waitTurnDone(page, 1);
  const q2 = "第二轮：这轮不该被继承";
  await sendMessage(page, q2);
  await waitTurnDone(page, 2);

  // 分叉锚点只在 timeline 已终态轮上（此刻 timeline=[轮1]，轮2 还在 live 区）
  await page.getByText(CALC_QUESTION, { exact: true }).first().hover();
  const fork = page.getByTestId("fork-turn");
  await expect(fork).toHaveCount(1);
  await fork.click();

  // 登记成功 → 自动切入新会话
  await expect(page.getByText("已从该轮分叉出新会话")).toBeVisible();

  // 侧栏：两个会话，新会话带分叉标记（GitBranch icon 的 title）
  await expect(page.getByTitle("删除会话")).toHaveCount(2, { timeout: 15_000 });
  await expect(page.getByTitle("分叉会话")).toHaveCount(1);

  // 新会话 timeline：继承轮 1（角标“继承”）+ 分界线；只继承到分叉点——轮 2 不在
  await expect(page.getByText("继承", { exact: true })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId("fork-divider")).toBeVisible();
  await expect(page.getByText("结论", { exact: true })).toHaveCount(1);
  await expect(page.getByText(CALC_QUESTION, { exact: true }).first()).toBeVisible();
  await expect(page.getByText(q2, { exact: true })).toHaveCount(0);

  // 续聊：分叉会话第一条消息触发认知面 checkpoint 播种，新轮正常走到终态
  const q3 = "分叉后的第一问";
  await sendMessage(page, q3);
  await waitTurnDone(page, 2); // 继承轮 1 + 新轮 = 2 个结论
  // 分叉会话标题取首条 own run 的 query（=q3，E2E 首跑实测）——侧栏行与聊天气泡撞文案，
  // 断言气泡须把范围锁到主内容区（aside 之外）。
  await expect(page.locator("aside + div").getByText(q3, { exact: true })).toBeVisible();
  await expect(page.getByTestId("fork-divider")).toBeVisible(); // 分界线仍在继承轮与新轮之间
});
