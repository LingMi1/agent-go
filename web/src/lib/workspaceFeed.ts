// 工作区「动态」feed 的纯函数聚合：多轮 RunState → 按时序的 artifact/sources 项。
// 纯函数便于 vitest 覆盖 ordering/去重；组件层（WorkspacePanel/ActivityTab）只做渲染。

import type { ArtifactRef, RunState } from "./sse/frameTypes";
import { parseWebSearchResult } from "./sse/toolPayloads";

export type ActivityItem =
  | { kind: "artifact"; art: ArtifactRef; toolName?: string }
  | {
      kind: "sources";
      toolCallId: string;
      query?: string;
      sources: { title: string; url: string; snippet: string }[];
    };

// artifactsByCall（toolCallId → resourceKey[]）是 RunState 的可选新字段（并行批次补充），
// 这里防御式读取：可能 undefined、值可能不是数组。
type FeedRunState = RunState & { artifactsByCall?: Record<string, string[]> };

function callArtifacts(byCall: Record<string, string[]>, toolCallId: string): string[] {
  const keys = byCall[toolCallId];
  return Array.isArray(keys) ? keys : [];
}

/**
 * 按时序聚合多轮 RunState：
 * - 每个 tool_result 的 web_search 观察串（parseWebSearchResult 非空）→ sources 项；
 * - artifacts → artifact 项：能通过 artifactsByCall 定位到 tool_result 的，插在该结果位置
 *   并带 toolName；其余按 artifacts 顺序补在该轮末尾（result 帧收尾产物等）；
 * - 按出现顺序全局去重（artifact 按 resourceKey、sources 按 toolCallId）。
 */
export function buildActivityFeed(states: RunState[]): ActivityItem[] {
  const items: ActivityItem[] = [];
  const seenArtifacts = new Set<string>();
  const seenSources = new Set<string>();

  for (const state of states) {
    const byCall = (state as FeedRunState).artifactsByCall ?? {};
    const artByKey = new Map(state.artifacts.map((a) => [a.resourceKey, a]));

    // toolCallId → toolName（tool_call 帧优先，tool_result 兜底）
    const nameByCall = new Map<string, string>();
    for (const c of Object.values(state.toolCalls)) {
      if (c.toolName) nameByCall.set(c.toolCallId, c.toolName);
    }
    for (const r of state.toolResults) {
      if (r.toolName && !nameByCall.has(r.toolCallId)) nameByCall.set(r.toolCallId, r.toolName);
    }

    const pushArtifact = (resourceKey: string, toolName?: string) => {
      const art = artByKey.get(resourceKey);
      if (!art || seenArtifacts.has(resourceKey)) return;
      seenArtifacts.add(resourceKey);
      items.push({ kind: "artifact", art, toolName });
    };

    for (const tr of state.toolResults) {
      const parsed = parseWebSearchResult(tr.text);
      if (parsed && !seenSources.has(tr.toolCallId)) {
        seenSources.add(tr.toolCallId);
        items.push({
          kind: "sources",
          toolCallId: tr.toolCallId,
          query: parsed.query || undefined,
          sources: parsed.results,
        });
      }
      for (const key of callArtifacts(byCall, tr.toolCallId)) {
        pushArtifact(key, tr.toolName || nameByCall.get(tr.toolCallId));
      }
    }

    // 未挂到任何 tool_result 位置的产物：按 artifacts 顺序补末尾；toolName 尽量反查。
    const keyToCall = new Map<string, string>();
    for (const [callId, keys] of Object.entries(byCall)) {
      if (!Array.isArray(keys)) continue;
      for (const k of keys) if (!keyToCall.has(k)) keyToCall.set(k, callId);
    }
    for (const a of state.artifacts) {
      const callId = keyToCall.get(a.resourceKey);
      pushArtifact(a.resourceKey, callId ? nameByCall.get(callId) : undefined);
    }
  }
  return items;
}

// chart 技能产物命名：echarts-option.json 或 *chart(s)*.json 变体（chart.json / sales-chart.json /
// echarts_v2.json…）；charting.json / mychart.json / chart.png 不算。
const CHART_NAME_RE = /(^|[-_.])e?charts?([-_.][^/]*)?\.json$/i;

export function isChartArtifact(art: { name: string; mimeType: string }): boolean {
  const name = art.name || "";
  return name === "echarts-option.json" || CHART_NAME_RE.test(name);
}
