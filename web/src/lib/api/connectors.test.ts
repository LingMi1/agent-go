import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  createConnector,
  createTrigger,
  deleteConnector,
  listConnectors,
  listTriggers,
  toggleTrigger,
} from "./client";

// node 环境无 localStorage/fetch——打桩 identity 存储 + 捕获 fetch 请求，
// 断言连接器/触发规则客户端的三段式请求整形（URL/方法/body/头）。
interface Captured {
  url: string;
  method: string;
  body?: unknown;
  headers: Record<string, string>;
}

let calls: Captured[] = [];

function fetchMock(status: number, json: unknown) {
  return vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({
      url,
      method: init?.method ?? "GET",
      body: init?.body ? JSON.parse(init.body as string) : undefined,
      headers: (init?.headers as Record<string, string>) ?? {},
    });
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => json,
    } as Response;
  });
}

beforeEach(() => {
  calls = [];
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

describe("连接器/触发规则客户端（D2）", () => {
  it("listConnectors：GET /connectors，解析 connectors 数组", async () => {
    vi.stubGlobal("fetch", fetchMock(200, { connectors: [{ connectorId: "c1", kind: "github" }] }));
    const items = await listConnectors();
    expect(items).toHaveLength(1);
    expect(calls[0].url).toBe("/connectors");
    expect(calls[0].method).toBe("GET");
    expect(calls[0].headers["X-User-Id"]).toBeDefined();
  });

  it("listTriggers：缺字段兜底空数组", async () => {
    vi.stubGlobal("fetch", fetchMock(200, {}));
    expect(await listTriggers()).toEqual([]);
  });

  it("createConnector：POST /connectors 带 kind/pat/pollIntervalS + JSON 头", async () => {
    vi.stubGlobal("fetch", fetchMock(200, {}));
    await createConnector({ kind: "github", pat: "ghp_x", pollIntervalS: 3600 });
    expect(calls[0].url).toBe("/connectors");
    expect(calls[0].method).toBe("POST");
    expect(calls[0].body).toEqual({ kind: "github", pat: "ghp_x", pollIntervalS: 3600 });
    expect(calls[0].headers["Content-Type"]).toBe("application/json");
  });

  it("createTrigger：POST /triggers 带 filter/needsApproval", async () => {
    vi.stubGlobal("fetch", fetchMock(200, {}));
    await createTrigger({
      connectorId: "c1",
      eventType: "issue",
      filter: { repo: "o/r" },
      queryTemplate: "回复 {{title}}",
      agentType: "react",
      needsApproval: true,
    });
    expect(calls[0].url).toBe("/triggers");
    expect(calls[0].body).toMatchObject({ connectorId: "c1", filter: { repo: "o/r" }, needsApproval: true });
  });

  it("deleteConnector：DELETE /connectors/{id}（id 编码）", async () => {
    vi.stubGlobal("fetch", fetchMock(200, {}));
    await deleteConnector("c 1");
    expect(calls[0].url).toBe("/connectors/c%201");
    expect(calls[0].method).toBe("DELETE");
  });

  it("toggleTrigger：POST /triggers/{id}/toggle 带 enabled", async () => {
    vi.stubGlobal("fetch", fetchMock(200, {}));
    await toggleTrigger("t1", false);
    expect(calls[0].url).toBe("/triggers/t1/toggle");
    expect(calls[0].body).toEqual({ enabled: false });
  });

  it("非 2xx → 抛错", async () => {
    vi.stubGlobal("fetch", fetchMock(503, {}));
    await expect(listConnectors()).rejects.toThrow(/503/);
  });
});
