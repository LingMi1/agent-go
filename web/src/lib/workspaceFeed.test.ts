import { describe, expect, it } from "vitest";
import type { ArtifactRef, RunState } from "./sse/frameTypes";
import { emptyRunState } from "./sse/frameTypes";
import { WEB_SEARCH_JSON_PREFIX } from "./sse/toolPayloads";
import { buildActivityFeed, isChartArtifact } from "./workspaceFeed";

function art(resourceKey: string, name = `${resourceKey}.md`): ArtifactRef {
  return {
    resourceKey,
    name,
    fileName: name,
    mimeType: "text/markdown",
    size: 10,
    previewUrl: `/artifacts/${resourceKey}`,
    downloadUrl: `/artifacts/${resourceKey}`,
    missing: false,
  };
}

function searchText(query: string, urls: string[]): string {
  const payload = {
    query,
    provider: "tavily",
    results: urls.map((u, i) => ({ title: `t${i}`, url: u, snippet: `s${i}` })),
  };
  return `1. t0\n${WEB_SEARCH_JSON_PREFIX}${JSON.stringify(payload)}`;
}

function state(over: Partial<RunState> & { artifactsByCall?: Record<string, string[]> }): RunState {
  return { ...emptyRunState("r"), ...over };
}

describe("buildActivityFeed", () => {
  it("按时序交错 sources 与 artifact（artifactsByCall 定位 + 末尾补漏）", () => {
    const s = state({
      toolResults: [
        { toolCallId: "c1", toolName: "web_search", text: searchText("q1", ["https://a.com/x"]) },
        { toolCallId: "c2", toolName: "chart", text: "已生成图表" },
      ],
      artifacts: [art("k1", "chart.json"), art("k2", "report.md")],
      artifactsByCall: { c2: ["k1"] },
    });
    const feed = buildActivityFeed([s]);
    expect(feed.map((i) => i.kind)).toEqual(["sources", "artifact", "artifact"]);
    expect(feed[0]).toMatchObject({ kind: "sources", toolCallId: "c1", query: "q1" });
    // k1 插在 c2 结果位置且带 toolName；k2 无归属 → 末尾补、toolName undefined
    expect(feed[1]).toMatchObject({ kind: "artifact", toolName: "chart" });
    expect((feed[1] as { art: ArtifactRef }).art.resourceKey).toBe("k1");
    expect(feed[2]).toMatchObject({ kind: "artifact", toolName: undefined });
    expect((feed[2] as { art: ArtifactRef }).art.resourceKey).toBe("k2");
  });

  it("sources 解析：无哨兵/解析失败的 tool_result 不产出", () => {
    const s = state({
      toolResults: [
        { toolCallId: "c1", toolName: "web_search", text: "纯文本，无哨兵" },
        { toolCallId: "c2", toolName: "web_search", text: `${WEB_SEARCH_JSON_PREFIX}{broken` },
      ],
    });
    expect(buildActivityFeed([s])).toEqual([]);
  });

  it("跨轮去重：重复 resourceKey / toolCallId 只出现一次，顺序保持首现位置", () => {
    const run1 = state({
      toolResults: [{ toolCallId: "c1", toolName: "web_search", text: searchText("q", ["https://a.com"]) }],
      artifacts: [art("k1")],
    });
    // 回放合并等场景下同 key/同 callId 再次出现
    const run2 = state({
      toolResults: [{ toolCallId: "c1", toolName: "web_search", text: searchText("q", ["https://a.com"]) }],
      artifacts: [art("k1"), art("k2")],
    });
    const feed = buildActivityFeed([run1, run2]);
    expect(feed).toHaveLength(3);
    expect(feed.map((i) => (i.kind === "artifact" ? i.art.resourceKey : i.toolCallId))).toEqual([
      "c1",
      "k1",
      "k2",
    ]);
  });

  it("artifactsByCall 缺失/畸形时防御式回退：全部按 artifacts 顺序产出", () => {
    const s = state({
      artifacts: [art("k1"), art("k2")],
      artifactsByCall: { c9: "not-an-array" as unknown as string[] },
    });
    const feed = buildActivityFeed([s]);
    expect(feed.map((i) => (i.kind === "artifact" ? i.art.resourceKey : ""))).toEqual(["k1", "k2"]);
  });

  it("toolName 反查：末尾补漏产物也能经 artifactsByCall + toolCalls 找回工具名", () => {
    const s = state({
      toolCalls: {
        m1: {
          toolCallId: "c1",
          toolName: "data_analysis",
          toolProvider: "local",
          status: "success",
          dispatchIndex: 0,
          summary: "",
        },
      },
      // 没有对应 tool_result（如降级路径），仍能从 toolCalls 反查
      artifacts: [art("k1")],
      artifactsByCall: { c1: ["k1"] },
    });
    const feed = buildActivityFeed([s]);
    expect(feed[0]).toMatchObject({ kind: "artifact", toolName: "data_analysis" });
  });

  it("空查询归一为 undefined", () => {
    const s = state({
      toolResults: [{ toolCallId: "c1", toolName: "web_search", text: searchText("", ["https://a.com"]) }],
    });
    expect(buildActivityFeed([s])[0]).toMatchObject({ kind: "sources", query: undefined });
  });
});

describe("isChartArtifact", () => {
  const mk = (name: string, mimeType = "application/json") => ({ name, mimeType });

  it.each([
    "echarts-option.json",
    "chart.json",
    "charts.json",
    "echarts.json",
    "sales-chart.json",
    "sales_chart.json",
    "chart-2.json",
    "charts_v1.json",
    "ECharts-Option.JSON",
  ])("正例：%s", (name) => {
    expect(isChartArtifact(mk(name))).toBe(true);
  });

  it.each(["chart.png", "report.json", "charting.json", "mychart.json", "chart.json.txt", "chart", ""])(
    "反例：%s",
    (name) => {
      expect(isChartArtifact(mk(name))).toBe(false);
    },
  );
});
