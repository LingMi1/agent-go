package store_test

import (
	"context"
	"os"
	"testing"
	"time"

	"my-agent/control-plane/internal/store"
)

// 认领语义：到期才可认领且原子推进 next_run_at（先 Admit 后 Claim 的存储侧保证）。
func TestScheduleClaimSemantics(t *testing.T) {
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
	if _, err := pool.Exec(ctx, `TRUNCATE schedules`); err != nil {
		t.Fatalf("truncate: %v", err)
	}

	repo := store.NewSchedulesRepository(pool)
	s := store.Schedule{
		ScheduleID: "sc1", OwnerID: "u1", SessionID: "sched-x", QueryText: "巡检",
		AgentType: "react", IntervalSeconds: 3600, Enabled: true,
	}
	if err := repo.Create(ctx, s); err != nil {
		t.Fatalf("create: %v", err)
	}

	// 默认 next_run_at=now() → 到期可认领；认领后推进 interval，秒内不可再认领。
	due, err := repo.ListDue(ctx, 10)
	if err != nil || len(due) != 1 {
		t.Fatalf("due: %v %v", due, err)
	}
	ok, err := repo.Claim(ctx, "sc1", "run-1")
	if err != nil || !ok {
		t.Fatalf("claim: %v %v", ok, err)
	}
	ok2, _ := repo.Claim(ctx, "sc1", "run-2")
	if ok2 {
		t.Fatal("推进后不应再次认领")
	}
	list, _ := repo.ListByOwner(ctx, "u1")
	if len(list) != 1 || list[0].LastRunID != "run-1" || !list[0].NextRunAt.After(time.Now()) {
		t.Fatalf("认领后状态不对: %+v", list)
	}

	// 禁用后即使到期也不可认领；owner 隔离删除。
	if err := repo.SetEnabled(ctx, "u1", "sc1", false); err != nil {
		t.Fatalf("disable: %v", err)
	}
	if _, err := pool.Exec(ctx, `UPDATE schedules SET next_run_at = now() - interval '1 minute'`); err != nil {
		t.Fatal(err)
	}
	if ok3, _ := repo.Claim(ctx, "sc1", "run-3"); ok3 {
		t.Fatal("禁用后不应认领")
	}
	if err := repo.Delete(ctx, "intruder", "sc1"); err == nil {
		t.Fatal("他人删除应报错")
	}
	if err := repo.Delete(ctx, "u1", "sc1"); err != nil {
		t.Fatalf("本人删除: %v", err)
	}
}
