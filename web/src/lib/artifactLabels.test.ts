import { describe, expect, it } from "vitest";
import { artifactLabels } from "../components/ArtifactWorkspace";
import type { ArtifactRef } from "./sse/frameTypes";

function art(key: string, name: string): ArtifactRef {
  return {
    resourceKey: key,
    name,
    fileName: name,
    mimeType: "text/markdown",
    size: 1,
    previewUrl: `/artifacts/${key}`,
    downloadUrl: `/artifacts/${key}`,
    missing: false,
  };
}

describe("artifactLabels（同名分组序号）", () => {
  it("唯一文件名不加序号；同名按出现顺序标第 N 份", () => {
    const labels = artifactLabels([
      art("k1", "report.md"),
      art("k2", "search-results.md"),
      art("k3", "search-results.md"),
    ]);
    expect(labels.get("k1")).toBe("report.md");
    expect(labels.get("k2")).toBe("search-results.md（第 1 份）");
    expect(labels.get("k3")).toBe("search-results.md（第 2 份）");
  });
  it("空列表安全", () => {
    expect(artifactLabels([]).size).toBe(0);
  });
});
