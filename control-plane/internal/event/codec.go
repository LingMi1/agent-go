package event

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"

	agentv1 "my-agent/control-plane/internal/genproto/agent/v1"
)

// sanitizeNUL 把字符串里的 NUL（U+0000）替换为 U+FFFD。
// PostgreSQL jsonb 不接受 \u0000 转义（22P05 unsupported Unicode escape sequence），
// 而 web_fetch 抓到的页面偶含 NUL——不净化则单条事件毒死整个 run（实测 deep_research
// 抓含 NUL 页面直接 FAILED）。在 FromProto 入口净化：实时推送与账本同源，replay ≡ live 保持。
func sanitizeNUL(s string) string {
	if !strings.ContainsRune(s, 0) {
		return s
	}
	return strings.ReplaceAll(s, "\x00", "�")
}

func sanitizeNULs(in []string) []string {
	for i, s := range in {
		in[i] = sanitizeNUL(s)
	}
	return in
}

// sanitizeNULJSON 对已序列化 JSON 做同类净化：把**真正的** \u0000 转义替换为 �。
// 用于 tool input 的 RawMessage 与 MarshalPayload 兜底。
//
// 必须区分转义奇偶性（22P02 回归教训）：网页正文里的字面文本 `\u0000` 会被 Go 序列化成
// `\\u0000`——朴素 ReplaceAll 会命中其中的 `\u0000` 子串，留下孤立反斜杠+�的非法转义，
// 整条 payload 变非法 JSON、run 落库即死（实测深研抓到含该字面文本的页面直接 FAILED）。
// 规则：前缀反斜杠为奇数个时末尾那个才是转义引导符（真 NUL 转义 → 替换）；
// 偶数个则全是成对字面反斜杠（字面文本 → 保留）。
func sanitizeNULJSON(b []byte) []byte {
	if !bytes.Contains(b, []byte(`\u0000`)) {
		return b
	}
	var out bytes.Buffer
	out.Grow(len(b))
	for i := 0; i < len(b); {
		if b[i] != '\\' {
			out.WriteByte(b[i])
			i++
			continue
		}
		j := i
		for j < len(b) && b[j] == '\\' {
			j++
		}
		n := j - i // 连续反斜杠数
		if n%2 == 1 && bytes.HasPrefix(b[j:], []byte("u0000")) {
			out.Write(b[i : j-1]) // 保留成对的字面反斜杠（偶数个）
			out.WriteString("�")
			i = j + len("u0000")
			continue
		}
		out.Write(b[i:j])
		i = j
	}
	return out.Bytes()
}

