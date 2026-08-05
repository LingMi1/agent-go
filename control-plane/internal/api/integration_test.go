package api_test

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/types/known/structpb"

	"my-agent/control-plane/internal/api"
	"my-agent/control-plane/internal/cognition"
	"my-agent/control-plane/internal/dispatch"
	"my-agent/control-plane/internal/event"
	agentv1 "my-agent/control-plane/internal/genproto/agent/v1"
	"my-agent/control-plane/internal/store"
	"my-agent/control-plane/internal/stream"
)

const ts = int64(1700000000000)

// fakeCog 是一个进程内的认知面假实现：把一次 ReAct(1 工具) run 的 5 个 golden 事件流出。
// 记录最近一次收到的 RunRequest（供断言 attachments/metadata 贯通）。
type fakeCog struct {
	agentv1.UnimplementedCognitionServiceServer
	mu   sync.Mutex
	last *agentv1.RunRequest
}

func (f *fakeCog) lastReq() *agentv1.RunRequest {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.last
}

func (f *fakeCog) Run(req *agentv1.RunRequest, srv grpc.ServerStreamingServer[agentv1.Event]) error {
	f.mu.Lock()
	f.last = req
	f.mu.Unlock()
	runID := req.GetRunId()
	var events []*agentv1.Event
	switch {
	case req.GetMetadata()["approval_resume_id"] != "":
		events = approvalResumeEvents(runID)
	case strings.Contains(req.GetQuery(), "需要审批"):
		events = approvalPauseEvents(runID)
	case req.GetAgentType() == "plan_solve":
		events = planEvents(runID)
	default:
		events = reactEvents(runID)
	}
	for _, e := range events {
		if err := srv.Send(e); err != nil {
			return err
		}
	}
	return nil
}

func reactEvents(runID string) []*agentv1.Event {
	in, _ := structpb.NewStruct(map[string]any{"expression": "2*(3+4)"})
	return []*agentv1.Event{
		{Seq: 1, RunId: runID, MessageId: runID + ":think:1", Type: agentv1.EventType_EVENT_TYPE_TOOL_THOUGHT, TsUnixMs: ts, IsFinal: true,
			Payload: &agentv1.Event_ToolThought{ToolThought: &agentv1.ThoughtPayload{Text: "先算一下"}}},
		{Seq: 2, RunId: runID, MessageId: "tc1", Type: agentv1.EventType_EVENT_TYPE_TOOL_CALL, TsUnixMs: ts, IsFinal: false,
			Payload: &agentv1.Event_ToolCall{ToolCall: &agentv1.ToolPayload{ToolCallId: "tc1", ToolName: "calculator", ToolProvider: "local", Status: agentv1.ToolCallStatus_TOOL_CALL_STATUS_RUNNING, DispatchIndex: 1, Input: in, Summary: "正在调用 calculator"}}},
		{Seq: 3, RunId: runID, MessageId: "tc1", Type: agentv1.EventType_EVENT_TYPE_TOOL_CALL, TsUnixMs: ts, IsFinal: true,
			Payload: &agentv1.Event_ToolCall{ToolCall: &agentv1.ToolPayload{ToolCallId: "tc1", ToolName: "calculator", ToolProvider: "local", Status: agentv1.ToolCallStatus_TOOL_CALL_STATUS_SUCCESS, DispatchIndex: 1, Input: in, Summary: "calculator 调用完成"}}},
		{Seq: 4, RunId: runID, MessageId: "tr1", Type: agentv1.EventType_EVENT_TYPE_TOOL_RESULT, TsUnixMs: ts, IsFinal: true,
			Payload: &agentv1.Event_ToolResult{ToolResult: &agentv1.ToolPayload{ToolCallId: "tc1", ToolName: "calculator", Input: in, ToolResult: "14"}}},
		{Seq: 5, RunId: runID, MessageId: "res1", Type: agentv1.EventType_EVENT_TYPE_RESULT, TsUnixMs: ts, IsFinal: true, Finish: true,
			Payload: &agentv1.Event_Result{Result: &agentv1.ResultPayload{Text: "答案是 14"}}},
	}
}

