package poller_test

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"my-agent/control-plane/internal/cognition"
	"my-agent/control-plane/internal/connector"
	"my-agent/control-plane/internal/dispatch"
	"my-agent/control-plane/internal/event"
	"my-agent/control-plane/internal/poller"
	"my-agent/control-plane/internal/secret"
	"my-agent/control-plane/internal/store"
	"my-agent/control-plane/internal/stream"
)

func discardLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// —— fake connector：Poll 返回固定事件 —— //
type fakeConnector struct {
	events []connector.RawEvent
	cursor string
}

func (f *fakeConnector) Authorize(context.Context, string, string) error { return nil }
func (f *fakeConnector) Poll(_ context.Context, _ connector.Conn) ([]connector.RawEvent, string, error) {
	return f.events, f.cursor, nil
}
func (f *fakeConnector) Normalize(raw connector.RawEvent) connector.InternalEvent {
	return connector.InternalEvent{
		Type:   "issue",
		Fields: map[string]string{"title": raw.Title, "repo": raw.Repo, "url": raw.URL},
	}
}

// —— fake connectors repo：记录 Claim / UpdateCursor（mutex 护栏，供跨 goroutine 断言） —— //
type fakeConnRepo struct {
	mu      sync.Mutex
	due     []store.Connector
	persist bool // true：ListDue 每拍都返回同一批（模拟连接器反复到期，供重叠护栏/重试测试）
	claimed []string
	cuID    string
	cuCur   string
	cuLast  string
	cuDone  bool
}

func (f *fakeConnRepo) Create(context.Context, store.Connector) error { return nil }
func (f *fakeConnRepo) ListByOwner(context.Context, string) ([]store.Connector, error) {
	return nil, nil
}
func (f *fakeConnRepo) Delete(context.Context, string, string) error           { return nil }
func (f *fakeConnRepo) SetEnabled(context.Context, string, string, bool) error { return nil }
func (f *fakeConnRepo) ListDue(_ context.Context, _ int) ([]store.Connector, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := f.due
	if !f.persist {
		f.due = nil // 只放一批（后续 tick 无到期，避免重复触发）
	}
	return out, nil
}
func (f *fakeConnRepo) Claim(_ context.Context, id string) (bool, error) {
	f.mu.Lock()
	f.claimed = append(f.claimed, id)
	f.mu.Unlock()
	return true, nil
}
func (f *fakeConnRepo) UpdateCursor(_ context.Context, id, cursor, lastPoll string) error {
	f.mu.Lock()
	f.cuID, f.cuCur, f.cuLast, f.cuDone = id, cursor, lastPoll, true
	f.mu.Unlock()
	return nil
}
func (f *fakeConnRepo) cursorDone() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.cuDone
}
func (f *fakeConnRepo) claimCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.claimed)
}

// —— scriptedCog：可脚本化的认知客户端——前 failFirst 次 RunAgent 返回 error（模拟认知面
// 短暂不可用）；block 非 nil 时 RunAgent 阻塞至其关闭（模拟慢 run，供重叠护栏测试）。 —— //
type scriptedCog struct {
	mu        sync.Mutex
	calls     int
	failFirst int
	block     chan struct{}
}

func (c *scriptedCog) RunAgent(_ context.Context, _ cognition.RunRequest) (cognition.Stream, error) {
	c.mu.Lock()
	c.calls++
	n := c.calls
	block := c.block
	c.mu.Unlock()
	if block != nil {
		<-block // 阻塞：模拟运行时长 > poll_interval 的慢 run
	}
	if n <= c.failFirst {
		return nil, errors.New("cognition unavailable")
	}
	return &eofStream{}, nil
}
func (c *scriptedCog) IngestDocument(context.Context, string, cognition.Attachment) (bool, string, string, error) {
	return false, "", "", nil
}
func (c *scriptedCog) HealthCheck(context.Context) error { return nil }
func (c *scriptedCog) Close() error                      { return nil }
func (c *scriptedCog) callCount() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.calls
}

// —— fake triggers repo —— //
type fakeTrigRepo struct{ byConnector map[string][]store.Trigger }