// FromProto 把 gRPC 收到的 proto Event 转换为规范 Envelope。
// seq 由 Python 分配，这里原样带入；Go 侧的单调/无空洞校验在 stream 层做。
func FromProto(p *agentv1.Event) (Envelope, error) {
	if p == nil {
		return Envelope{}, fmt.Errorf("event: nil proto event")
	}
	e := Envelope{
		SchemaVersion: SchemaVersion,
		Seq:           p.GetSeq(),
		RunID:         p.GetRunId(),
		MessageID:     p.GetMessageId(),
		Type:          eventTypeFromProto(p.GetType()),
		TSUnixMs:      p.GetTsUnixMs(),
		IsFinal:       p.GetIsFinal(),
		Finish:        p.GetFinish(),
		Step:          p.GetStep(),
	}
	switch payload := p.GetPayload().(type) {
	case *agentv1.Event_ToolThought: // tool_thought 与 plan_thought 共用此槽
		e.Thought = &ThoughtPayload{
			Text:           sanitizeNUL(payload.ToolThought.GetText()),
			PlannerRoundID: payload.ToolThought.GetPlannerRoundId(),
		}
	case *agentv1.Event_Plan:
		e.Plan = &PlanPayload{
			Title:          sanitizeNUL(payload.Plan.GetTitle()),
			Steps:          sanitizeNULs(payload.Plan.GetSteps()),
			StepStatus:     payload.Plan.GetStepStatus(),
			Notes:          sanitizeNULs(payload.Plan.GetNotes()),
			PlannerRoundID: payload.Plan.GetPlannerRoundId(),
		}
	case *agentv1.Event_Task:
		e.Task = &TaskPayload{Text: sanitizeNUL(payload.Task.GetText())}
	case *agentv1.Event_ToolCall:
		tool, err := toolPayloadFromProto(payload.ToolCall)
		if err != nil {
			return Envelope{}, err
		}
		e.Tool = tool
	case *agentv1.Event_ToolResult:
		tool, err := toolPayloadFromProto(payload.ToolResult)
		if err != nil {
			return Envelope{}, err
		}
		e.Tool = tool
	case *agentv1.Event_Result:
		e.Result = &ResultPayload{
			Text:      sanitizeNUL(payload.Result.GetText()),
			Artifacts: artifactsFromProto(payload.Result.GetArtifactRefs()),
		}
		if u := payload.Result.GetUsage(); u != nil && (u.GetInputTokens() != 0 || u.GetOutputTokens() != 0 || u.GetModelCalls() != 0) {
			e.Result.Usage = &UsagePayload{
				InputTokens: u.GetInputTokens(), OutputTokens: u.GetOutputTokens(), ModelCalls: u.GetModelCalls(),
			}
		}
	case *agentv1.Event_Approval:
		e.Approval = &ApprovalPayload{
			ApprovalID:         payload.Approval.GetApprovalId(),
			ToolName:           payload.Approval.GetToolName(),
			Input:              payload.Approval.GetInput().AsMap(),
			Reason:             payload.Approval.GetReason(),
			PendingToolCallIDs: payload.Approval.GetPendingToolCallIds(),
		}
	}
	return e, nil
}

func toolPayloadFromProto(p *agentv1.ToolPayload) (*ToolPayload, error) {
	if p == nil {
		return nil, fmt.Errorf("event: nil tool payload")
	}
	var input json.RawMessage
	if p.GetInput() != nil {
		raw, err := json.Marshal(p.GetInput().AsMap())
		if err != nil {
			return nil, fmt.Errorf("event: marshal tool input: %w", err)
		}
		input = sanitizeNULJSON(raw)
	}
	return &ToolPayload{
		ToolCallID:    p.GetToolCallId(),
		ToolName:      p.GetToolName(),
		ToolProvider:  p.GetToolProvider(),
		Status:        toolStatusFromProto(p.GetStatus()),
		DispatchIndex: p.GetDispatchIndex(),
		Input:         input,
		ToolResult:    sanitizeNUL(p.GetToolResult()),
		Summary:       sanitizeNUL(p.GetSummary()),
		ErrorMsg:      sanitizeNUL(p.GetErrorMsg()),
		Artifacts:     artifactsFromProto(p.GetArtifactRefs()),
	}, nil
}

func artifactsFromProto(in []*agentv1.ArtifactRef) []ArtifactRef {
	if len(in) == 0 {
		return nil
	}
	out := make([]ArtifactRef, 0, len(in))
	for _, a := range in {
		out = append(out, ArtifactRef{
			ResourceKey: a.GetResourceKey(),
			Name:        a.GetName(),
			PreviewURL:  a.GetPreviewUrl(),
			DownloadURL: a.GetDownloadUrl(),
			FileName:    a.GetFileName(),
			MimeType:    a.GetMimeType(),
			Size:        a.GetSize(),
			Missing:     a.GetMissing(),
		})
	}
	return out
}

func eventTypeFromProto(t agentv1.EventType) MessageType {
	switch t {
	case agentv1.EventType_EVENT_TYPE_TOOL_THOUGHT:
		return TypeToolThought
	case agentv1.EventType_EVENT_TYPE_TOOL_CALL:
		return TypeToolCall
	case agentv1.EventType_EVENT_TYPE_TOOL_RESULT:
		return TypeToolResult
	case agentv1.EventType_EVENT_TYPE_RESULT:
		return TypeResult
	case agentv1.EventType_EVENT_TYPE_PLAN_THOUGHT:
		return TypePlanThought
	case agentv1.EventType_EVENT_TYPE_PLAN:
		return TypePlan
	case agentv1.EventType_EVENT_TYPE_TASK:
		return TypeTask
	case agentv1.EventType_EVENT_TYPE_APPROVAL_REQUEST:
		return TypeApprovalRequest
	default:
		return ""
	}
}

