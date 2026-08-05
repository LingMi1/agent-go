// auth.spec（docs/17 §6）：D3 密码登录/注册 + 身份头。
// 锚点 = 登录页 data-testid（login-card/username/password/submit/toggle）。
// 注册换发 token → 请求同时携带 X-User-Id（owner 兜底）+ Authorization: Bearer（D3）。
import { expect, test } from "@playwright/test";
import { freshUserId } from "./helpers";

test("注册后请求携带 X-User-Id + Bearer，登出回到登录页", async ({ page }) => {
  const userId = freshUserId();
  const password = "secret123";
  await page.goto("/");

  // 登录页形态：切换到注册模式（fresh 用户名，避免 409）。
  await expect(page.getByTestId("login-card")).toBeVisible();
  await expect(page.getByTestId("login-submit")).toBeDisabled(); // 空表单不可提交
  await page.getByTestId("login-toggle").click();
  await page.getByTestId("login-username").fill(userId);
  await page.getByTestId("login-password").fill(password);

  // 提交注册后 App 拉 GET /sessions——用它断言身份头（X-User-Id + Bearer token）注入。
  const sessionsReq = page.waitForRequest(
    (req) => req.url().includes("/sessions") && req.method() === "GET",
  );
  await page.getByTestId("login-submit").click();
  const req = await sessionsReq;
  expect(req.headers()["x-user-id"]).toBe(userId);
  expect(req.headers()["authorization"]).toMatch(/^Bearer .+/);

  // 已登录形态：侧栏 + 账号底栏显示当前用户。
  await expect(page.getByRole("button", { name: "新对话" })).toBeVisible();
  await expect(page.getByTitle(userId)).toBeVisible();

  // 登出：清身份，回登录页。
  await page.getByRole("button", { name: "退出" }).click();
  await expect(page.getByTestId("login-card")).toBeVisible();
  await expect(page.getByRole("button", { name: "新对话" })).toHaveCount(0);
});
