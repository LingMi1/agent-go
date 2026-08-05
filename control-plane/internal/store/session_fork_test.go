package store_test

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"my-agent/control-plane/internal/store"
)

// seedForkChain 造一条分叉链（docs/14 §6）：
//
//	o1/sA: r1(t0) r2(t2) r3(t4)           —— 根会话三轮
//	o1/sB: fork(sA after r2) + own r4(t6) —— 从 sA 第 2 轮分叉，续了一轮
//	o1/sC: fork(sB after r4) + own r5(t8) —— 分叉的分叉（锚在 sB 的 own 轮）
//	o1/sD: fork(sB after r1)              —— 锚在 sB 的**继承**轮（属 sA），0 own run
//	o2/sE: r9(t1)                         —— 他人会话（隔离对照）
func seedForkChain(t *testing.T, pool *pgxpool.Pool) {
	t.Helper()
	ctx := context.Background()
	runs := store.NewRunRepository(pool)
	sessions := store.NewSessionRepository(pool)
	base := time.Date(2026, 7, 1, 10, 0, 0, 0, time.UTC)

	rows := []struct {
		run, session, owner, query string
		at                         time.Time
	}{
		{"r1", "sA", "o1", "根一问", base},
		{"r2", "sA", "o1", "根二问", base.Add(2 * time.Minute)},
		{"r3", "sA", "o1", "根三问", base.Add(4 * time.Minute)},
		{"r4", "sB", "o1", "B 续问", base.Add(6 * time.Minute)},
		{"r5", "sC", "o1", "C 续问", base.Add(8 * time.Minute)},
		{"r9", "sE", "o2", "他人问", base.Add(1 * time.Minute)},
	}
	for _, r := range rows {
		if err := runs.CreateRun(ctx, store.CreateRunParams{
			RunID: r.run, SessionID: r.session, OwnerID: r.owner, EntryAgent: "react", QueryText: r.query,
		}); err != nil {
			t.Fatalf("CreateRun %s: %v", r.run, err)
		}
		setCreatedAt(t, pool, r.run, r.at)
	}
	forks := []store.SessionFork{
		{SessionID: "sB", ParentSessionID: "sA", ForkAfterRunID: "r2", OwnerID: "o1"},
		{SessionID: "sC", ParentSessionID: "sB", ForkAfterRunID: "r4", OwnerID: "o1"},
		{SessionID: "sD", ParentSessionID: "sB", ForkAfterRunID: "r1", OwnerID: "o1"},
	}
	for _, f := range forks {
		if err := sessions.CreateFork(ctx, f); err != nil {
			t.Fatalf("CreateFork %s: %v", f.SessionID, err)
		}
	}
}

func runIDs(runs []store.Run) []string {
	out := make([]string, 0, len(runs))
	for _, r := range runs {
		out = append(out, r.RunID)
	}
	return out
}

func assertRunIDs(t *testing.T, got []store.Run, want ...string) {
	t.Helper()
	ids := runIDs(got)
	if len(ids) != len(want) {
		t.Fatalf("timeline 长度不对: got %v want %v", ids, want)
	}
	for i := range want {
		if ids[i] != want[i] {
			t.Fatalf("timeline 顺序不对: got %v want %v", ids, want)
		}
	}
}

func TestForkRegistryAndOwnerIsolation(t *testing.T) {
	pool := testPool(t)
	ctx := context.Background()
	seedForkChain(t, pool)
	repo := store.NewSessionRepository(pool)

	// 登记往返：原 run_id 原样、owner 归属、created_at 落库。
	f, err := repo.GetFork(ctx, "o1", "sB")
	if err != nil {
		t.Fatalf("GetFork: %v", err)
	}
	if f.ParentSessionID != "sA" || f.ForkAfterRunID != "r2" || f.OwnerID != "o1" || f.CreatedAt.IsZero() {
		t.Fatalf("fork row wrong: %+v", f)
	}
	// owner 隔离：他人查同一分叉 → ErrForkNotFound（不泄露存在性）。
	if _, err := repo.GetFork(ctx, "o2", "sB"); !errors.Is(err, store.ErrForkNotFound) {
		t.Fatalf("expected ErrForkNotFound for other owner, got %v", err)
	}
	// 非分叉会话 / 不存在的会话同样 not found。
	if _, err := repo.GetFork(ctx, "o1", "sA"); !errors.Is(err, store.ErrForkNotFound) {
		t.Fatalf("expected ErrForkNotFound for non-fork, got %v", err)
	}
}

