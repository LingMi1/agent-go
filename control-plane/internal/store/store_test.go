package store_test

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"reflect"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"

	"my-agent/control-plane/internal/event"
	"my-agent/control-plane/internal/store"
)

const ts = int64(1700000000000)

// GuardTestDSN 是**防误删开发库的硬闸**：这些测试开头会 TRUNCATE runs/events，
// 若 TEST_PG_DSN 指向开发库 my_agent 就会清空真实对话历史（踩过两次的坑）。
// 因此库名必须显式含 "test"（如 my_agent_test，见 `make test-db`），否则直接 Fatal，
// **在 TRUNCATE 之前**挡下。导出给 api 包的集成测试共用。
func GuardTestDSN(t *testing.T, dsn string) {
	t.Helper()
	db := dsn
	if i := strings.LastIndex(db, "/"); i >= 0 {
		db = db[i+1:]
	}
	if i := strings.IndexAny(db, "?"); i >= 0 {
		db = db[:i]
	}
	if !strings.Contains(db, "test") {
		t.Fatalf("拒绝对库 %q 跑会 TRUNCATE 的测试（会清空开发库对话历史）——"+
			"请把 TEST_PG_DSN 指向独立测试库（库名须含 test，如 my_agent_test；见 make test-db）", db)
	}
}

// 集成测试需要一个 PostgreSQL。设置 TEST_PG_DSN 后运行；否则跳过。
// 例：TEST_PG_DSN=postgres://agent:agent_pwd@localhost:55432/my_agent_test go test ./internal/store/...
func testPool(t *testing.T) *pgxpool.Pool {
	t.Helper()
	dsn := os.Getenv("TEST_PG_DSN")
	if dsn == "" {
		t.Skip("TEST_PG_DSN 未设置，跳过 store 集成测试")
	}
	GuardTestDSN(t, dsn)
	ctx := context.Background()
	pool, err := store.NewPool(ctx, dsn)
	if err != nil {
		t.Fatalf("NewPool: %v", err)
	}
	if err := store.Migrate(ctx, pool); err != nil {
		t.Fatalf("Migrate: %v", err)
	}
	if _, err := pool.Exec(ctx, `TRUNCATE events, runs, session_forks CASCADE`); err != nil {
		t.Fatalf("truncate: %v", err)
	}
	t.Cleanup(pool.Close)
	return pool
}

func jsonSemEqual(t *testing.T, a, b []byte) bool {
	t.Helper()
	var x, y any
	if err := json.Unmarshal(a, &x); err != nil {
		t.Fatalf("unmarshal a: %v (%s)", err, a)
	}
	if err := json.Unmarshal(b, &y); err != nil {
		t.Fatalf("unmarshal b: %v (%s)", err, b)
	}
	return reflect.DeepEqual(x, y)
}

func goldenEnvelopes() []event.Envelope {
	in := json.RawMessage(`{"expression":"2*(3+4)"}`)
	return []event.Envelope{
		{Seq: 1, RunID: "r1", MessageID: "r1:think:1", Type: event.TypeToolThought, TSUnixMs: ts, IsFinal: true,
			Thought: &event.ThoughtPayload{Text: "先算一下"}},
		{Seq: 2, RunID: "r1", MessageID: "tc1", Type: event.TypeToolCall, TSUnixMs: ts, IsFinal: false,
			Tool: &event.ToolPayload{ToolCallID: "tc1", ToolName: "calculator", ToolProvider: "local", Status: event.StatusRunning, DispatchIndex: 1, Input: in, Summary: "正在调用 calculator"}},
		{Seq: 3, RunID: "r1", MessageID: "tc1", Type: event.TypeToolCall, TSUnixMs: ts, IsFinal: true,
			Tool: &event.ToolPayload{ToolCallID: "tc1", ToolName: "calculator", ToolProvider: "local", Status: event.StatusSuccess, DispatchIndex: 1, Input: in, Summary: "calculator 调用完成"}},
		{Seq: 4, RunID: "r1", MessageID: "tr1", Type: event.TypeToolResult, TSUnixMs: ts, IsFinal: true,
			Tool: &event.ToolPayload{ToolCallID: "tc1", ToolName: "calculator", Input: in, ToolResult: "14",
				Artifacts: []event.ArtifactRef{{ResourceKey: "k1", Name: "n1", Size: 7}}}},
		{Seq: 5, RunID: "r1", MessageID: "res1", Type: event.TypeResult, TSUnixMs: ts, IsFinal: true, Finish: true,
			Result: &event.ResultPayload{Text: "答案是 14"}},
	}
}

func TestRunLifecycle(t *testing.T) {
	pool := testPool(t)
	ctx := context.Background()
	repo := store.NewRunRepository(pool)

	if err := repo.CreateRun(ctx, store.CreateRunParams{RunID: "run-life", SessionID: "s1", OwnerID: "o1", QueryText: "hi"}); err != nil {
		t.Fatalf("CreateRun: %v", err)
	}
	got, err := repo.GetRun(ctx, "run-life")
	if err != nil {
		t.Fatalf("GetRun: %v", err)
	}
	if got.Status != store.StatusRunning || got.FinishedAt != nil {
		t.Fatalf("expected RUNNING & no finishedAt, got %+v", got)
	}
	if err := repo.FinishRun(ctx, store.FinishRunParams{RunID: "run-life", Status: store.StatusSuccess, FinalSummaryText: "做完了"}); err != nil {
		t.Fatalf("FinishRun: %v", err)
	}
	got, _ = repo.GetRun(ctx, "run-life")
	if got.Status != store.StatusSuccess || got.FinalSummaryText == nil || *got.FinalSummaryText != "做完了" || got.FinishedAt == nil {
		t.Fatalf("unexpected finished run: %+v", got)
	}
	if _, err := repo.GetRun(ctx, "nope"); !errors.Is(err, store.ErrRunNotFound) {
		t.Fatalf("expected ErrRunNotFound, got %v", err)
	}
}

