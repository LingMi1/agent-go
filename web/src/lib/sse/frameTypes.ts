// SSE 帧与归并后视图模型。字段名逐字对齐 control-plane/internal/event/codec.go 的 ToSSEFrame。

export type MessageType =
  | "tool_thought"
  | "tool_call"
  | "tool_result"
  | "result"
  | "plan_thought"
  | "plan"
  | "task"
  | "approval_request" // M11 HITL：人工审批请求（审批=run 边界）
  | "heartbeat";

export interface ArtifactRef {
  resourceKey: string;
  name: string;
  previewUrl: string;
  downloadUrl: string;
  fileName: string;
  mimeType: string;
  size: number;
  missing: boolean;
}

export interface ToolResultFrame {
  toolName: string;
  toolParam?: unknown;
  toolResult: string;
  toolCallId: string;
}

export interface PlanFrame {
  title: string;
  steps: string[];
  stepStatus: string[];
  notes: string[];
}

// M11 HITL：approval_request 帧载荷（对齐 event.ApprovalPayload 的 json tag）。
export interface ApprovalFrame {
  approvalId: string;
  toolName: string;
  input?: unknown;
  reason?: string;
  pendingToolCallIds?: string[];
}

export interface ResultMap {
  status?: string;
  toolName?: string;
  toolCallId?: string;
  toolProvider?: string;
  dispatchIndex?: number;
  summary?: string;
  input?: unknown;
  errorMsg?: string;
  plannerRoundId?: string;
}

export interface SseFrame {
  requestId: string;
  messageId: string;
  seq: number;
  messageType: MessageType;
  messageTime: string;
  isFinal: boolean;
  finish: boolean;
  toolThought?: string;
  toolResult?: ToolResultFrame;
  result?: string;
  planThought?: string;
  plan?: PlanFrame;
  task?: string;
  resultMap?: ResultMap;
  artifactRefs?: ArtifactRef[];
  approval?: ApprovalFrame; // M11
}

// —— 归并后的视图模型（组件只读） ——
export interface ThoughtView {
  kind: "tool" | "plan";
  text: string;
  plannerRoundId?: string;
  // additive：思考时间戳（首帧/末帧 messageTime），ThoughtBlock 折叠行算"已思考 n 秒"。
  firstAt?: string;
  lastAt?: string;
}

export interface ToolCallView {
  toolCallId: string;
  toolName: string;
  toolProvider: string;
  status: string; // running | success | failed
  dispatchIndex: number;
  summary: string;
  input?: unknown;
  errorMsg?: string;
}

export interface ToolResultView {
  toolCallId: string;
  toolName: string;
  text: string;
}

export interface PlanView {
  title: string;
  steps: string[];
  stepStatus: string[];
  notes: string[];
  plannerRoundId?: string;
}

export interface TaskView {
  text: string;
}

// M11 HITL：审批卡视图（同 id 原位更新；decision 由前端 resumeApproval 本地补丁——
// run1 账本只记请求，决议链在 run2）。
export interface ApprovalView {
  approvalId: string;
  toolName: string;
  input?: unknown;
  reason?: string;
  status: "pending" | "approved" | "rejected";
}

export type OrderKind = "thought" | "toolCall" | "toolResult" | "plan" | "task" | "result" | "approval";
export interface OrderEntry {
  kind: OrderKind;
  key: string;
}

export interface RunState {
  runId: string;
  thoughts: Record<string, ThoughtView>;
  toolCalls: Record<string, ToolCallView>;
  toolResults: ToolResultView[];
  plan?: PlanView;
  plannerRounds: PlanView[];
  tasks: TaskView[];
  result?: { text: string; artifacts: ArtifactRef[] };
  approvals: Record<string, ApprovalView>; // M11
  artifacts: ArtifactRef[];
  // additive：toolCallId → resourceKey[]（ToolRow 图片缩略图条按调用归属 artifact）。
  artifactsByCall?: Record<string, string[]>;
  finished: boolean;
  order: OrderEntry[];
  unknown: SseFrame[];
}

export function emptyRunState(runId = ""): RunState {
  return {
    runId,
    thoughts: {},
    toolCalls: {},
    toolResults: [],
    approvals: {},
    plannerRounds: [],
    tasks: [],
    artifacts: [],
    finished: false,
    order: [],
    unknown: [],
  };
}
