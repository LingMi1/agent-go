import { beforeEach, describe, expect, it } from "vitest";
import {
  clearRole,
  clearToken,
  getRole,
  getToken,
  getUserId,
  setRole,
  setToken,
  setUserId,
} from "./identity";

// vitest 跑在 node 环境（无 localStorage）——用 Map 造一个最小 Storage 打桩，
// 覆盖 D3 新增的 token/role 读写往返（identity.ts 是认证状态的唯一持久层）。
beforeEach(() => {
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

describe("identity token/role（D3）", () => {
  it("token 往返：读→空、写→读回、清→空", () => {
    expect(getToken()).toBe("");
    setToken("abc123");
    expect(getToken()).toBe("abc123");
    clearToken();
    expect(getToken()).toBe("");
  });

  it("role 往返", () => {
    expect(getRole()).toBe("");
    setRole("admin");
    expect(getRole()).toBe("admin");
    clearRole();
    expect(getRole()).toBe("");
  });

  it("userId / token / role 三键互不干扰", () => {
    setUserId("alice");
    setToken("t-1");
    setRole("user");
    expect(getUserId()).toBe("alice");
    expect(getToken()).toBe("t-1");
    expect(getRole()).toBe("user");
    clearToken();
    // 清 token 不影响 userId/role
    expect(getUserId()).toBe("alice");
    expect(getRole()).toBe("user");
    expect(getToken()).toBe("");
  });
});