func toolStatusFromProto(s agentv1.ToolCallStatus) ToolStatus {
	switch s {
	case agentv1.ToolCallStatus_TOOL_CALL_STATUS_RUNNING:
		return StatusRunning
	case agentv1.ToolCallStatus_TOOL_CALL_STATUS_SUCCESS:
		return StatusSuccess
	case agentv1.ToolCallStatus_TOOL_CALL_STATUS_FAILED:
		return StatusFailed
	default:
		return ""
	}
}

// sseFrame 是浏览器经 SSE 收到的 JSON 形状，字段名兼容原 AgentResponse。
type sseFrame struct {
	RequestID    string           `json:"requestId"`
	MessageID    string           `json:"messageId"`
	Seq          uint64           `json:"seq"`
	MessageType  string           `json:"messageType"`
	MessageTime  string           `json:"messageTime"`
	IsFinal      bool             `json:"isFinal"`
	Finish       bool             `json:"finish"`
	ToolThought  string           `json:"toolThought,omitempty"`
	ToolResult   *sseToolResult   `json:"toolResult,omitempty"`
	Result       string           `json:"result,omitempty"`
	PlanThought  string           `json:"planThought,omitempty"`
	Plan         *planFrame       `json:"plan,omitempty"`
	Task         string           `json:"task,omitempty"`
	ResultMap    map[string]any   `json:"resultMap,omitempty"`
	ArtifactRefs []ArtifactRef    `json:"artifactRefs,omitempty"`
	Approval     *ApprovalPayload `json:"approval,omitempty"` // M11 HITL
}

// planFrame 是 SSE 里 plan 对象的形状（plannerRoundId 放 resultMap，不在此）。
type planFrame struct {
	Title      string   `json:"title"`
	Steps      []string `json:"steps"`
	StepStatus []string `json:"stepStatus"`
	Notes      []string `json:"notes"`
}

type sseToolResult struct {
	ToolName   string          `json:"toolName"`
	ToolParam  json.RawMessage `json:"toolParam,omitempty"`
	ToolResult string          `json:"toolResult"`
	ToolCallID string          `json:"toolCallId"`
}

// ToSSEFrame 把 Envelope 渲染成浏览器 SSE 帧的 JSON（data: 部分）。
// 实时与回放共用此函数，是“重放=实时”不变量的唯一渲染点。
func ToSSEFrame(e Envelope) ([]byte, error) {
	frame := sseFrame{
		RequestID:   e.RunID,
		MessageID:   e.MessageID,
		Seq:         e.Seq,
		MessageType: string(e.Type),
		MessageTime: strconv.FormatInt(e.TSUnixMs, 10),
		IsFinal:     e.IsFinal,
		Finish:      e.Finish,
	}
	switch e.Type {
	case TypeToolThought:
		if e.Thought != nil {
			frame.ToolThought = e.Thought.Text
		}
	case TypeToolCall:
		if e.Tool != nil {
			frame.ResultMap = map[string]any{
				"status":        string(e.Tool.Status),
				"toolName":      e.Tool.ToolName,
				"toolCallId":    e.Tool.ToolCallID,
				"toolProvider":  e.Tool.ToolProvider,
				"dispatchIndex": e.Tool.DispatchIndex,
				"summary":       e.Tool.Summary,
			}
			if len(e.Tool.Input) > 0 {
				frame.ResultMap["input"] = json.RawMessage(e.Tool.Input)
			}
			if e.Tool.ErrorMsg != "" {
				frame.ResultMap["errorMsg"] = e.Tool.ErrorMsg
			}
		}
	case TypeToolResult:
		if e.Tool != nil {
			frame.ToolResult = &sseToolResult{
				ToolName:   e.Tool.ToolName,
				ToolParam:  e.Tool.Input,
				ToolResult: e.Tool.ToolResult,
				ToolCallID: e.Tool.ToolCallID,
			}
			frame.ArtifactRefs = e.Tool.Artifacts
		}
	case TypeResult:
		if e.Result != nil {
			frame.Result = e.Result.Text
			frame.ArtifactRefs = e.Result.Artifacts
		}
	case TypePlanThought:
		if e.Thought != nil {
			frame.PlanThought = e.Thought.Text
			if e.Thought.PlannerRoundID != "" {
				frame.ResultMap = map[string]any{"plannerRoundId": e.Thought.PlannerRoundID}
			}
		}
	case TypePlan:
		if e.Plan != nil {
			frame.Plan = &planFrame{
				Title:      e.Plan.Title,
				Steps:      e.Plan.Steps,
				StepStatus: e.Plan.StepStatus,
				Notes:      e.Plan.Notes,
			}
			if e.Plan.PlannerRoundID != "" {
				frame.ResultMap = map[string]any{"plannerRoundId": e.Plan.PlannerRoundID}
			}
		}
	case TypeTask:
		if e.Task != nil {
			frame.Task = e.Task.Text
		}
	case TypeApprovalRequest:
		if e.Approval != nil {
			frame.Approval = e.Approval // 结构体直接入帧（camelCase json tag 已定）
		}
	}
	return json.Marshal(frame)
}