// planEvents 流出一次 Plan-Execute run 的代表性事件（plan_thought/plan/task/tool_*/result）。
func planEvents(runID string) []*agentv1.Event {
	in, _ := structpb.NewStruct(map[string]any{"expression": "2+3"})
	return []*agentv1.Event{
		{Seq: 1, RunId: runID, MessageId: runID + ":plan_thought:1", Type: agentv1.EventType_EVENT_TYPE_PLAN_THOUGHT, TsUnixMs: ts, IsFinal: true,
			Payload: &agentv1.Event_ToolThought{ToolThought: &agentv1.ThoughtPayload{Text: "我来拆解任务", PlannerRoundId: runID + ":planner:1"}}},
		{Seq: 2, RunId: runID, MessageId: runID + ":plan:1", Type: agentv1.EventType_EVENT_TYPE_PLAN, TsUnixMs: ts, IsFinal: true,
			Payload: &agentv1.Event_Plan{Plan: &agentv1.PlanPayload{Title: "计算并汇总", Steps: []string{"计算 2+3"}, StepStatus: []string{"in_progress"}, PlannerRoundId: runID + ":planner:1"}}},
		{Seq: 3, RunId: runID, MessageId: runID + ":task:1", Type: agentv1.EventType_EVENT_TYPE_TASK, TsUnixMs: ts, IsFinal: true,
			Payload: &agentv1.Event_Task{Task: &agentv1.TaskPayload{Text: "计算 2+3"}}},
		{Seq: 4, RunId: runID, MessageId: "b1:tc1", Type: agentv1.EventType_EVENT_TYPE_TOOL_CALL, TsUnixMs: ts, IsFinal: false,
			Payload: &agentv1.Event_ToolCall{ToolCall: &agentv1.ToolPayload{ToolCallId: "b1:tc1", ToolName: "calculator", ToolProvider: "local", Status: agentv1.ToolCallStatus_TOOL_CALL_STATUS_RUNNING, DispatchIndex: 1, Input: in, Summary: "正在调用 calculator"}}},
		{Seq: 5, RunId: runID, MessageId: "b1:tc1", Type: agentv1.EventType_EVENT_TYPE_TOOL_CALL, TsUnixMs: ts, IsFinal: true,
			Payload: &agentv1.Event_ToolCall{ToolCall: &agentv1.ToolPayload{ToolCallId: "b1:tc1", ToolName: "calculator", ToolProvider: "local", Status: agentv1.ToolCallStatus_TOOL_CALL_STATUS_SUCCESS, DispatchIndex: 1, Input: in, Summary: "calculator 调用完成"}}},
		{Seq: 6, RunId: runID, MessageId: "b1:tc1:result", Type: agentv1.EventType_EVENT_TYPE_TOOL_RESULT, TsUnixMs: ts, IsFinal: true,
			Payload: &agentv1.Event_ToolResult{ToolResult: &agentv1.ToolPayload{ToolCallId: "b1:tc1", ToolName: "calculator", Input: in, ToolResult: "5"}}},
		{Seq: 7, RunId: runID, MessageId: runID + ":result", Type: agentv1.EventType_EVENT_TYPE_RESULT, TsUnixMs: ts, IsFinal: true, Finish: true,
			Payload: &agentv1.Event_Result{Result: &agentv1.ResultPayload{Text: "计算完成：2+3=5"}}},
	}
}