func (f *fakeTrigRepo) Create(context.Context, store.Trigger) error                  { return nil }
func (f *fakeTrigRepo) ListByOwner(context.Context, string) ([]store.Trigger, error) { return nil, nil }
func (f *fakeTrigRepo) Delete(context.Context, string, string) error                 { return nil }
func (f *fakeTrigRepo) SetEnabled(context.Context, string, string, bool) error       { return nil }
func (f *fakeTrigRepo) ListByConnector(_ context.Context, id string) ([]store.Trigger, error) {
	return f.byConnector[id], nil
}

// —— fake cognition client：捕获 RunRequest（= 渲染后的 StartCommand 投影） —— //
type fakeCog struct {
	mu       sync.Mutex
	requests []cognition.RunRequest
}

func (c *fakeCog) RunAgent(_ context.Context, req cognition.RunRequest) (cognition.Stream, error) {
	c.mu.Lock()
	c.requests = append(c.requests, req)
	c.mu.Unlock()
	return &eofStream{}, nil
}
func (c *fakeCog) IngestDocument(context.Context, string, cognition.Attachment) (bool, string, string, error) {
	return false, "", "", nil
}
func (c *fakeCog) HealthCheck(context.Context) error { return nil }
func (c *fakeCog) Close() error                      { return nil }
func (c *fakeCog) snapshot() []cognition.RunRequest {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]cognition.RunRequest(nil), c.requests...)
}

type eofStream struct{}

func (eofStream) Recv() (event.Envelope, error) { return event.Envelope{}, io.EOF }

// —— fake run/event repos（Dispatcher 依赖） —— //
type fakeRuns struct {
	mu      sync.Mutex
	created []store.CreateRunParams
}

func (r *fakeRuns) CreateRun(_ context.Context, p store.CreateRunParams) error {
	r.mu.Lock()
	r.created = append(r.created, p)
	r.mu.Unlock()
	return nil
}
func (r *fakeRuns) FinishRun(context.Context, store.FinishRunParams) error { return nil }
func (r *fakeRuns) GetRun(context.Context, string) (store.Run, error) {
	return store.Run{}, store.ErrRunNotFound
}
func (r *fakeRuns) snapshot() []store.CreateRunParams {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]store.CreateRunParams(nil), r.created...)
}

type fakeEvents struct{}

func (fakeEvents) Append(context.Context, event.Envelope) error { return nil }
func (fakeEvents) ListByRun(context.Context, string) ([]event.Envelope, error) {
	return nil, nil
}

