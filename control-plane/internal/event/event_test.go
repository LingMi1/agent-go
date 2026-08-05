package event

import (
	"encoding/json"
	"reflect"
	"testing"

	"google.golang.org/protobuf/types/known/structpb"
	agentv1 "my-agent/control-plane/internal/genproto/agent/v1"
)

const ts = int64(1700000000000)

func structOf(t *testing.T, m map[string]any) *structpb.Struct {
	t.Helper()
	s, err := structpb.NewStruct(m)
	if err != nil {
		t.Fatalf("structpb: %v", err)
	}
	return s
}

func assertJSONEqual(t *testing.T, got []byte, want string) {
	t.Helper()
	var g, w any
	if err := json.Unmarshal(got, &g); err != nil {
		t.Fatalf("unmarshal got: %v (%s)", err, got)
	}
	if err := json.Unmarshal([]byte(want), &w); err != nil {
		t.Fatalf("unmarshal want: %v", err)
	}
	if !reflect.DeepEqual(g, w) {
		t.Errorf("json mismatch\n got: %s\nwant: %s", got, want)
	}
}

// ---- golden 帧：实时与回放共用的渲染结果 ----

func TestToSSEFrame_Golden(t *testing.T) {
	cases := []struct {
		name string
		env  Envelope
		want string
	}{
		{
			name: "tool_thought",
			env: Envelope{Seq: 1, RunID: "r1", MessageID: "m1", Type: TypeToolThought, TSUnixMs: ts,
				Thought: &ThoughtPayload{Text: "我需要算一下"}},
			want: `{"requestId":"r1","messageId":"m1","seq":1,"messageType":"tool_thought","messageTime":"1700000000000","isFinal":false,"finish":false,"toolThought":"我需要算一下"}`,
		},
		{
			name: "tool_call_running",
			env: Envelope{Seq: 2, RunID: "r1", MessageID: "tc1", Type: TypeToolCall, TSUnixMs: ts, IsFinal: false,
				Tool: &ToolPayload{ToolCallID: "tc1", ToolName: "calculator", ToolProvider: "local",
					Status: StatusRunning, DispatchIndex: 1, Input: json.RawMessage(`{"expression":"2*(3+4)"}`), Summary: "正在调用 calculator"}},
			want: `{"requestId":"r1","messageId":"tc1","seq":2,"messageType":"tool_call","messageTime":"1700000000000","isFinal":false,"finish":false,"resultMap":{"status":"running","toolName":"calculator","toolCallId":"tc1","toolProvider":"local","dispatchIndex":1,"summary":"正在调用 calculator","input":{"expression":"2*(3+4)"}}}`,
		},
		{
			name: "tool_call_success",
			env: Envelope{Seq: 3, RunID: "r1", MessageID: "tc1", Type: TypeToolCall, TSUnixMs: ts, IsFinal: true,
				Tool: &ToolPayload{ToolCallID: "tc1", ToolName: "calculator", ToolProvider: "local",
					Status: StatusSuccess, DispatchIndex: 1, Input: json.RawMessage(`{"expression":"2*(3+4)"}`), Summary: "calculator 调用完成"}},
			want: `{"requestId":"r1","messageId":"tc1","seq":3,"messageType":"tool_call","messageTime":"1700000000000","isFinal":true,"finish":false,"resultMap":{"status":"success","toolName":"calculator","toolCallId":"tc1","toolProvider":"local","dispatchIndex":1,"summary":"calculator 调用完成","input":{"expression":"2*(3+4)"}}}`,
		},
		{
			name: "tool_result",
			env: Envelope{Seq: 4, RunID: "r1", MessageID: "tr1", Type: TypeToolResult, TSUnixMs: ts, IsFinal: true,
				Tool: &ToolPayload{ToolCallID: "tc1", ToolName: "calculator", Input: json.RawMessage(`{"expression":"2*(3+4)"}`), ToolResult: "14"}},
			want: `{"requestId":"r1","messageId":"tr1","seq":4,"messageType":"tool_result","messageTime":"1700000000000","isFinal":true,"finish":false,"toolResult":{"toolName":"calculator","toolParam":{"expression":"2*(3+4)"},"toolResult":"14","toolCallId":"tc1"}}`,
		},
		{
			name: "result",
			env: Envelope{Seq: 5, RunID: "r1", MessageID: "res1", Type: TypeResult, TSUnixMs: ts, IsFinal: true, Finish: true,
				Result: &ResultPayload{Text: "答案是 14"}},
			want: `{"requestId":"r1","messageId":"res1","seq":5,"messageType":"result","messageTime":"1700000000000","isFinal":true,"finish":true,"result":"答案是 14"}`,
		},
		{
			name: "plan_thought",
			env: Envelope{Seq: 1, RunID: "r1", MessageID: "r1:plan:1", Type: TypePlanThought, TSUnixMs: ts, IsFinal: false,
				Thought: &ThoughtPayload{Text: "先规划", PlannerRoundID: "pr1"}},
			want: `{"requestId":"r1","messageId":"r1:plan:1","seq":1,"messageType":"plan_thought","messageTime":"1700000000000","isFinal":false,"finish":false,"planThought":"先规划","resultMap":{"plannerRoundId":"pr1"}}`,
		},
		{
			name: "plan",
			env: Envelope{Seq: 2, RunID: "r1", MessageID: "plan1", Type: TypePlan, TSUnixMs: ts, IsFinal: true,
				Plan: &PlanPayload{Title: "计划", Steps: []string{"A", "B"}, StepStatus: []string{"in_progress", "not_started"}, Notes: []string{"", ""}, PlannerRoundID: "pr1"}},
			want: `{"requestId":"r1","messageId":"plan1","seq":2,"messageType":"plan","messageTime":"1700000000000","isFinal":true,"finish":false,"plan":{"title":"计划","steps":["A","B"],"stepStatus":["in_progress","not_started"],"notes":["",""]},"resultMap":{"plannerRoundId":"pr1"}}`,
		},
		{
			name: "task",
			env: Envelope{Seq: 3, RunID: "r1", MessageID: "task1", Type: TypeTask, TSUnixMs: ts, IsFinal: true,
				Task: &TaskPayload{Text: "做 A"}},
			want: `{"requestId":"r1","messageId":"task1","seq":3,"messageType":"task","messageTime":"1700000000000","isFinal":true,"finish":false,"task":"做 A"}`,
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got, err := ToSSEFrame(c.env)
			if err != nil {
				t.Fatalf("ToSSEFrame: %v", err)
			}
			assertJSONEqual(t, got, c.want)
		})
	}
}