// approvalPauseEvents：run1 挂起——thought + RUNNING 工具卡 + approval_request + 挂起 result。
func approvalPauseEvents(runID string) []*agentv1.Event {
	in, _ := structpb.NewStruct(map[string]any{"expression": "rm -rf /"})
	apIn, _ := structpb.NewStruct(map[string]any{"expression": "rm -rf /"})
	return []*agentv1.Event{
		{Seq: 1, RunId: runID, MessageId: runID + ":think:1", Type: agentv1.EventType_EVENT_TYPE_TOOL_THOUGHT, TsUnixMs: ts, IsFinal: true,
			Payload: &agentv1.Event_ToolThought{ToolThought: &agentv1.ThoughtPayload{Text: "高危操作，先请求授权"}}},
		{Seq: 2, RunId: runID, MessageId: "tc-danger", Type: agentv1.EventType_EVENT_TYPE_TOOL_CALL, TsUnixMs: ts, IsFinal: false,
			Payload: &agentv1.Event_ToolCall{ToolCall: &agentv1.ToolPayload{ToolCallId: "tc-danger", ToolName: "calculator", ToolProvider: "local", Status: agentv1.ToolCallStatus_TOOL_CALL_STATUS_RUNNING, DispatchIndex: 1, Input: in, Summary: "正在调用 calculator"}}},
		{Seq: 3, RunId: runID, MessageId: runID + ":approval:ap-1", Type: agentv1.EventType_EVENT_TYPE_APPROVAL_REQUEST, TsUnixMs: ts, IsFinal: true,
			Payload: &agentv1.Event_Approval{Approval: &agentv1.ApprovalPayload{ApprovalId: "ap-1", ToolName: "calculator", Input: apIn, Reason: "高危", PendingToolCallIds: []string{"tc-danger"}}}},
		{Seq: 4, RunId: runID, MessageId: runID + ":result", Type: agentv1.EventType_EVENT_TYPE_RESULT, TsUnixMs: ts, IsFinal: true, Finish: true,
			Payload: &agentv1.Event_Result{Result: &agentv1.ResultPayload{Text: "⏸ 已挂起等待人工审批"}}},
	}
}

// approvalResumeEvents：run2 决议——注记 + 工具完成 + 终态（含 usage 供 W5 断言）。
func approvalResumeEvents(runID string) []*agentv1.Event {
	return []*agentv1.Event{
		{Seq: 1, RunId: runID, MessageId: runID + ":info:1", Type: agentv1.EventType_EVENT_TYPE_TOOL_THOUGHT, TsUnixMs: ts, IsFinal: true,
			Payload: &agentv1.Event_ToolThought{ToolThought: &agentv1.ThoughtPayload{Text: "人工审批已批准 ✅"}}},
		{Seq: 2, RunId: runID, MessageId: runID + ":result", Type: agentv1.EventType_EVENT_TYPE_RESULT, TsUnixMs: ts, IsFinal: true, Finish: true,
			Payload: &agentv1.Event_Result{Result: &agentv1.ResultPayload{Text: "已执行，完成", Usage: &agentv1.UsageInfo{InputTokens: 120, OutputTokens: 30, ModelCalls: 2}}}},
	}
}

func discardLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// e2eEnv 端到端测试环境：真实 PG（TEST_PG_DSN，未设则 Skip）+ bufconn 假认知面 + 完整路由。
// 三个 e2e 测试共用，避免装配代码三份漂移（NewRouter 签名/TRUNCATE 表清单只改一处）。
type e2eEnv struct {
	ctx    context.Context
	runs   store.RunRepository
	events store.EventRepository
	router http.Handler
	cog    *fakeCog
}

func newE2EEnv(t *testing.T) *e2eEnv {
	t.Helper()
	dsn := os.Getenv("TEST_PG_DSN")
	if dsn == "" {
		t.Skip("TEST_PG_DSN 未设置，跳过端到端集成测试")
	}
	// 防误删开发库硬闸：库名须含 test（这些测试会 TRUNCATE runs/events）。
	if db := dsn[strings.LastIndex(dsn, "/")+1:]; !strings.Contains(db, "test") {
		t.Fatalf("拒绝对库 %q 跑会 TRUNCATE 的集成测试（会清空开发库对话历史）——TEST_PG_DSN 须指向含 test 的库", db)
	}
	ctx := context.Background()
	pool, err := store.NewPool(ctx, dsn)
	if err != nil {
		t.Fatalf("NewPool: %v", err)
	}
	t.Cleanup(pool.Close)
	if err := store.Migrate(ctx, pool); err != nil {
		t.Fatalf("Migrate: %v", err)
	}
	if _, err := pool.Exec(ctx, `TRUNCATE events, runs, session_forks, users, auth_sessions CASCADE`); err != nil {
		t.Fatalf("truncate: %v", err)
	}

	// 进程内 gRPC 假认知面（bufconn）。
	lis := bufconn.Listen(1 << 20)
	gsrv := grpc.NewServer()
	cog := &fakeCog{}
	agentv1.RegisterCognitionServiceServer(gsrv, cog)
	go func() { _ = gsrv.Serve(lis) }()
	t.Cleanup(gsrv.Stop)
	conn, err := grpc.NewClient("passthrough:///bufnet",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return lis.DialContext(ctx) }),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatalf("grpc client: %v", err)
	}
	client := cognition.NewClient(conn)

	runs := store.NewRunRepository(pool)
	events := store.NewEventRepository(pool)
	hub := stream.NewHub(events, time.Hour, discardLogger())
	d := dispatch.New(4, runs, client, hub, 40, discardLogger())
	// D3：装配真实 user/token repo（e2e 覆盖注册→登录→带 token run 归属正确）。
	router := api.NewRouter(d, runs, store.NewSessionRepository(pool), events, nil, nil, nil, nil, nil, nil, nil,
		store.NewUserRepository(pool), store.NewSessionTokenRepository(pool), nil, nil, time.Minute, "", discardLogger())
	return &e2eEnv{ctx: ctx, runs: runs, events: events, router: router, cog: cog}
}

