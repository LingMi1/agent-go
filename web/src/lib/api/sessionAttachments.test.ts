import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { listSessionRuns, type SessionRunMeta } from "./client";

// 会话轮附件持久化：GET /sessions/{id}/runs 返回的 attachments（AttachmentRef 数组）
// 必须原样透传到 SessionRunMeta——loadSession 据此把附件填回回放轮（RunTurn.attachments），
// 刷新/重进会话后附件 chips 与工作区「上传内容」段才能还原。

let lastUrl = "";

function fetchMock(status: number, json: unknown) {
  return vi.fn(async (url: string) => {
    lastUrl = url;
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => json,
    } as Response;
  });
}

beforeEach(() => {
  lastUrl = "";
  const store = new Map<string, string>();
  globalThis.localStorage = {
    getItem: (k: string) => (store.has(k) ? (store.get(k) as string) : null),
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
    key: () => null,
    length: 0,
  } as Storage;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("listSessionRuns：附件元数据透传", () => {
  it("runs[].attachments 原样透传（含 previewUrl/downloadUrl），无附件轮为 undefined", async () => {
    const att = {
      resourceKey: "uploads/o1/s1/ab/cat.png",
      fileName: "cat.png",
      mimeType: "image/png",
      size: 7,
      previewUrl: "/artifacts/uploads/o1/s1/ab/cat.png",
      downloadUrl: "/artifacts/uploads/o1/s1/ab/cat.png",
    };
    vi.stubGlobal(
      "fetch",
      fetchMock(200, {
        sessionId: "s1",
        runs: [
          { runId: "r1", query: "带附件", agentType: "react", status: "SUCCESS", createdAt: "2026-07-21T00:00:00Z", attachments: [att] },
          { runId: "r2", query: "无附件", agentType: "react", status: "SUCCESS", createdAt: "2026-07-21T00:01:00Z" },
        ],
      }),
    );
    const metas: SessionRunMeta[] = await listSessionRuns("s1");
    expect(lastUrl).toBe("/sessions/s1/runs");
    expect(metas).toHaveLength(2);
    expect(metas[0].attachments).toEqual([att]);
    expect(metas[1].attachments).toBeUndefined();
  });

  it("缺 runs 字段兜底空数组", async () => {
    vi.stubGlobal("fetch", fetchMock(200, {}));
    expect(await listSessionRuns("s1")).toEqual([]);
  });

  it("非 2xx → 抛错", async () => {
    vi.stubGlobal("fetch", fetchMock(503, {}));
    await expect(listSessionRuns("s1")).rejects.toThrow(/503/);
  });
});