// ---- 校验规则 ----

func TestValidate(t *testing.T) {
	valid := map[string]Envelope{
		"thought":      {Seq: 1, RunID: "r", MessageID: "m", Type: TypeToolThought, Thought: &ThoughtPayload{Text: "x"}},
		"call_running": {Seq: 1, RunID: "r", MessageID: "tc", Type: TypeToolCall, Tool: &ToolPayload{ToolCallID: "tc", Status: StatusRunning}},
		"call_success": {Seq: 1, RunID: "r", MessageID: "tc", Type: TypeToolCall, IsFinal: true, Tool: &ToolPayload{ToolCallID: "tc", Status: StatusSuccess}},
		"tool_result":  {Seq: 1, RunID: "r", MessageID: "tr", Type: TypeToolResult, IsFinal: true, Tool: &ToolPayload{ToolCallID: "tc"}},
		"result":       {Seq: 1, RunID: "r", MessageID: "res", Type: TypeResult, IsFinal: true, Finish: true, Result: &ResultPayload{Text: "ok"}},
		"plan_thought": {Seq: 1, RunID: "r", MessageID: "p", Type: TypePlanThought, Thought: &ThoughtPayload{Text: "x", PlannerRoundID: "pr"}},
		"plan":         {Seq: 1, RunID: "r", MessageID: "pl", Type: TypePlan, IsFinal: true, Plan: &PlanPayload{Title: "t"}},
		"task":         {Seq: 1, RunID: "r", MessageID: "tk", Type: TypeTask, IsFinal: true, Task: &TaskPayload{Text: "do"}},
	}
	for name, e := range valid {
		if err := e.Validate(); err != nil {
			t.Errorf("%s: expected valid, got %v", name, err)
		}
	}

	invalid := map[string]Envelope{
		"seq_zero":                {Seq: 0, RunID: "r", MessageID: "m", Type: TypeToolThought, Thought: &ThoughtPayload{}},
		"no_runid":                {Seq: 1, MessageID: "m", Type: TypeToolThought, Thought: &ThoughtPayload{}},
		"finish_on_non_result":    {Seq: 1, RunID: "r", MessageID: "tc", Type: TypeToolCall, Finish: true, Tool: &ToolPayload{ToolCallID: "tc", Status: StatusRunning}},
		"result_without_finish":   {Seq: 1, RunID: "r", MessageID: "res", Type: TypeResult, IsFinal: true, Result: &ResultPayload{}},
		"thought_missing_payload": {Seq: 1, RunID: "r", MessageID: "m", Type: TypeToolThought},
		"call_id_mismatch":        {Seq: 1, RunID: "r", MessageID: "X", Type: TypeToolCall, Tool: &ToolPayload{ToolCallID: "tc", Status: StatusRunning}},
		"call_running_final":      {Seq: 1, RunID: "r", MessageID: "tc", Type: TypeToolCall, IsFinal: true, Tool: &ToolPayload{ToolCallID: "tc", Status: StatusRunning}},
		"call_bad_status":         {Seq: 1, RunID: "r", MessageID: "tc", Type: TypeToolCall, Tool: &ToolPayload{ToolCallID: "tc"}},
		"bad_type":                {Seq: 1, RunID: "r", MessageID: "m", Type: MessageType("nope")},
		"plan_missing_payload":    {Seq: 1, RunID: "r", MessageID: "pl", Type: TypePlan, IsFinal: true},
		"task_not_final":          {Seq: 1, RunID: "r", MessageID: "tk", Type: TypeTask, Task: &TaskPayload{Text: "do"}},
		"plan_finish_true":        {Seq: 1, RunID: "r", MessageID: "pl", Type: TypePlan, IsFinal: true, Finish: true, Plan: &PlanPayload{Title: "t"}},
	}
	for name, e := range invalid {
		if err := e.Validate(); err == nil {
			t.Errorf("%s: expected invalid, got nil error", name)
		}
	}
}