// D3 e2e（docs/17 §6）：注册 → 带 token 发 run → run 归属 = token 身份（且 X-User-Id 头被忽略）。
func TestEndToEnd_AuthTokenOwnership(t *testing.T) {
	env := newE2EEnv(t)

	// 注册取 token。
	rec := httptest.NewRequest(http.MethodPost, "/auth/register",
		strings.NewReader(`{"username":"u-tok","password":"secret123"}`))
	rec.Header.Set("Content-Type", "application/json")
	regRec := httptest.NewRecorder()
	env.router.ServeHTTP(regRec, rec)
	if regRec.Code != http.StatusOK {
		t.Fatalf("register: %d %s", regRec.Code, regRec.Body.String())
	}
	var reg struct{ Token string }
	_ = json.Unmarshal(regRec.Body.Bytes(), &reg)
	if reg.Token == "" {
		t.Fatal("register 无 token")
	}

	// 带 token 发 run，且故意附一个不同的 X-User-Id 头——token 身份须胜出（头被忽略）。
	req := httptest.NewRequest(http.MethodPost, "/runs", strings.NewReader(`{"query":"算 2*(3+4)","sessionId":"s-tok"}`))
	req.Header.Set("Authorization", "Bearer "+reg.Token)
	req.Header.Set("X-User-Id", "someone-else")
	runRec := httptest.NewRecorder()
	env.router.ServeHTTP(runRec, req)
	runID := runRec.Header().Get("X-Run-Id")
	if runID == "" {
		t.Fatalf("run 未启动: %d %s", runRec.Code, runRec.Body.String())
	}
	// 认知面收到的 owner_id = token 身份。
	if got := env.cog.lastReq(); got == nil || got.GetMetadata()["owner_id"] != "u-tok" {
		t.Fatalf("owner_id 应为 token 身份 u-tok: %v", env.cog.lastReq().GetMetadata())
	}
	// 落库 run 归属 u-tok（而非 X-User-Id 头的 someone-else）。
	run, err := env.runs.GetRun(env.ctx, runID)
	if err != nil || run.OwnerID != "u-tok" {
		t.Fatalf("run 归属应为 u-tok: %+v err=%v", run, err)
	}
}