// 头号不变量（存储层）：append 后按 seq 重放，渲染帧与原始帧逐字段一致。
func TestEventAppendListReplay(t *testing.T) {
	pool := testPool(t)
	ctx := context.Background()
	runs := store.NewRunRepository(pool)
	events := store.NewEventRepository(pool)

	if err := runs.CreateRun(ctx, store.CreateRunParams{RunID: "r1", SessionID: "s1", OwnerID: "o1", QueryText: "算 2*(3+4)"}); err != nil {
		t.Fatalf("CreateRun: %v", err)
	}
	golden := goldenEnvelopes()
	for _, e := range golden {
		if err := e.Validate(); err != nil {
			t.Fatalf("golden seq %d invalid: %v", e.Seq, err)
		}
		if err := events.Append(ctx, e); err != nil {
			t.Fatalf("Append seq %d: %v", e.Seq, err)
		}
	}

	got, err := events.ListByRun(ctx, "r1")
	if err != nil {
		t.Fatalf("ListByRun: %v", err)
	}
	if len(got) != len(golden) {
		t.Fatalf("expected %d events, got %d", len(golden), len(got))
	}
	for i := range got {
		if got[i].Seq != uint64(i+1) {
			t.Fatalf("seq order broken at %d: %d", i, got[i].Seq)
		}
		wantFrame, _ := event.ToSSEFrame(golden[i])
		gotFrame, _ := event.ToSSEFrame(got[i])
		if !jsonSemEqual(t, gotFrame, wantFrame) {
			t.Errorf("replay frame mismatch at seq %d\n got: %s\nwant: %s", got[i].Seq, gotFrame, wantFrame)
		}
	}
}

func TestDuplicateSeq(t *testing.T) {
	pool := testPool(t)
	ctx := context.Background()
	runs := store.NewRunRepository(pool)
	events := store.NewEventRepository(pool)

	if err := runs.CreateRun(ctx, store.CreateRunParams{RunID: "rdup", SessionID: "s1", OwnerID: "o1", QueryText: "x"}); err != nil {
		t.Fatalf("CreateRun: %v", err)
	}
	e := event.Envelope{Seq: 1, RunID: "rdup", MessageID: "m", Type: event.TypeToolThought, TSUnixMs: ts, Thought: &event.ThoughtPayload{Text: "x"}}
	if err := events.Append(ctx, e); err != nil {
		t.Fatalf("first append: %v", err)
	}
	if err := events.Append(ctx, e); !errors.Is(err, store.ErrDuplicateSeq) {
		t.Fatalf("expected ErrDuplicateSeq, got %v", err)
	}
}

// Plan-Execute 新事件类型(plan_thought/plan/task)的迁移与往返。
func TestPlanEventsRoundtrip(t *testing.T) {
	pool := testPool(t)
	ctx := context.Background()
	runs := store.NewRunRepository(pool)
	events := store.NewEventRepository(pool)
	if err := runs.CreateRun(ctx, store.CreateRunParams{RunID: "rplan", SessionID: "s1", OwnerID: "o1", QueryText: "x"}); err != nil {
		t.Fatalf("CreateRun: %v", err)
	}
	envs := []event.Envelope{
		{Seq: 1, RunID: "rplan", MessageID: "rplan:plan:1", Type: event.TypePlanThought, TSUnixMs: ts, Thought: &event.ThoughtPayload{Text: "规划中", PlannerRoundID: "pr1"}},
		{Seq: 2, RunID: "rplan", MessageID: "plan1", Type: event.TypePlan, TSUnixMs: ts, IsFinal: true, Plan: &event.PlanPayload{Title: "T", Steps: []string{"A", "B"}, StepStatus: []string{"in_progress", "not_started"}, Notes: []string{"", ""}, PlannerRoundID: "pr1"}},
		{Seq: 3, RunID: "rplan", MessageID: "task1", Type: event.TypeTask, TSUnixMs: ts, IsFinal: true, Task: &event.TaskPayload{Text: "做 A"}},
	}
	for _, e := range envs {
		if err := e.Validate(); err != nil {
			t.Fatalf("seq %d invalid: %v", e.Seq, err)
		}
		if err := events.Append(ctx, e); err != nil {
			t.Fatalf("Append seq %d: %v", e.Seq, err)
		}
	}
	got, err := events.ListByRun(ctx, "rplan")
	if err != nil || len(got) != 3 {
		t.Fatalf("ListByRun: %v (n=%d)", err, len(got))
	}
	for i := range got {
		w, _ := event.ToSSEFrame(envs[i])
		g, _ := event.ToSSEFrame(got[i])
		if !jsonSemEqual(t, g, w) {
			t.Errorf("plan-event roundtrip mismatch seq %d\n got %s\nwant %s", got[i].Seq, g, w)
		}
	}
}
