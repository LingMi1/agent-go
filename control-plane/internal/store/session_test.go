package store_test

import (
	"context"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"my-agent/control-plane/internal/store"
)

// setCreatedAt 把 run 的 created_at 固定到给定时刻（测试聚合/排序需要确定的时间线）。
func setCreatedAt(t *testing.T, pool *pgxpool.Pool, runID string, at time.Time) {
	t.Helper()
	if _, err := pool.Exec(context.Background(),
		`UPDATE runs SET created_at = $2 WHERE run_id = $1`, runID, at); err != nil {
		t.Fatalf("setCreatedAt %s: %v", runID, err)
	}
}

// seedSessions 造两位 owner 的时间线：
//
//	o1/sA: r1(t0,"第一问",react) r2(t2,"第二问",react)   → runCount=2, title=第一问, lastActive=t2
//	o1/sB: r3(t1,"B 的问题",plan_solve)                  → runCount=1
//	o2/sC: r4(t3,"别人的问题",react)                     → 不得泄给 o1
func seedSessions(t *testing.T, pool *pgxpool.Pool) (t0, t1, t2 time.Time) {
	t.Helper()
	ctx := context.Background()
	repo := store.NewRunRepository(pool)
	base := time.Date(2026, 7, 1, 10, 0, 0, 0, time.UTC)
	t0, t1, t2 = base, base.Add(1*time.Minute), base.Add(2*time.Minute)
	t3 := base.Add(3 * time.Minute)

	rows := []struct {
		run, session, owner, agent, query string
		at                                time.Time
	}{
		{"r1", "sA", "o1", "react", "第一问", t0},
		{"r2", "sA", "o1", "react", "第二问", t2},
		{"r3", "sB", "o1", "plan_solve", "B 的问题", t1},
		{"r4", "sC", "o2", "react", "别人的问题", t3},
	}
	for _, r := range rows {
		if err := repo.CreateRun(ctx, store.CreateRunParams{
			RunID: r.run, SessionID: r.session, OwnerID: r.owner, EntryAgent: r.agent, QueryText: r.query,
		}); err != nil {
			t.Fatalf("CreateRun %s: %v", r.run, err)
		}
		setCreatedAt(t, pool, r.run, r.at)
	}
	return t0, t1, t2
}

func TestListSessions(t *testing.T) {
	pool := testPool(t)
	ctx := context.Background()
	t0, t1, t2 := seedSessions(t, pool)
	repo := store.NewSessionRepository(pool)

	got, err := repo.ListSessions(ctx, "o1", 0) // limit<=0 → 默认值
	if err != nil {
		t.Fatalf("ListSessions: %v", err)
	}
	if len(got) != 2 {
		t.Fatalf("expected 2 sessions for o1, got %d: %+v", len(got), got)
	}
	// 按 lastActiveAt 降序：sA(t2) 在 sB(t1) 前。
	sA, sB := got[0], got[1]
	if sA.SessionID != "sA" || sB.SessionID != "sB" {
		t.Fatalf("order wrong: %s, %s", sA.SessionID, sB.SessionID)
	}
	if sA.RunCount != 2 || sB.RunCount != 1 {
		t.Fatalf("runCount wrong: sA=%d sB=%d", sA.RunCount, sB.RunCount)
	}
	// 标题/entryAgent = 会话首条 run（created_at 最早）。
	if sA.Title != "第一问" || sA.EntryAgent != "react" {
		t.Fatalf("sA title/agent wrong: %q %q", sA.Title, sA.EntryAgent)
	}
	if sB.Title != "B 的问题" || sB.EntryAgent != "plan_solve" {
		t.Fatalf("sB title/agent wrong: %q %q", sB.Title, sB.EntryAgent)
	}
	if !sA.CreatedAt.Equal(t0) || !sA.LastActiveAt.Equal(t2) {
		t.Fatalf("sA times wrong: created=%v lastActive=%v", sA.CreatedAt, sA.LastActiveAt)
	}
	if !sB.CreatedAt.Equal(t1) || !sB.LastActiveAt.Equal(t1) {
		t.Fatalf("sB times wrong: %+v", sB)
	}

	// owner 隔离：o2 只看到 sC。
	got2, err := repo.ListSessions(ctx, "o2", 0)
	if err != nil || len(got2) != 1 || got2[0].SessionID != "sC" {
		t.Fatalf("o2 isolation broken: %+v err=%v", got2, err)
	}

	// limit 生效：只取最新会话。
	top, err := repo.ListSessions(ctx, "o1", 1)
	if err != nil || len(top) != 1 || top[0].SessionID != "sA" {
		t.Fatalf("limit=1 wrong: %+v err=%v", top, err)
	}

	// 无 run 的 owner → 空列表（非 nil 错误）。
	empty, err := repo.ListSessions(ctx, "nobody", 0)
	if err != nil || len(empty) != 0 {
		t.Fatalf("expected empty for unknown owner, got %+v err=%v", empty, err)
	}
}

func TestListRunsBySession(t *testing.T) {
	pool := testPool(t)
	ctx := context.Background()
	seedSessions(t, pool)
	repo := store.NewSessionRepository(pool)

	// created_at 升序返回该会话全部 run。
	runs, err := repo.ListRunsBySession(ctx, "o1", "sA")
	if err != nil {
		t.Fatalf("ListRunsBySession: %v", err)
	}
	if len(runs) != 2 || runs[0].RunID != "r1" || runs[1].RunID != "r2" {
		t.Fatalf("expected [r1 r2], got %+v", runs)
	}
	if runs[0].QueryText != "第一问" || runs[0].Status != store.StatusRunning {
		t.Fatalf("run fields wrong: %+v", runs[0])
	}

	// owner 不匹配 → 空（上层据此回 404，不泄露他人会话存在性）。
	if other, err := repo.ListRunsBySession(ctx, "o2", "sA"); err != nil || len(other) != 0 {
		t.Fatalf("owner isolation broken: %+v err=%v", other, err)
	}
	// 不存在的会话 → 空。
	if none, err := repo.ListRunsBySession(ctx, "o1", "ghost"); err != nil || len(none) != 0 {
		t.Fatalf("expected empty for unknown session, got %+v err=%v", none, err)
	}
}
