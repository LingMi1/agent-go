// E2E 公共工具（docs/11 §4）：一次性身份注入、确定性对话锚点、离线 PNG 构造。
//
// 身份机制：localStorage 键 my-agent.userId（identity.ts），所有请求注入 X-User-Id。
// 每个测试用全新 browser context + 唯一 e2e-<随机> userId——数据天然按 owner 隔离
// （runs/events/artifacts/kb 都是 owner 域），删除类用例只碰自己的数据。
import { expect, type Page } from "@playwright/test";
import { deflateSync } from "node:zlib";

/** fake 模型的确定性脚本问题（SAMPLE_QUESTIONS[0]）：必出 calculator 工具卡 + “答案是 14。”。 */
export const CALC_QUESTION = "帮我算一下 2*(3+4) 等于多少";

/** 一次性用户 id（owner 级数据隔离；不与开发数据混淆）。 */
export function freshUserId(): string {
  return `e2e-${Math.random().toString(36).slice(2, 10)}${Date.now().toString(36)}`;
}

/**
 * 以指定身份进入应用：goto 前预写 localStorage（跳过登录页）。
 * 全新 context 下 my-agent.ui 不存在 → activeNav 默认 "chat"，无跨测试视图残留。
 */
export async function gotoAsUser(page: Page, userId: string): Promise<void> {
  await page.addInitScript((id: string) => localStorage.setItem("my-agent.userId", id), userId);
  await page.goto("/");
  // 已登录形态的第一锚点：侧栏“新对话”。
  await expect(page.getByRole("button", { name: "新对话" })).toBeVisible();
}

/** Composer 输入框（placeholder 随状态变化，统一用前缀匹配）。 */
export function composer(page: Page) {
  return page.getByPlaceholder(/输入问题，Enter 发送/);
}

/** 通过 Composer 发送一条消息（等待可输入 → fill → Enter）。 */
export async function sendMessage(page: Page, text: string): Promise<void> {
  const box = composer(page);
  await expect(box).toBeEnabled({ timeout: 30_000 });
  await box.fill(text);
  await box.press("Enter");
}

/**
 * 等待第 n 轮完成。完成锚（docs/11 §4.3 风险④）：“结论”标签数达到 n 且
 * 运行指示（data-testid=run-indicator）消失——healthz 徽章 30s 轮询不可作等待锚点。
 */
export async function waitTurnDone(page: Page, n: number): Promise<void> {
  await expect(page.getByText("结论", { exact: true })).toHaveCount(n, { timeout: 30_000 });
  await expect(page.getByTestId("run-indicator")).toHaveCount(0, { timeout: 30_000 });
}

// —— 离线构造合法 PNG（inpaint 底图用 setInputFiles 直喂 buffer，不依赖磁盘 fixture）——

function crc32(buf: Buffer): number {
  let c: number;
  const table: number[] = [];
  for (let n = 0; n < 256; n++) {
    c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  let crc = 0xffffffff;
  for (const b of buf) crc = table[(crc ^ b) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

function pngChunk(type: string, data: Buffer): Buffer {
  const t = Buffer.from(type, "ascii");
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length, 0);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(Buffer.concat([t, data])), 0);
  return Buffer.concat([len, t, data, crc]);
}

/** 生成 w×h 纯色 truecolor PNG（MaskEditor 画布逻辑尺寸=naturalSize，256 够鼠标好点）。 */
export function makePng(w = 256, h = 256, rgb: [number, number, number] = [220, 90, 40]): Buffer {
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0);
  ihdr.writeUInt32BE(h, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 2; // color type: truecolor
  const row = Buffer.concat([Buffer.from([0]), Buffer.alloc(w * 3).fill(Buffer.from(rgb))]);
  const raw = Buffer.concat(Array.from({ length: h }, () => row));
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    pngChunk("IHDR", ihdr),
    pngChunk("IDAT", deflateSync(raw)),
    pngChunk("IEND", Buffer.alloc(0)),
  ]);
}
