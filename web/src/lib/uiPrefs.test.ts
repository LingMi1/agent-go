import { describe, expect, it } from "vitest";
import {
  ARTIFACTS_MAX_W,
  ARTIFACTS_MIN_W,
  WORKSPACE_SPLIT_DEFAULT,
  WORKSPACE_SPLIT_MAX,
  WORKSPACE_SPLIT_MIN,
  clampArtifactsWidth,
  clampWorkspaceSplit,
  loadUiPrefs,
} from "./uiPrefs";

describe("clampArtifactsWidth", () => {
  it("夹取到 [MIN, MAX] 且取整", () => {
    expect(clampArtifactsWidth(100)).toBe(ARTIFACTS_MIN_W);
    expect(clampArtifactsWidth(10_000)).toBe(ARTIFACTS_MAX_W);
    expect(clampArtifactsWidth(400.6)).toBe(401);
  });
  it("非法值回退默认", () => {
    expect(clampArtifactsWidth(Number.NaN)).toBe(384);
    expect(clampArtifactsWidth(Number.POSITIVE_INFINITY)).toBe(384);
  });
});

describe("clampWorkspaceSplit", () => {
  it("夹取到 [MIN, MAX]，区间内原样保留", () => {
    expect(clampWorkspaceSplit(0)).toBe(WORKSPACE_SPLIT_MIN);
    expect(clampWorkspaceSplit(0.05)).toBe(WORKSPACE_SPLIT_MIN);
    expect(clampWorkspaceSplit(1.5)).toBe(WORKSPACE_SPLIT_MAX);
    expect(clampWorkspaceSplit(0.4)).toBe(0.4);
  });
  it("非法值回退默认 0.55", () => {
    expect(clampWorkspaceSplit(Number.NaN)).toBe(WORKSPACE_SPLIT_DEFAULT);
    expect(clampWorkspaceSplit(Number.POSITIVE_INFINITY)).toBe(WORKSPACE_SPLIT_DEFAULT);
    expect(WORKSPACE_SPLIT_DEFAULT).toBe(0.55);
  });
  it("loadUiPrefs：无键落默认，越界值被夹取", () => {
    // node 环境无 localStorage → 走 catch 分支 → 默认
    expect(loadUiPrefs().workspaceSplit).toBe(WORKSPACE_SPLIT_DEFAULT);

    const store: Record<string, string> = {};
    (globalThis as { localStorage?: unknown }).localStorage = {
      getItem: (k: string) => store[k] ?? null,
      setItem: (k: string, v: string) => {
        store[k] = v;
      },
    };
    try {
      store["my-agent.ui"] = JSON.stringify({ workspaceSplit: 0.3 });
      expect(loadUiPrefs().workspaceSplit).toBe(0.3);
      store["my-agent.ui"] = JSON.stringify({ workspaceSplit: 0.99 });
      expect(loadUiPrefs().workspaceSplit).toBe(WORKSPACE_SPLIT_MAX);
      store["my-agent.ui"] = JSON.stringify({});
      expect(loadUiPrefs().workspaceSplit).toBe(WORKSPACE_SPLIT_DEFAULT);
    } finally {
      delete (globalThis as { localStorage?: unknown }).localStorage;
    }
  });
});

describe("theme 偏好", () => {
  it("无存储/未知值默认 light；显式 dark 才是 dark", () => {
    // node 环境无 localStorage → 走 catch 分支 → 默认 light
    expect(loadUiPrefs().theme).toBe("light");

    const store: Record<string, string> = {};
    (globalThis as { localStorage?: unknown }).localStorage = {
      getItem: (k: string) => store[k] ?? null,
      setItem: (k: string, v: string) => {
        store[k] = v;
      },
    };
    try {
      store["my-agent.ui"] = JSON.stringify({ theme: "dark" });
      expect(loadUiPrefs().theme).toBe("dark");
      store["my-agent.ui"] = JSON.stringify({ theme: "banana" });
      expect(loadUiPrefs().theme).toBe("light");
    } finally {
      delete (globalThis as { localStorage?: unknown }).localStorage;
    }
  });
});