// M8：附件引用与 owner 元数据贯通 startRun→gRPC；伪造 key 在 SSE 开始前被 403。
func TestEndToEnd_AttachmentsAndOwnerMetadata(t *testing.T) {
	env := newE2EEnv(t)

	// 1) 合法附件：透传至 proto RunRequest（attachments + metadata.owner_id/output_format/agent_type）。
	body := `{"query":"这份文档讲什么","sessionId":"sess-att","agentType":"deep_research","outputFormat":"table","imageGen":true,` +
		`"attachments":[{"resourceKey":"uploads/u1/sess-att/ab12-doc.txt","fileName":"doc.txt","mimeType":"text/plain","size":6}]}`
	req := httptest.NewRequest(http.MethodPost, "/runs", strings.NewReader(body))
	req.Header.Set("X-User-Id", "u1")
	rec := httptest.NewRecorder()
	env.router.ServeHTTP(rec, req)
	if rec.Header().Get("X-Run-Id") == "" {
		t.Fatalf("run did not start: %d %s", rec.Code, rec.Body.String())
	}
	got := env.cog.lastReq()
	if got == nil {
		t.Fatal("fakeCog 未收到请求")
	}
	atts := got.GetAttachments()
	if len(atts) != 1 || atts[0].GetResourceKey() != "uploads/u1/sess-att/ab12-doc.txt" ||
		atts[0].GetFileName() != "doc.txt" || atts[0].GetMimeType() != "text/plain" || atts[0].GetSize() != 6 {
		t.Fatalf("attachments 未贯通: %+v", atts)
	}
	if got.GetMetadata()["owner_id"] != "u1" {
		t.Fatalf("owner_id 未进 metadata: %v", got.GetMetadata())
	}
	// M9：三档白名单放行 deep_research；输出格式经 metadata 透传。
	if got.GetAgentType() != "deep_research" {
		t.Fatalf("deep_research 未过白名单: %s", got.GetAgentType())
	}
	if got.GetMetadata()["output_format"] != "table" {
		t.Fatalf("output_format 未进 metadata: %v", got.GetMetadata())
	}
	// 生图开关经 metadata["image_gen"]="1" 透传。
	if got.GetMetadata()["image_gen"] != "1" {
		t.Fatalf("image_gen 未进 metadata: %v", got.GetMetadata())
	}

	// 2) 伪造他人 key → 403，且不落 run、不发 SSE。
	forged := `{"query":"x","attachments":[{"resourceKey":"uploads/u2/s/zz-secret.txt"}]}`
	req2 := httptest.NewRequest(http.MethodPost, "/runs", strings.NewReader(forged))
	req2.Header.Set("X-User-Id", "u1")
	rec2 := httptest.NewRecorder()
	env.router.ServeHTTP(rec2, req2)
	if rec2.Code != http.StatusForbidden || rec2.Header().Get("X-Run-Id") != "" {
		t.Fatalf("forged attachment: expected 403 wo/ run, got %d %s", rec2.Code, rec2.Body.String())
	}
	// 运行产物 key 同样拒绝（只认自己的 uploads/）。
	forged2 := `{"query":"x","attachments":[{"resourceKey":"someRunId/tc1/report.md"}]}`
	req3 := httptest.NewRequest(http.MethodPost, "/runs", strings.NewReader(forged2))
	req3.Header.Set("X-User-Id", "u1")
	rec3 := httptest.NewRecorder()
	env.router.ServeHTTP(rec3, req3)
	if rec3.Code != http.StatusForbidden {
		t.Fatalf("artifact-key attachment: expected 403, got %d", rec3.Code)
	}
}

// parseSSE 提取所有 `event: message` 帧的 data JSON（跳过心跳）。
func parseSSE(body string) []map[string]any {
	var frames []map[string]any
	for _, block := range strings.Split(body, "\n\n") {
		if !strings.Contains(block, "event: message") {
			continue
		}
		for _, line := range strings.Split(block, "\n") {
			if strings.HasPrefix(line, "data: ") {
				var m map[string]any
				if err := json.Unmarshal([]byte(strings.TrimPrefix(line, "data: ")), &m); err == nil {
					frames = append(frames, m)
				}
			}
		}
	}
	return frames
}

