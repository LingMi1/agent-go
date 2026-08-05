import { describe, expect, it } from "vitest";
import {
  canCreateConnector,
  canCreateTrigger,
  eventTypeLabel,
  pollIntervalLabel,
  triggerFilter,
  triggersByConnector,
} from "./connectorForm";
import type { TriggerItem } from "./api/client";

describe("connectorForm 纯逻辑", () => {
  it("triggerFilter：非空 repo → {repo}，空 → undefined", () => {
    expect(triggerFilter("octo/repo")).toEqual({ repo: "octo/repo" });
    expect(triggerFilter("  octo/repo  ")).toEqual({ repo: "octo/repo" });
    expect(triggerFilter("")).toBeUndefined();
    expect(triggerFilter("   ")).toBeUndefined();
  });

  it("triggersByConnector：按 connectorId 分组", () => {
    const trigs = [
      { triggerId: "t1", connectorId: "c1" },
      { triggerId: "t2", connectorId: "c1" },
      { triggerId: "t3", connectorId: "c2" },
    ] as TriggerItem[];
    const m = triggersByConnector(trigs);
    expect(m.get("c1")?.map((t) => t.triggerId)).toEqual(["t1", "t2"]);
    expect(m.get("c2")?.map((t) => t.triggerId)).toEqual(["t3"]);
    expect(m.get("c9")).toBeUndefined();
  });

  it("提交条件校验", () => {
    expect(canCreateConnector("ghp_x")).toBe(true);
    expect(canCreateConnector("   ")).toBe(false);
    expect(canCreateTrigger("c1", "回复 {{title}}")).toBe(true);
    expect(canCreateTrigger("", "x")).toBe(false);
    expect(canCreateTrigger("c1", "  ")).toBe(false);
  });

  it("标签", () => {
    expect(pollIntervalLabel(3600)).toBe("每小时");
    expect(pollIntervalLabel(42)).toBe("每 42 秒");
    expect(eventTypeLabel("issue")).toBe("Issue");
    expect(eventTypeLabel("pull_request")).toBe("Pull Request");
    expect(eventTypeLabel("unknown")).toBe("unknown");
  });
});