// 端到端匹配：fake github Poll 返回固定事件 → poller 认领(Claim)、渲染 query_template、
// 起 run（断言 RunRequest 里的 Query/SessionID/OwnerID/AgentType）、推进游标(UpdateCursor)。
func TestPollerMatchAndFire(t *testing.T) {
	key := make([]byte, secret.KeySize)
	ct, err := secret.Seal(key, []byte("ghp_x"))
	if err != nil {
		t.Fatalf("seal: %v", err)
	}

	connRepo := &fakeConnRepo{due: []store.Connector{{
		ConnectorID: "c1", OwnerID: "owner-1", Kind: "github",
		TokenCiphertext: ct, PollIntervalS: 60, Enabled: true, Cursor: "old",
	}}}
	trigRepo := &fakeTrigRepo{byConnector: map[string][]store.Trigger{
		"c1": {
			{TriggerID: "trig-abcdef12", OwnerID: "owner-1", ConnectorID: "c1", EventType: "issue",
				Filter: json.RawMessage(`{"repo":"o/r"}`), QueryTemplate: "回复 issue：{{title}}（{{repo}}）",
				AgentType: "plan_solve", NeedsApproval: false, Enabled: true},
			// 不匹配（repo 不符）——不得起 run。
			{TriggerID: "trig-nomatch1", OwnerID: "owner-1", ConnectorID: "c1", EventType: "issue",
				Filter: json.RawMessage(`{"repo":"o/other"}`), QueryTemplate: "x", AgentType: "react", Enabled: true},
			// 禁用——不得起 run。
			{TriggerID: "trig-disabled", OwnerID: "owner-1", ConnectorID: "c1", EventType: "issue",
				QueryTemplate: "y", AgentType: "react", Enabled: false},
		},
	}}
	fconn := &fakeConnector{
		events: []connector.RawEvent{{ID: "n1", SubjectType: "Issue", Title: "登录报错", Repo: "o/r", URL: "u"}},
		cursor: "2026-07-05T12:00:00Z",
	}
	cog := &fakeCog{}
	runs := &fakeRuns{}
	hub := stream.NewHub(fakeEvents{}, time.Hour, discardLogger())
	d := dispatch.New(4, runs, cog, hub, 40, discardLogger())

	p := poller.New(connRepo, trigRepo, d, fconn, key, time.Minute, 5*time.Millisecond, 2, discardLogger())
	runUntil(t, p, connRepo.cursorDone)

	// Claim 按连接器调用一次。
	connRepo.mu.Lock()
	claimed := append([]string(nil), connRepo.claimed...)
	connRepo.mu.Unlock()
	if len(claimed) != 1 || claimed[0] != "c1" {
		t.Fatalf("Claim 未按连接器调用: %+v", claimed)
	}
	// 恰起一个 run（仅命中的 enabled 规则）。
	reqs := cog.snapshot()
	if len(reqs) != 1 {
		t.Fatalf("应恰起 1 个 run，得 %d：%+v", len(reqs), reqs)
	}
	req := reqs[0]
	if req.Query != "回复 issue：登录报错（o/r）" {
		t.Fatalf("query_template 渲染不对: %q", req.Query)
	}
	if req.OwnerID != "owner-1" {
		t.Fatalf("run 归属应为连接器 owner，得 %q", req.OwnerID)
	}
	if req.AgentType != "plan_solve" {
		t.Fatalf("agentType 应取规则值，得 %q", req.AgentType)
	}
	if req.SessionID != "trig-trig-abc" { // "trig-" + triggerID[:8]("trig-abc")
		t.Fatalf("sessionId 应服务端生成 trig-<id[:8]>，得 %q", req.SessionID)
	}
	// CreateRun 用同一 SessionID/OwnerID 落库。
	created := runs.snapshot()
	if len(created) != 1 || created[0].OwnerID != "owner-1" || created[0].SessionID != req.SessionID {
		t.Fatalf("CreateRun 不对: %+v", created)
	}
	// 游标推进（newCursor + lastPollID=最后事件 id）。
	connRepo.mu.Lock()
	defer connRepo.mu.Unlock()
	if connRepo.cuCur != "2026-07-05T12:00:00Z" || connRepo.cuLast != "n1" {
		t.Fatalf("UpdateCursor 不对: cur=%q last=%q", connRepo.cuCur, connRepo.cuLast)
	}
}

// needs_approval=true → 渲染的 query 前缀带审批引导（走 M11 HITL）。
func TestPollerNeedsApprovalPrefix(t *testing.T) {
	key := make([]byte, secret.KeySize)
	ct, _ := secret.Seal(key, []byte("p"))
	connRepo := &fakeConnRepo{due: []store.Connector{{
		ConnectorID: "c1", OwnerID: "o1", Kind: "github", TokenCiphertext: ct, PollIntervalS: 60, Enabled: true,
	}}}
	trigRepo := &fakeTrigRepo{byConnector: map[string][]store.Trigger{
		"c1": {{TriggerID: "t1", OwnerID: "o1", ConnectorID: "c1", EventType: "issue",
			QueryTemplate: "起草回复", AgentType: "react", NeedsApproval: true, Enabled: true}},
	}}
	fconn := &fakeConnector{events: []connector.RawEvent{{ID: "n1", SubjectType: "Issue", Title: "T", Repo: "o/r"}}, cursor: "c"}
	cog := &fakeCog{}
	hub := stream.NewHub(fakeEvents{}, time.Hour, discardLogger())
	d := dispatch.New(4, &fakeRuns{}, cog, hub, 40, discardLogger())
	p := poller.New(connRepo, trigRepo, d, fconn, key, time.Minute, 5*time.Millisecond, 2, discardLogger())
	runUntil(t, p, connRepo.cursorDone)

	reqs := cog.snapshot()
	if len(reqs) != 1 {
		t.Fatalf("应起 1 run，得 %d", len(reqs))
	}
	if q := reqs[0].Query; q == "起草回复" || len(q) <= len("起草回复") {
		t.Fatalf("needs_approval 应加审批前缀，得 %q", q)
	}
}