func TestListSessionsIncludesZeroRunForks(t *testing.T) {
	pool := testPool(t)
	ctx := context.Background()
	seedForkChain(t, pool)
	repo := store.NewSessionRepository(pool)

	list, err := repo.ListSessions(ctx, "o1", 0)
	if err != nil {
		t.Fatalf("ListSessions: %v", err)
	}
	byID := map[string]store.SessionSummary{}
	for _, s := range list {
		byID[s.SessionID] = s
	}
	if len(byID) != 4 {
		t.Fatalf("expected 4 sessions (sA sB sC sD), got %v", byID)
	}

	// 非分叉会话：forkedFrom 为空，聚合语义与 M7 一致。
	sA := byID["sA"]
	if sA.ForkedFrom != "" || sA.RunCount != 3 || sA.Title != "根一问" {
		t.Fatalf("sA wrong: %+v", sA)
	}
	// 有 own run 的分叉会话：title 取 own 首问，runCount 只数 own，forkedFrom=父。
	sB := byID["sB"]
	if sB.ForkedFrom != "sA" || sB.RunCount != 1 || sB.Title != "B 续问" || sB.EntryAgent != "react" {
		t.Fatalf("sB wrong: %+v", sB)
	}
	// 0 own-run 的分叉会话也可见：title/entryAgent 回退父会话首 run，时间取 fork 创建时刻。
	sD := byID["sD"]
	if sD.ForkedFrom != "sB" || sD.RunCount != 0 || sD.Title != "B 续问" || sD.EntryAgent != "react" {
		t.Fatalf("sD wrong: %+v", sD)
	}
	if sD.CreatedAt.IsZero() || !sD.LastActiveAt.Equal(sD.CreatedAt) {
		t.Fatalf("sD times wrong: %+v", sD)
	}

	// owner 隔离：o2 只见 sE，且不见 o1 的分叉。
	l2, err := repo.ListSessions(ctx, "o2", 0)
	if err != nil || len(l2) != 1 || l2[0].SessionID != "sE" || l2[0].ForkedFrom != "" {
		t.Fatalf("o2 isolation broken: %+v err=%v", l2, err)
	}
}

func TestListRunsBySessionForkChain(t *testing.T) {
	pool := testPool(t)
	ctx := context.Background()
	seedForkChain(t, pool)
	repo := store.NewSessionRepository(pool)

	// 父→子：sB = [sA 截至 r2 的继承段, own r4]；继承轮标 inherited 且保留原 runId。
	runsB, err := repo.ListRunsBySession(ctx, "o1", "sB")
	if err != nil {
		t.Fatalf("ListRunsBySession sB: %v", err)
	}
	assertRunIDs(t, runsB, "r1", "r2", "r4")
	if !runsB[0].Inherited || !runsB[1].Inherited || runsB[2].Inherited {
		t.Fatalf("inherited flags wrong: %+v", runsB)
	}
	// 继承轮是原 run 的原字段（只读投影，零复制）。
	if runsB[0].SessionID != "sA" || runsB[0].QueryText != "根一问" {
		t.Fatalf("inherited run should keep original fields: %+v", runsB[0])
	}

	// 父→子→孙：sC = [r1(i), r2(i), r4(i), own r5]。
	runsC, err := repo.ListRunsBySession(ctx, "o1", "sC")
	if err != nil {
		t.Fatalf("ListRunsBySession sC: %v", err)
	}
	assertRunIDs(t, runsC, "r1", "r2", "r4", "r5")
	for i := 0; i < 3; i++ {
		if !runsC[i].Inherited {
			t.Fatalf("runsC[%d] should be inherited: %+v", i, runsC[i])
		}
	}
	if runsC[3].Inherited {
		t.Fatalf("own run should not be inherited: %+v", runsC[3])
	}

	// 锚在继承轮的分叉：sD fork 自 sB after r1（r1 属 sA）——sB 的 own 段贡献为空，
	// 截断递归对 sA 生效，timeline 只有 [r1(i)]。
	runsD, err := repo.ListRunsBySession(ctx, "o1", "sD")
	if err != nil {
		t.Fatalf("ListRunsBySession sD: %v", err)
	}
	assertRunIDs(t, runsD, "r1")
	if !runsD[0].Inherited {
		t.Fatalf("sD 唯一轮应为继承: %+v", runsD[0])
	}

	// owner 隔离：他人查分叉会话 → 空（上层 404）。
	if other, err := repo.ListRunsBySession(ctx, "o2", "sB"); err != nil || len(other) != 0 {
		t.Fatalf("owner isolation broken: %+v err=%v", other, err)
	}
}

