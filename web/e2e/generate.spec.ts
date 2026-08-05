// generate.spec（docs/11 §4.3 + docs/12）：生图工作区（fake provider）与 inpaint 蒙版链路。
//
// 出图链路（FAKE 全家桶）：COGNITION_IMAGE_GEN_PROVIDER=fake 让 provider 离线可用，
// providers/fake.py 的脚本模型在生图模式（IMAGE_GEN_INSTRUCTION 标记）下会真调
// image_generate（含 source_images/mask 解析）→ MinIO 落图 → 事件账本登记 →
// GET /artifacts 可列。因此除请求契约外，本 spec 还断言"生成后历史网格出现图片"
// （expect.poll，events 先落库再推流：终态帧到达时产物必已可列，禁 sleep）。
import { expect, test, type Page } from "@playwright/test";
import { freshUserId, gotoAsUser, makePng } from "./helpers";

// GenerateWorkspace 专属文件 input（ChatView 的附件 input 常挂载但 accept 更长，靠 accept 区分）。
const SOURCE_INPUT = 'input[accept=".png,.jpg,.jpeg,.webp"]';

async function openGenerate(page: Page): Promise<void> {
  await gotoAsUser(page, freshUserId());
  await page.getByRole("button", { name: "生图", exact: true }).click();
  await expect(page.getByRole("heading", { name: "生图工作区" })).toBeVisible();
}

/** 出图断言：生成完成（终态后组件已 refresh）→ 历史网格出现 ≥n 张图片缩略图。 */
async function expectHistoryImages(page: Page, n = 1): Promise<void> {
  await expect
    .poll(() => page.locator('[data-testid="generate-history"] img').count(), {
      timeout: 30_000,
      message: `生成历史网格应出现至少 ${n} 张图片`,
    })
    .toBeGreaterThanOrEqual(n);
}

test("文生图：提示词+张数 → POST /runs 请求契约正确且 run 走到终态", async ({ page }) => {
  await openGenerate(page);

  await page.getByTestId("generate-prompt").fill("一只戴帽子的橘猫");
  // 张数选择器（radix Select，当前值 “1 张”）
  await page.getByRole("combobox").filter({ hasText: "张" }).click();
  await page.getByRole("option", { name: "2 张" }).click();

  const runReq = page.waitForRequest((r) => r.url().includes("/runs") && r.method() === "POST");
  await page.getByTestId("generate-submit").click();
  const body = (await runReq).postDataJSON() as {
    query: string;
    sessionId: string;
    agentType: string;
    imageGen?: boolean;
    attachments?: unknown[];
  };
  expect(body.imageGen).toBe(true);
  expect(body.agentType).toBe("react");
  expect(body.sessionId).toMatch(/^generate:/); // 每次生成独立会话（不污染对话列表）
  expect(body.query).toContain("共 2 张");
  expect(body.query).toContain("一只戴帽子的橘猫");
  expect(body.attachments).toBeUndefined(); // 纯文生图无附件

  // run 走到终态：生成按钮从“生成中…”恢复可用
  await expect(page.getByTestId("generate-submit")).toBeEnabled({ timeout: 30_000 });
  await expect(page.getByTestId("generate-submit")).toContainText("生成");

  // 出图：fake 模型下单 image_generate → fake provider 落图 → 历史网格出现图片
  await expectHistoryImages(page);
});