// #4 重叠护栏：慢触发 run（时长>poll_interval）在途期间，连接器反复到期也不得重复触发同事件。
// 构造：ListDue 每拍都返回同一连接器（persist），首个 run 在 RunAgent 处阻塞（慢 run）；
// 断言在阻塞期间——尽管连接器反复到期——只认领一次、只起一个 run（无重复取回/重复起 run）。
func TestPollerOverlapGuardSlowRun(t *testing.T) {
	key := make([]byte, secret.KeySize)
	ct, _ := secret.Seal(key, []byte("p"))
	connRepo := &fakeConnRepo{persist: true, due: []store.Connector{{
		ConnectorID: "c1", OwnerID: "o1", Kind: "github", TokenCiphertext: ct, PollIntervalS: 60, Enabled: true, Cursor: "old",
	}}}
	trigRepo := &fakeTrigRepo{byConnector: map[string][]store.Trigger{
		"c1": {{TriggerID: "t1", OwnerID: "o1", ConnectorID: "c1", EventType: "issue",
			QueryTemplate: "回复 {{title}}", AgentType: "react", Enabled: true}},
	}}
	fconn := &fakeConnector{events: []connector.RawEvent{{ID: "n1", SubjectType: "Issue", Title: "同一事件", Repo: "o/r"}}, cursor: "c"}
	cog := &scriptedCog{block: make(chan struct{})} // 慢 run：RunAgent 阻塞
	hub := stream.NewHub(fakeEvents{}, time.Hour, discardLogger())
	d := dispatch.New(8, &fakeRuns{}, cog, hub, 40, discardLogger())
	p := poller.New(connRepo, trigRepo, d, fconn, key, time.Minute, 5*time.Millisecond, 4, discardLogger())

	ctx, cancel := context.WithCancel(context.Background())
	stopped := make(chan struct{})
	go func() { p.Run(ctx); close(stopped) }()

	// 等首个（阻塞的）run 起来。
	waitTrue(t, func() bool { return cog.callCount() >= 1 }, "首个 run 未起")
	// 慢 run 在途期间多跑若干拍（~16 拍 @5ms）：重叠护栏应阻止再次认领/触发同事件。
	time.Sleep(80 * time.Millisecond)
	if n := cog.callCount(); n != 1 {
		close(cog.block)
		cancel()
		<-stopped
		t.Fatalf("慢 run 在途期间同事件被重复触发：RunAgent 调用 %d 次（应为 1）", n)
	}
	if n := connRepo.claimCount(); n != 1 {
		close(cog.block)
		cancel()
		<-stopped
		t.Fatalf("慢 run 在途期间连接器被重复认领：Claim %d 次（应为 1）", n)
	}
	// 收尾：放行阻塞的 run。
	close(cog.block)
	cancel()
	<-stopped
}

