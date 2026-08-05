package store_test

import (
	"context"
	"os"
	"testing"

	"my-agent/control-plane/internal/event"
	"my-agent/control-plane/internal/store"
)

// 跨会话产物列举：owner 隔离 + 去重（tool_result/result 双份、多图）+ 跳 missing。
func TestArtifactListByOwner(t *testing.T) {
	dsn := os.Getenv("TEST_PG_DSN")
	if dsn == "" {
		t.Skip("TEST_PG_DSN 未设置")
	}
	GuardTestDSN(t, dsn)
	ctx := context.Background()
	pool, err := store.NewPool(ctx, dsn)
	if err != nil {
		t.Fatalf("NewPool: %v", err)
	}
	t.Cleanup(pool.Close)
	if err := store.Migrate(ctx, pool); err != nil {
		t.Fatalf("Migrate: %v", err)
	}
	if _, err := pool.Exec(ctx, `TRUNCATE events, runs CASCADE`); err != nil {
		t.Fatalf("truncate: %v", err)
	}

	runs := store.NewRunRepository(pool)
	events := store.NewEventRepository(pool)
	mkRun := func(id, owner string) {
		if err := runs.CreateRun(ctx, store.CreateRunParams{
			RunID: id, SessionID: "s-" + id, OwnerID: owner, EntryAgent: "react", QueryText: "q",
		}); err != nil {
			t.Fatalf("run %s: %v", id, err)
		}
	}
	// 构造带 artifacts 的事件（Envelope.Append 内部 MarshalPayload 序列化 payload）。
	appendArtifactEvent := func(runID string, seq uint64, mtype event.MessageType, keys []string, ts int64) {
		refs := make([]event.ArtifactRef, len(keys))
		for i, k := range keys {
			refs[i] = event.ArtifactRef{ResourceKey: k, Name: k, FileName: k, DownloadURL: "/artifacts/" + k, MimeType: "image/png", Size: 10}
		}
		env := event.Envelope{RunID: runID, Seq: seq, MessageID: "m" + runID, Type: mtype, IsFinal: true, TSUnixMs: ts}
		if mtype == event.TypeResult {
			env.Finish = true
			env.Result = &event.ResultPayload{Text: "done", Artifacts: refs}
		} else {
			env.Tool = &event.ToolPayload{ToolCallID: "tc", ToolName: "image_generate", Artifacts: refs}
		}
		if err := events.Append(ctx, env); err != nil {
			t.Fatalf("append: %v", err)
		}
	}

	mkRun("r1", "u1")
	mkRun("r2", "u1")
	mkRun("r3", "intruder")
	// r1：同一产物 img-a 出现在 tool_result 与 result（去重后 1 个）；多图 img-b/img-c。
	appendArtifactEvent("r1", 1, event.TypeToolResult, []string{"r1/tc/img-a.png", "r1/tc/img-b.png"}, 1000)
	appendArtifactEvent("r1", 2, event.TypeResult, []string{"r1/tc/img-a.png"}, 1001)
	// r2：一个更晚的产物。
	appendArtifactEvent("r2", 1, event.TypeToolResult, []string{"r2/tc/img-c.png"}, 2000)
	// intruder：不可见。
	appendArtifactEvent("r3", 1, event.TypeResult, []string{"r3/tc/secret.png"}, 3000)

	repo := store.NewArtifactListRepository(pool)
	arts, err := repo.ListByOwner(ctx, "u1", 100, 0, "", "")
	if err != nil {
		t.Fatalf("ListByOwner: %v", err)
	}
	keys := make([]string, len(arts))
	for i, a := range arts {
		keys[i] = a.ResourceKey
	}
	// 去重后 3 个（img-a 只 1 次），最新在前（img-c ts=2000 最新），无 intruder。
	if len(arts) != 3 {
		t.Fatalf("期望 3 个去重产物，得 %v", keys)
	}
	if arts[0].ResourceKey != "r2/tc/img-c.png" {
		t.Fatalf("最新应在首位，得 %v", keys)
	}
	if arts[0].SessionID != "s-r2" {
		t.Fatalf("应带来源 session_id，得 %q", arts[0].SessionID)
	}
	for _, k := range keys {
		if k == "r3/tc/secret.png" {
			t.Fatal("他人产物泄漏")
		}
	}
	// limit 生效。
	one, _ := repo.ListByOwner(ctx, "u1", 1, 0, "", "")
	if len(one) != 1 {
		t.Fatalf("limit=1 应回 1 个，得 %d", len(one))
	}
	// 游标分页（B.11）：以首页末项为游标取下一页，不重不漏。
	page1, _ := repo.ListByOwner(ctx, "u1", 2, 0, "", "")
	if len(page1) != 2 {
		t.Fatalf("首页 limit=2 应回 2 个，得 %d", len(page1))
	}
	last := page1[len(page1)-1]
	page2, _ := repo.ListByOwner(ctx, "u1", 2, last.TSUnixMs, last.ResourceKey, "")
	if len(page2) != 1 {
		t.Fatalf("次页应回剩余 1 个，得 %d", len(page2))
	}
	for _, a := range page1 {
		if a.ResourceKey == page2[0].ResourceKey {
			t.Fatal("分页重复返回同一产物")
		}
	}
}
