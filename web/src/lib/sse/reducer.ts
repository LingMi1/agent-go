// 事件归并（纯函数）：按 messageType 原位更新，返回新 RunState。
// 原位更新规则严格对齐 codec.go：tool_call 同 messageId 覆盖、thought 累加、plan 快照替换、
// result finish 终态、artifact 按 resourceKey 去重、未知类型不崩（记 unknown，为 M7 留位）。

import type {
  ArtifactRef,
  OrderEntry,
  OrderKind,
  PlanView,
  RunState,
  SseFrame,
} from "./frameTypes";
import { emptyRunState } from "./frameTypes";

function mergeArtifacts(existing: ArtifactRef[], incoming?: ArtifactRef[]): ArtifactRef[] {
  if (!incoming || incoming.length === 0) return existing;
  const byKey = new Map(existing.map((a) => [a.resourceKey, a]));
  for (const a of incoming) byKey.set(a.resourceKey, a); // 按 resourceKey 去重，后者覆盖
  return [...byKey.values()];
}

function withOrder(order: OrderEntry[], kind: OrderKind, key: string): OrderEntry[] {
  if (order.some((e) => e.kind === kind && e.key === key)) return order;
  return [...order, { kind, key }];
}

export function applyFrame(state: RunState, frame: SseFrame): RunState {
  const next: RunState = { ...state, runId: state.runId || frame.requestId };
  const mid = frame.messageId;

  switch (frame.messageType) {
    case "tool_thought":
    case "plan_thought": {
      const kind = frame.messageType === "plan_thought" ? "plan" : "tool";
      const prev = state.thoughts[mid];
      const delta = frame.messageType === "plan_thought" ? frame.planThought : frame.toolThought;
      next.thoughts = {
        ...state.thoughts,
        [mid]: {
          kind,
          text: (prev?.text ?? "") + (delta ?? ""), // 累加
          plannerRoundId: frame.resultMap?.plannerRoundId ?? prev?.plannerRoundId,
          firstAt: prev?.firstAt ?? frame.messageTime, // additive：首帧时间
          lastAt: frame.messageTime, // additive：每帧更新
        },
      };
      next.order = withOrder(state.order, "thought", mid);
      break;
    }
    case "tool_call": {
      const rm = frame.resultMap ?? {};
      next.toolCalls = {
        ...state.toolCalls,
        [mid]: {
          // 同 messageId 覆盖：running → success/failed
          toolCallId: String(rm.toolCallId ?? mid),
          toolName: String(rm.toolName ?? ""),
          toolProvider: String(rm.toolProvider ?? "local"),
          status: String(rm.status ?? "running"),
          dispatchIndex: Number(rm.dispatchIndex ?? 0),
          summary: String(rm.summary ?? ""),
          input: rm.input,
          errorMsg: rm.errorMsg ? String(rm.errorMsg) : undefined,
        },
      };
      next.order = withOrder(state.order, "toolCall", mid);
      break;
    }
    case "tool_result": {
      const tr = frame.toolResult;
      if (tr) {
        next.toolResults = [
          ...state.toolResults,
          { toolCallId: tr.toolCallId, toolName: tr.toolName, text: tr.toolResult },
        ];
        next.order = withOrder(state.order, "toolResult", tr.toolCallId);
      }
      next.artifacts = mergeArtifacts(state.artifacts, frame.artifactRefs);
      // additive：artifact 归属到工具调用（resultMap.toolCallId 优先，toolResult 兜底）。
      const callId = frame.resultMap?.toolCallId ?? tr?.toolCallId;
      if (callId && frame.artifactRefs?.length) {
        const prev = state.artifactsByCall ?? {};
        const keys = new Set(prev[callId] ?? []);
        for (const a of frame.artifactRefs) keys.add(a.resourceKey);
        next.artifactsByCall = { ...prev, [callId]: [...keys] };
      }
      break;
    }
    case "plan": {
      if (frame.plan) {
        const view: PlanView = { ...frame.plan, plannerRoundId: frame.resultMap?.plannerRoundId };
        next.plan = view;
        const rid = view.plannerRoundId ?? "plan";
        const rounds = state.plannerRounds.filter((r) => (r.plannerRoundId ?? "plan") !== rid);
        next.plannerRounds = [...rounds, view]; // 同轮替换、跨轮追加
        next.order = withOrder(state.order, "plan", rid);
      }
      break;
    }
    case "task": {
      next.tasks = [...state.tasks, { text: frame.task ?? "" }];
      next.order = withOrder(state.order, "task", String(state.tasks.length));
      break;
    }
    case "result": {
      const artifacts = mergeArtifacts(state.artifacts, frame.artifactRefs);
      next.artifacts = artifacts;
      next.result = { text: frame.result ?? "", artifacts };
      next.finished = state.finished || frame.finish === true;
      next.order = withOrder(state.order, "result", "result");
      break;
    }
    case "approval_request": {
      // M11 HITL：同 id 原位更新（pending；决议由 resumeApproval 本地补丁）。
      const ap = frame.approval;
      if (ap?.approvalId) {
        next.approvals = {
          ...state.approvals,
          [ap.approvalId]: {
            approvalId: ap.approvalId,
            toolName: ap.toolName ?? "",
            input: ap.input,
            reason: ap.reason,
            status: state.approvals[ap.approvalId]?.status ?? "pending",
          },
        };
        // run1 中已 RUNNING 的工具卡翻"待审批"（不然回放里永远转圈）。
        const pend = ap.pendingToolCallIds ?? [];
        if (pend.length > 0) {
          const calls = { ...state.toolCalls };
          for (const id of pend) {
            const c = calls[id];
            if (c && c.status === "running") {
              calls[id] = { ...c, status: "awaiting_approval", summary: "等待人工审批" };
            }
          }
          next.toolCalls = calls;
        }
        next.order = withOrder(state.order, "approval", ap.approvalId);
      }
      break;
    }
    case "heartbeat":
      return state; // 忽略
    default:
      next.unknown = [...state.unknown, frame]; // 未知类型不崩，为审批/external 事件留位
      break;
  }
  return next;
}

export function reduceFrames(frames: SseFrame[], initial?: RunState): RunState {
  return frames.reduce(applyFrame, initial ?? emptyRunState());
}