// #6 派发失败不推进游标：命中事件的 run 起不来（认知面不可用）时，本拍不得 UpdateCursor
// （否则该事件被静默越过、永不重试）。构造：ListDue 只放一批，RunAgent 恒失败。
func TestPollerDispatchFailKeepsCursor(t *testing.T) {
	key := make([]byte, secret.KeySize)
	ct, _ := secret.Seal(key, []byte("p"))
	connRepo := &fakeConnRepo{due: []store.Connector{{
		ConnectorID: "c1", OwnerID: "o1", Kind: "github", TokenCiphertext: ct, PollIntervalS: 60, Enabled: true, Cursor: "old",
	}}}
	trigRepo := &fakeTrigRepo{byConnector: map[string][]store.Trigger{
		"c1": {{TriggerID: "t1", OwnerID: "o1", ConnectorID: "c1", EventType: "issue",
			QueryTemplate: "回复 {{title}}", AgentType: "react", Enabled: true}},
	}}
	fconn := &fakeConnector{events: []connector.RawEvent{{ID: "n1", SubjectType: "Issue", Title: "E", Repo: "o/r"}}, cursor: "newcur"}
	cog := &scriptedCog{failFirst: 1 << 30} // 恒失败
	hub := stream.NewHub(fakeEvents{}, time.Hour, discardLogger())
	d := dispatch.New(4, &fakeRuns{}, cog, hub, 40, discardLogger())
	p := poller.New(connRepo, trigRepo, d, fconn, key, time.Minute, 5*time.Millisecond, 2, discardLogger())

	runUntil(t, p, func() bool { return cog.callCount() >= 1 })

	if connRepo.cursorDone() {
		t.Fatal("派发失败仍推进了游标（命中事件被静默丢弃）")
	}
	if n := cog.callCount(); n != 1 {
		t.Fatalf("应恰尝试起 1 个 run，得 %d", n)
	}
}

// #6 下拍重试：派发失败当拍不推进游标；连接器再度到期时以同游标重取、重试，最终成功后才推进。
// 构造：ListDue 每拍返回（persist），RunAgent 首次失败、其后成功。
func TestPollerDispatchFailRetriesNextTick(t *testing.T) {
	key := make([]byte, secret.KeySize)
	ct, _ := secret.Seal(key, []byte("p"))
	connRepo := &fakeConnRepo{persist: true, due: []store.Connector{{
		ConnectorID: "c1", OwnerID: "o1", Kind: "github", TokenCiphertext: ct, PollIntervalS: 60, Enabled: true, Cursor: "old",
	}}}
	trigRepo := &fakeTrigRepo{byConnector: map[string][]store.Trigger{
		"c1": {{TriggerID: "t1", OwnerID: "o1", ConnectorID: "c1", EventType: "issue",
			QueryTemplate: "回复 {{title}}", AgentType: "react", Enabled: true}},
	}}
	fconn := &fakeConnector{events: []connector.RawEvent{{ID: "n1", SubjectType: "Issue", Title: "E", Repo: "o/r"}}, cursor: "newcur"}
	cog := &scriptedCog{failFirst: 1} // 首次失败，其后成功
	hub := stream.NewHub(fakeEvents{}, time.Hour, discardLogger())
	d := dispatch.New(4, &fakeRuns{}, cog, hub, 40, discardLogger())
	p := poller.New(connRepo, trigRepo, d, fconn, key, time.Minute, 5*time.Millisecond, 2, discardLogger())

	runUntil(t, p, connRepo.cursorDone) // 只有成功派发后才推进游标

	if n := cog.callCount(); n < 2 {
		t.Fatalf("失败事件应被下拍重试（RunAgent 应≥2 次），得 %d", n)
	}
	if connRepo.cuCur != "newcur" {
		t.Fatalf("重试成功后游标应推进到 newcur，得 %q", connRepo.cuCur)
	}
}

// waitTrue 轮询等待 cond 为真，超时 fatal（不停机——调用方负责收尾）。
func waitTrue(t *testing.T, cond func() bool, msg string) {
	t.Helper()
	deadline := time.After(3 * time.Second)
	for !cond() {
		select {
		case <-deadline:
			t.Fatal("超时：" + msg)
		case <-time.After(1 * time.Millisecond):
		}
	}
}

// runUntil 跑 poller（短 tick）直到 done() 为真（处理完一批），再优雅停机。
func runUntil(t *testing.T, p *poller.Poller, done func() bool) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	stopped := make(chan struct{})
	go func() { p.Run(ctx); close(stopped) }()
	deadline := time.After(3 * time.Second)
	for !done() {
		select {
		case <-deadline:
			cancel()
			<-stopped
			t.Fatal("超时：poller 未在 3s 内处理完一批")
		case <-time.After(2 * time.Millisecond):
		}
	}
	cancel()
	<-stopped
}