// 全链路：POST /runs → dispatch → hub → store(真实 PG) → SSE；再 GET 回放，断言重放==实时。
func TestEndToEnd_RunAndReplay(t *testing.T) {
	env := newE2EEnv(t)
	ctx, runs, events, router := env.ctx, env.runs, env.events, env.router

	// 1) 发起 run，捕获 SSE。
	req := httptest.NewRequest(http.MethodPost, "/runs", strings.NewReader(`{"query":"算 2*(3+4)","sessionId":"s1"}`))
	req.Header.Set("X-User-Id", "u1")
	rec := httptest.NewRecorder()
	router.ServeHTTP(rec, req)

	runID := rec.Header().Get("X-Run-Id")
	if runID == "" {
		t.Fatalf("missing X-Run-Id")
	}
	live := parseSSE(rec.Body.String())
	if len(live) != 5 {
		t.Fatalf("expected 5 live frames, got %d: %s", len(live), rec.Body.String())
	}
	wantTypes := []string{"tool_thought", "tool_call", "tool_call", "tool_result", "result"}
	for i, f := range live {
		if f["messageType"] != wantTypes[i] {
			t.Fatalf("frame %d type=%v want %s", i, f["messageType"], wantTypes[i])
		}
		if int(f["seq"].(float64)) != i+1 {
			t.Fatalf("frame %d seq=%v", i, f["seq"])
		}
		if f["requestId"] != runID {
			t.Fatalf("frame %d requestId=%v want %s", i, f["requestId"], runID)
		}
	}
	if live[4]["finish"] != true {
		t.Fatalf("last frame finish should be true")
	}
	if live[0]["finish"] == true {
		t.Fatalf("non-result frame finish should be false")
	}

	// 2) run 终态应为 SUCCESS，且落库事件满足序列不变量（seq 无空洞、finish 仅 result 且恰一条）。
	run, err := runs.GetRun(ctx, runID)
	if err != nil || run.Status != store.StatusSuccess {
		t.Fatalf("expected SUCCESS run, got %+v err=%v", run, err)
	}
	persisted, err := events.ListByRun(ctx, runID)
	if err != nil {
		t.Fatalf("ListByRun: %v", err)
	}
	if err := event.ValidateSequence(persisted); err != nil {
		t.Fatalf("ValidateSequence: %v", err)
	}

	// 3) 回放，断言与实时逐帧一致（重放==实时）。
	rreq := httptest.NewRequest(http.MethodGet, "/runs/"+runID+"/events", nil)
	rreq.Header.Set("X-User-Id", "u1")
	rrec := httptest.NewRecorder()
	router.ServeHTTP(rrec, rreq)
	replay := parseSSE(rrec.Body.String())
	if !reflect.DeepEqual(replay, live) {
		t.Fatalf("replay != live\nlive:   %v\nreplay: %v", live, replay)
	}

	// 4) 越权回放应 403。
	freq := httptest.NewRequest(http.MethodGet, "/runs/"+runID+"/events", nil)
	freq.Header.Set("X-User-Id", "intruder")
	frec := httptest.NewRecorder()
	router.ServeHTTP(frec, freq)
	if frec.Code != http.StatusForbidden {
		t.Fatalf("expected 403 for other owner, got %d", frec.Code)
	}
}

// 跨 agent_type 回放同构：plan_solve（plan_thought/plan/task/tool_*/result）重放==实时 + 序列不变量。
func TestEndToEnd_PlanSolveReplay(t *testing.T) {
	env := newE2EEnv(t)
	ctx, runs, events, router := env.ctx, env.runs, env.events, env.router

	req := httptest.NewRequest(http.MethodPost, "/runs", strings.NewReader(`{"query":"算 2+3 并汇总","sessionId":"s2","agentType":"plan_solve"}`))
	req.Header.Set("X-User-Id", "u1")
	rec := httptest.NewRecorder()
	router.ServeHTTP(rec, req)

	runID := rec.Header().Get("X-Run-Id")
	live := parseSSE(rec.Body.String())
	wantTypes := []string{"plan_thought", "plan", "task", "tool_call", "tool_call", "tool_result", "result"}
	if len(live) != len(wantTypes) {
		t.Fatalf("expected %d frames, got %d: %s", len(wantTypes), len(live), rec.Body.String())
	}
	for i, f := range live {
		if f["messageType"] != wantTypes[i] {
			t.Fatalf("frame %d type=%v want %s", i, f["messageType"], wantTypes[i])
		}
	}
	if live[len(live)-1]["finish"] != true {
		t.Fatalf("last frame finish should be true")
	}

	run, err := runs.GetRun(ctx, runID)
	if err != nil || run.Status != store.StatusSuccess || run.EntryAgent != "plan_solve" {
		t.Fatalf("expected SUCCESS plan_solve run, got %+v err=%v", run, err)
	}

	persisted, err := events.ListByRun(ctx, runID)
	if err != nil {
		t.Fatalf("ListByRun: %v", err)
	}
	if err := event.ValidateSequence(persisted); err != nil {
		t.Fatalf("ValidateSequence: %v", err)
	}

	rreq := httptest.NewRequest(http.MethodGet, "/runs/"+runID+"/events", nil)
	rreq.Header.Set("X-User-Id", "u1")
	rrec := httptest.NewRecorder()
	router.ServeHTTP(rrec, rreq)
	replay := parseSSE(rrec.Body.String())
	if !reflect.DeepEqual(replay, live) {
		t.Fatalf("plan_solve replay != live\nlive:   %v\nreplay: %v", live, replay)
	}
}