// MarshalPayload 序列化 Envelope 的活跃 payload，用于 events 表的 payload JSONB 列。
// run_id/seq/type 等作为列单独存，故这里只存类型相关的 body。
// 出口统一过 sanitizeNULJSON：FromProto 已净化主要来源，这里兜底其余生产者
// （如 Approval.Input map 等未逐字段净化的路径），保证 jsonb 落库永不 22P05。
func (e Envelope) MarshalPayload() ([]byte, error) {
	marshal := func(v any) ([]byte, error) {
		b, err := json.Marshal(v)
		if err != nil {
			return nil, err
		}
		return sanitizeNULJSON(b), nil
	}
	switch e.Type {
	case TypeToolThought, TypePlanThought:
		return marshal(e.Thought)
	case TypeToolCall, TypeToolResult:
		return marshal(e.Tool)
	case TypeResult:
		return marshal(e.Result)
	case TypePlan:
		return marshal(e.Plan)
	case TypeTask:
		return marshal(e.Task)
	case TypeApprovalRequest:
		return marshal(e.Approval)
	default:
		return nil, fmt.Errorf("event: cannot marshal payload for type %q", e.Type)
	}
}

// UnmarshalPayload 从 payload JSONB 重建 Envelope 的活跃 payload。
// 调用方需先设置 e.Type（来自 message_type 列）。
func (e *Envelope) UnmarshalPayload(data []byte) error {
	switch e.Type {
	case TypeToolThought, TypePlanThought:
		var p ThoughtPayload
		if err := json.Unmarshal(data, &p); err != nil {
			return err
		}
		e.Thought = &p
	case TypeToolCall, TypeToolResult:
		var p ToolPayload
		if err := json.Unmarshal(data, &p); err != nil {
			return err
		}
		e.Tool = &p
	case TypeResult:
		var p ResultPayload
		if err := json.Unmarshal(data, &p); err != nil {
			return err
		}
		e.Result = &p
	case TypePlan:
		var p PlanPayload
		if err := json.Unmarshal(data, &p); err != nil {
			return err
		}
		e.Plan = &p
	case TypeTask:
		var p TaskPayload
		if err := json.Unmarshal(data, &p); err != nil {
			return err
		}
		e.Task = &p
	case TypeApprovalRequest:
		var p ApprovalPayload
		if err := json.Unmarshal(data, &p); err != nil {
			return err
		}
		e.Approval = &p
	default:
		return fmt.Errorf("event: cannot unmarshal payload for type %q", e.Type)
	}
	return nil
}
