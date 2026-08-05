// Package event 定义贯穿控制面的规范事件模型（Envelope）及其与 proto / SSE / 存储的编解码。
//
// 它是“实时与回放同构”的关键枢纽：gRPC 收到的 proto Event 先转成 Envelope，
// Envelope 既用于落库（payload JSONB），也用于经 ToSSEFrame 渲染成浏览器帧；
// 回放时从库里重建 Envelope，再经同一个 ToSSEFrame 渲染——故重放帧与实时逐字段一致。
package event

import "encoding/json"

// SchemaVersion 是事件契约的主版本内标记，随契约只增演进。
const SchemaVersion = "v1"

// MessageType 是事件的语义类型（与浏览器 SSE 的 messageType 字段一致）。
type MessageType string

const (
	TypeToolThought MessageType = "tool_thought"
	TypeToolCall    MessageType = "tool_call"
	TypeToolResult  MessageType = "tool_result"
	TypeResult      MessageType = "result"
	// —— Plan-Execute 加性扩展 ——
	TypePlanThought MessageType = "plan_thought"
	TypePlan        MessageType = "plan"
	TypeTask        MessageType = "task"
	// M11 HITL：人工审批请求（finish 恒 false——审批=run 边界，随后的挂起 RESULT 收尾）。
	TypeApprovalRequest MessageType = "approval_request"
	// TypeHeartbeat 仅由网关注入，不经 gRPC、不入库、不参与重放比对。
	TypeHeartbeat MessageType = "heartbeat"
)

// ToolStatus 是工具调用的生命周期状态。
type ToolStatus string

const (
	StatusRunning ToolStatus = "running"
	StatusSuccess ToolStatus = "success"
	StatusFailed  ToolStatus = "failed"
)

// Envelope 是控制面内事件的规范表示（来源于 proto Event）。
// 其中恰有一个 payload 指针非空，且与 Type 对应。
type Envelope struct {
	SchemaVersion string
	Seq           uint64 // 每 run 单调、无空洞、从 1；由 Python 分配、Go 校验
	RunID         string
	MessageID     string // 原位更新键；tool_call 时 == Tool.ToolCallID
	Type          MessageType
	TSUnixMs      int64 // Python 发射时间 → SSE messageTime
	IsFinal       bool  // 本条消息终态
	Finish        bool  // 整个 run 终态；仅 result 为 true
	Step          string

	Thought  *ThoughtPayload // tool_thought 与 plan_thought 共用
	Tool     *ToolPayload    // tool_call 与 tool_result 共用
	Result   *ResultPayload
	Plan     *PlanPayload     // plan 事件
	Task     *TaskPayload     // task 事件（单个 <sep> 子任务）
	Approval *ApprovalPayload // M11 approval_request 事件
}

// ThoughtPayload 承载 tool_thought / plan_thought 的思考文本。
type ThoughtPayload struct {
	Text           string `json:"text"`
	PlannerRoundID string `json:"plannerRoundId,omitempty"` // 仅 plan_thought 填写
}

// PlanPayload 是计划快照（plan 事件）；steps/stepStatus/notes 并行索引。
type PlanPayload struct {
	Title          string   `json:"title"`
	Steps          []string `json:"steps"`
	StepStatus     []string `json:"stepStatus"`
	Notes          []string `json:"notes"`
	PlannerRoundID string   `json:"plannerRoundId,omitempty"`
}

// TaskPayload 是单个子任务（task 事件）。
type TaskPayload struct {
	Text string `json:"text"`
}

// ToolPayload 承载 tool_call（running/success/failed）与 tool_result。
type ToolPayload struct {
	ToolCallID    string          `json:"toolCallId"`
	ToolName      string          `json:"toolName"`
	ToolProvider  string          `json:"toolProvider"`
	Status        ToolStatus      `json:"status,omitempty"`
	DispatchIndex int32           `json:"dispatchIndex"`
	Input         json.RawMessage `json:"input,omitempty"`      // 解析后的参数对象
	ToolResult    string          `json:"toolResult,omitempty"` // observation 文本（tool_result）
	Summary       string          `json:"summary,omitempty"`
	ErrorMsg      string          `json:"errorMsg,omitempty"`
	Artifacts     []ArtifactRef   `json:"artifacts,omitempty"`
}

// ResultPayload 承载最终答复。
type ResultPayload struct {
	Text      string        `json:"text"`
	Artifacts []ArtifactRef `json:"artifacts,omitempty"`
	Usage     *UsagePayload `json:"usage,omitempty"` // M11：全 run 累计 token 用量
}

// UsagePayload：mapper 全 run 聚合的 token 用量（随终态 RESULT 到达，FinishRun 落 runs 表）。
type UsagePayload struct {
	InputTokens  int64 `json:"inputTokens"`
	OutputTokens int64 `json:"outputTokens"`
	ModelCalls   int32 `json:"modelCalls"`
}

// ApprovalPayload：人工审批请求（M11 HITL）。pending_tool_call_ids 供前端把
// run1 中仍 RUNNING 的工具卡翻"待审批"（不发伪造终态帧）。
type ApprovalPayload struct {
	ApprovalID         string         `json:"approvalId"`
	ToolName           string         `json:"toolName"`
	Input              map[string]any `json:"input,omitempty"`
	Reason             string         `json:"reason,omitempty"`
	PendingToolCallIDs []string       `json:"pendingToolCallIds,omitempty"`
}

// ArtifactRef 沿用原项目 8 字段形状：artifact UI 零改、缺文件降级显式。
type ArtifactRef struct {
	ResourceKey string `json:"resourceKey"`
	Name        string `json:"name"`
	PreviewURL  string `json:"previewUrl"`
	DownloadURL string `json:"downloadUrl"`
	FileName    string `json:"fileName"`
	MimeType    string `json:"mimeType"`
	Size        int64  `json:"size"`
	Missing     bool   `json:"missing"`
}