// M11：HITL 审批全链路——run1 挂起（approval 帧入账本可回放）→ /approvals 决议
// 即新 run 的 SSE 流（resume metadata 贯通）→ 越权/缺参矩阵。
func TestEndToEnd_ApprovalFlow(t *testing.T) {
	env := newE2EEnv(t)

	// run1：触发挂起。
	req := httptest.NewRequest(http.MethodPost, "/runs", strings.NewReader(`{"query":"这个操作需要审批","sessionId":"s-ap"}`))
	req.Header.Set("X-User-Id", "u1")
	rec := httptest.NewRecorder()
	env.router.ServeHTTP(rec, req)
	run1 := rec.Header().Get("X-Run-Id")
	frames := parseSSE(rec.Body.String())
	var approval map[string]any
	for _, f := range frames {
		if f["messageType"] == "approval_request" {
			approval = f
		}
	}
	if approval == nil {
		t.Fatalf("run1 未见 approval_request 帧: %v", frames)
	}
	ap := approval["approval"].(map[string]any)
	if ap["approvalId"] != "ap-1" || ap["toolName"] != "calculator" {
		t.Fatalf("approval 载荷不对: %v", ap)
	}
	pend := ap["pendingToolCallIds"].([]any)
	if len(pend) != 1 || pend[0] != "tc-danger" {
		t.Fatalf("pendingToolCallIds 不对: %v", pend)
	}

	// 回放同构：approval 帧持久化可重现。
	rr := httptest.NewRequest(http.MethodGet, "/runs/"+run1+"/events", nil)
	rr.Header.Set("X-User-Id", "u1")
	rrec := httptest.NewRecorder()
	env.router.ServeHTTP(rrec, rr)
	replayHasApproval := false
	for _, f := range parseSSE(rrec.Body.String()) {
		if f["messageType"] == "approval_request" {
			replayHasApproval = true
		}
	}
	if !replayHasApproval {
		t.Fatal("回放缺 approval_request 帧（Marshal/Unmarshal 断链）")
	}

	// 决议：POST /runs/{run1}/approvals → 新 run SSE。
	ar := httptest.NewRequest(http.MethodPost, "/runs/"+run1+"/approvals",
		strings.NewReader(`{"approvalId":"ap-1","approved":true,"comment":"放行"}`))
	ar.Header.Set("X-User-Id", "u1")
	arec := httptest.NewRecorder()
	env.router.ServeHTTP(arec, ar)
	run2 := arec.Header().Get("X-Run-Id")
	if run2 == "" || run2 == run1 {
		t.Fatalf("决议应开启新 run: %q", run2)
	}
	got := env.cog.lastReq()
	md := got.GetMetadata()
	if md["approval_resume_id"] != "ap-1" || md["approval_decision"] != "approved" || md["approval_comment"] != "放行" {
		t.Fatalf("resume metadata 未贯通: %v", md)
	}
	if got.GetSessionId() != "s-ap" || !strings.Contains(got.GetQuery(), "[审批] 通过") {
		t.Fatalf("resume run 上下文不对: session=%s query=%s", got.GetSessionId(), got.GetQuery())
	}
	finalSeen := false
	for _, f := range parseSSE(arec.Body.String()) {
		if f["messageType"] == "result" && f["finish"] == true {
			finalSeen = true
		}
	}
	if !finalSeen {
		t.Fatal("run2 未见终态 result")
	}

	// 越权：他人决议 → 403；缺 approvalId → 400。
	bad := httptest.NewRequest(http.MethodPost, "/runs/"+run1+"/approvals",
		strings.NewReader(`{"approvalId":"ap-1","approved":true}`))
	bad.Header.Set("X-User-Id", "intruder")
	brec := httptest.NewRecorder()
	env.router.ServeHTTP(brec, bad)
	if brec.Code != http.StatusForbidden {
		t.Fatalf("他人决议应 403: %d", brec.Code)
	}
	missing := httptest.NewRequest(http.MethodPost, "/runs/"+run1+"/approvals", strings.NewReader(`{"approved":true}`))
	missing.Header.Set("X-User-Id", "u1")
	mrec := httptest.NewRecorder()
	env.router.ServeHTTP(mrec, missing)
	if mrec.Code != http.StatusBadRequest {
		t.Fatalf("缺 approvalId 应 400: %d", mrec.Code)
	}
}