// ---- FromProto ----

func TestFromProto(t *testing.T) {
	p := &agentv1.Event{
		Seq: 2, RunId: "r1", MessageId: "tc1", Type: agentv1.EventType_EVENT_TYPE_TOOL_CALL,
		TsUnixMs: ts, IsFinal: false, Finish: false,
		Payload: &agentv1.Event_ToolCall{ToolCall: &agentv1.ToolPayload{
			ToolCallId: "tc1", ToolName: "calculator", ToolProvider: "local",
			Status: agentv1.ToolCallStatus_TOOL_CALL_STATUS_RUNNING, DispatchIndex: 1,
			Input: structOf(t, map[string]any{"expression": "2*(3+4)"}), Summary: "正在调用 calculator",
		}},
	}
	e, err := FromProto(p)
	if err != nil {
		t.Fatalf("FromProto: %v", err)
	}
	if e.Type != TypeToolCall || e.Seq != 2 || e.MessageID != "tc1" {
		t.Fatalf("unexpected meta: %+v", e)
	}
	if e.Tool == nil || e.Tool.Status != StatusRunning || e.Tool.ToolName != "calculator" {
		t.Fatalf("unexpected tool: %+v", e.Tool)
	}
	assertJSONEqual(t, e.Tool.Input, `{"expression":"2*(3+4)"}`)
	if err := e.Validate(); err != nil {
		t.Errorf("FromProto output should validate: %v", err)
	}
}

// ---- payload 存储往返 ----

func TestPayloadRoundtrip(t *testing.T) {
	orig := Envelope{Seq: 4, RunID: "r1", MessageID: "tr1", Type: TypeToolResult, TSUnixMs: ts, IsFinal: true,
		Tool: &ToolPayload{ToolCallID: "tc1", ToolName: "calculator", Input: json.RawMessage(`{"expression":"2*(3+4)"}`), ToolResult: "14",
			Artifacts: []ArtifactRef{{ResourceKey: "k", Name: "n", Size: 123, Missing: false}}}}
	payload, err := orig.MarshalPayload()
	if err != nil {
		t.Fatalf("MarshalPayload: %v", err)
	}
	rebuilt := Envelope{Seq: orig.Seq, RunID: orig.RunID, MessageID: orig.MessageID, Type: orig.Type, TSUnixMs: orig.TSUnixMs, IsFinal: orig.IsFinal}
	if err := rebuilt.UnmarshalPayload(payload); err != nil {
		t.Fatalf("UnmarshalPayload: %v", err)
	}
	// 重建后渲染的帧应与原始帧逐字段一致（重放=实时的存储层保证）。
	gotOrig, _ := ToSSEFrame(orig)
	gotRebuilt, _ := ToSSEFrame(rebuilt)
	assertJSONEqual(t, gotRebuilt, string(gotOrig))
}