func TestListRunsBySessionCycleGuard(t *testing.T) {
	pool := testPool(t)
	ctx := context.Background()
	runs := store.NewRunRepository(pool)
	repo := store.NewSessionRepository(pool)

	// 病态数据：sX ⇄ sY 互为父（正常业务造不出来，护栏必须兜住不死循环）。
	for _, r := range []struct{ run, session string }{{"x1", "sX"}, {"y1", "sY"}} {
		if err := runs.CreateRun(ctx, store.CreateRunParams{
			RunID: r.run, SessionID: r.session, OwnerID: "o1", QueryText: "q",
		}); err != nil {
			t.Fatalf("CreateRun: %v", err)
		}
	}
	if err := repo.CreateFork(ctx, store.SessionFork{SessionID: "sX", ParentSessionID: "sY", ForkAfterRunID: "y1", OwnerID: "o1"}); err != nil {
		t.Fatalf("CreateFork sX: %v", err)
	}
	if err := repo.CreateFork(ctx, store.SessionFork{SessionID: "sY", ParentSessionID: "sX", ForkAfterRunID: "x1", OwnerID: "o1"}); err != nil {
		t.Fatalf("CreateFork sY: %v", err)
	}

	done := make(chan struct{})
	var got []store.Run
	var err error
	go func() {
		got, err = repo.ListRunsBySession(ctx, "o1", "sX")
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(10 * time.Second):
		t.Fatal("cycle guard failed: ListRunsBySession 未终止")
	}
	if err != nil {
		t.Fatalf("ListRunsBySession: %v", err)
	}
	// 环在 visited 处斩断：y1 继承段 + x1 own。
	assertRunIDs(t, got, "y1", "x1")
}

func TestForkAfterParentDeleted(t *testing.T) {
	pool := testPool(t)
	ctx := context.Background()
	seedForkChain(t, pool)
	repo := store.NewSessionRepository(pool)

	// 删父会话 sA → 锚点 r2 消失，sB 的继承段自然为空，只剩 own runs（不报错）。
	if n, err := repo.DeleteSession(ctx, "o1", "sA"); err != nil || n == 0 {
		t.Fatalf("DeleteSession sA: n=%d err=%v", n, err)
	}
	runsB, err := repo.ListRunsBySession(ctx, "o1", "sB")
	if err != nil {
		t.Fatalf("ListRunsBySession sB after parent delete: %v", err)
	}
	assertRunIDs(t, runsB, "r4")
	if runsB[0].Inherited {
		t.Fatalf("own run mislabeled inherited: %+v", runsB[0])
	}
	// 孙会话 sC：sB 段仍在（锚 r4 未删），更远的 sA 段消失。
	runsC, err := repo.ListRunsBySession(ctx, "o1", "sC")
	if err != nil {
		t.Fatalf("ListRunsBySession sC: %v", err)
	}
	assertRunIDs(t, runsC, "r4", "r5")

	// 0 own-run 的分叉会话删除：删的是登记行（返回 1 → 上层 200），列表随之消失。
	if n, err := repo.DeleteSession(ctx, "o1", "sD"); err != nil || n != 1 {
		t.Fatalf("DeleteSession sD: n=%d err=%v", n, err)
	}
	list, err := repo.ListSessions(ctx, "o1", 0)
	if err != nil {
		t.Fatalf("ListSessions: %v", err)
	}
	for _, s := range list {
		if s.SessionID == "sD" || s.SessionID == "sA" {
			t.Fatalf("deleted session still listed: %+v", s)
		}
	}
	// 有 own run 的分叉会话删除后，fork 登记行也应清掉（否则复活成 0-run 幽灵条目）。
	if n, err := repo.DeleteSession(ctx, "o1", "sB"); err != nil || n != 2 { // r4 + fork 行
		t.Fatalf("DeleteSession sB: n=%d err=%v", n, err)
	}
	list, _ = repo.ListSessions(ctx, "o1", 0)
	for _, s := range list {
		if s.SessionID == "sB" {
			t.Fatalf("deleted fork session resurrected: %+v", s)
		}
	}
}
