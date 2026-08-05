package store_test

import (
	"context"
	"testing"

	"my-agent/control-plane/internal/event"
	"my-agent/control-plane/internal/store"
)

// 删除会话：owner 隔离 + events 先于 runs（外键，无 CASCADE）+ 删 0 行判空。
func TestDeleteSession(t *testing.T) {
	pool := testPool(t)
	ctx := context.Background()
	runs := store.NewRunRepository(pool)
	events := store.NewEventRepository(pool)
	sess := store.NewSessionRepository(pool)

	mk := func(runID, sid, owner string) {
		if err := runs.CreateRun(ctx, store.CreateRunParams{RunID: runID, SessionID: sid, OwnerID: owner, EntryAgent: "react", QueryText: "q"}); err != nil {
			t.Fatalf("CreateRun %s: %v", runID, err)
		}
	}
	mk("d1", "sess-A", "u1")
	mk("d2", "sess-A", "u1") // 同会话两 run
	mk("d3", "sess-B", "u1") // 另一会话
	mk("d4", "sess-A", "u2") // 他人的同名会话
	// 给 d1 一条 event，验证外键子表先删（否则删 runs 会因外键失败）。
	if err := events.Append(ctx, event.Envelope{
		RunID: "d1", Seq: 1, MessageID: "m", Type: event.TypeToolThought, TSUnixMs: ts,
		Thought: &event.ThoughtPayload{Text: "x"},
	}); err != nil {
		t.Fatalf("append event: %v", err)
	}

	// u1 删 sess-A → 删 2 个 run（连同 events）；sess-B、u2 的 sess-A 不受影响。
	n, err := sess.DeleteSession(ctx, "u1", "sess-A")
	if err != nil {
		t.Fatalf("DeleteSession: %v", err)
	}
	if n != 2 {
		t.Fatalf("期望删 2 个 run，得 %d", n)
	}
	if _, err := runs.GetRun(ctx, "d1"); err == nil {
		t.Fatal("d1 应已删除")
	}
	if _, err := runs.GetRun(ctx, "d3"); err != nil {
		t.Fatal("sess-B 不该被删")
	}
	if _, err := runs.GetRun(ctx, "d4"); err != nil {
		t.Fatal("他人 sess-A 不该被删")
	}
	// 重复删/不存在会话 → 0 行（上层据此 404）。
	if n2, _ := sess.DeleteSession(ctx, "u1", "sess-A"); n2 != 0 {
		t.Fatalf("重复删应 0 行，得 %d", n2)
	}
	if n3, _ := sess.DeleteSession(ctx, "u1", "nope"); n3 != 0 {
		t.Fatalf("不存在会话应 0 行，得 %d", n3)
	}
}