test("inpaint：上传底图 → 画布画蒙版 → 确认 → 生成请求含 source+mask 两个附件", async ({ page }) => {
  await openGenerate(page);

  // 上传底图（内存构造 256×256 PNG，走真实 /uploads → MinIO）
  await page.locator(SOURCE_INPUT).setInputFiles({
    name: "e2e-source.png",
    mimeType: "image/png",
    buffer: makePng(256, 256),
  });
  const editMask = page.getByTestId("edit-mask");
  await expect(editMask).toBeVisible();
  await expect(editMask).toBeEnabled({ timeout: 30_000 }); // 上传完成才可编辑蒙版

  // 蒙版编辑器：画布尺寸=底图 naturalSize，鼠标横划一笔
  await editMask.click();
  await expect(page.getByTestId("mask-editor")).toBeVisible();
  const canvas = page.getByTestId("mask-canvas");
  await expect(canvas).toBeVisible();
  const box = await canvas.boundingBox();
  if (!box) throw new Error("mask-canvas 无 boundingBox");
  await page.mouse.move(box.x + box.width * 0.25, box.y + box.height * 0.5);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.75, box.y + box.height * 0.5, { steps: 8 });
  await page.mouse.up();
  await expect(page.getByText("已画 1 笔")).toBeVisible();

  // 确认蒙版：对话框关闭，表单出现蒙版缩略图
  await page.getByTestId("mask-confirm").click();
  await expect(page.getByTestId("mask-editor")).toBeHidden();
  await expect(page.getByTestId("mask-thumb")).toBeVisible();

  // 生成：请求体必须带 2 个附件（source+mask），query 把蒙版文件名写给模型（docs/12 §4.1）
  await page.getByTestId("generate-prompt").fill("把画面中间换成一朵花");
  const runReq = page.waitForRequest((r) => r.url().includes("/runs") && r.method() === "POST");
  await page.getByTestId("generate-submit").click();
  const body = (await runReq).postDataJSON() as {
    query: string;
    imageGen?: boolean;
    attachments?: { fileName: string; resourceKey: string }[];
  };
  expect(body.imageGen).toBe(true);
  expect(body.attachments).toHaveLength(2);
  expect(body.attachments![0].fileName).toBe("e2e-source.png");
  expect(body.attachments![1].fileName).toMatch(/^mask-[a-z0-9]+\.png$/);
  expect(body.query).toContain(body.attachments![1].fileName); // 蒙版文件名进 query
  expect(body.query).toContain("局部重绘");

  await expect(page.getByTestId("generate-submit")).toBeEnabled({ timeout: 30_000 });

  // 出图：inpaint 链路同样必须真出图（fake 模型把 source+mask 填进 image_generate）
  await expectHistoryImages(page);
});

test("移除蒙版/更换底图后蒙版作废（不带过期 mask 出请求）", async ({ page }) => {
  await openGenerate(page);
  await page.locator(SOURCE_INPUT).setInputFiles({
    name: "e2e-source.png",
    mimeType: "image/png",
    buffer: makePng(256, 256),
  });
  const editMask = page.getByTestId("edit-mask");
  await expect(editMask).toBeEnabled({ timeout: 30_000 });
  await editMask.click();
  const canvas = page.getByTestId("mask-canvas");
  await expect(canvas).toBeVisible();
  const box = await canvas.boundingBox();
  if (!box) throw new Error("mask-canvas 无 boundingBox");
  await page.mouse.move(box.x + box.width * 0.3, box.y + box.height * 0.3);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.6, box.y + box.height * 0.6, { steps: 5 });
  await page.mouse.up();
  await page.getByTestId("mask-confirm").click();
  await expect(page.getByTestId("mask-thumb")).toBeVisible();

  // 点 × 移除蒙版 → 生成请求应只剩底图 1 个附件（图生图，无 mask）
  await page.getByTestId("mask-remove").click();
  await expect(page.getByTestId("mask-thumb")).toHaveCount(0);
  await page.getByTestId("generate-prompt").fill("整体重绘为水彩风");
  const runReq = page.waitForRequest((r) => r.url().includes("/runs") && r.method() === "POST");
  await page.getByTestId("generate-submit").click();
  const body = (await runReq).postDataJSON() as { attachments?: { fileName: string }[]; query: string };
  expect(body.attachments).toHaveLength(1);
  expect(body.attachments![0].fileName).toBe("e2e-source.png");
  expect(body.query).not.toContain("局部重绘");

  await expect(page.getByTestId("generate-submit")).toBeEnabled({ timeout: 30_000 });
});
