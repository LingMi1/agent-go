package store_test

import (
	"context"
	"os"
	"testing"

	"my-agent/control-plane/internal/store"
)

// UsageReport：owner 域聚合（合计/按天/按模式），他人数据不可见。
func TestUsageReport(t *testing.T) {
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
	mk := func(id, owner, agent string, in, out int64, calls int32) {
		if err := runs.CreateRun(ctx, store.CreateRunParams{
			RunID: id, SessionID: "s-" + id, OwnerID: owner, EntryAgent: agent, QueryText: "q",
		}); err != nil {
			t.Fatalf("create %s: %v", id, err)
		}
		if err := runs.FinishRun(ctx, store.FinishRunParams{
			RunID: id, Status: store.StatusSuccess, FinalSummaryText: "ok",
			InputTokens: in, OutputTokens: out, ModelCalls: calls,
		}); err != nil {
			t.Fatalf("finish %s: %v", id, err)
		}
	}
	mk("r1", "u1", "react", 100, 20, 2)
	mk("r2", "u1", "plan_solve", 300, 50, 5)
	mk("r3", "intruder", "react", 999, 999, 9) // 不可见

	stats := store.NewStatsRepository(pool)
	rep, err := stats.UsageReport(ctx, "u1", 7)
	if err != nil {
		t.Fatalf("UsageReport: %v", err)
	}
	if rep.Totals.Runs != 2 || rep.Totals.InputTokens != 400 || rep.Totals.OutputTokens != 70 || rep.Totals.ModelCalls != 7 {
		t.Fatalf("totals 不对: %+v", rep.Totals)
	}
	if len(rep.Daily) != 1 || rep.Daily[0].Runs != 2 || rep.Daily[0].InputTokens != 400 {
		t.Fatalf("daily 不对: %+v", rep.Daily)
	}
	if len(rep.ByAgent) != 2 || rep.ByAgent[0].AgentType != "plan_solve" { // 按 input 降序
		t.Fatalf("byAgent 不对: %+v", rep.ByAgent)
	}
	// days 越界回退默认
	rep2, _ := stats.UsageReport(ctx, "u1", -5)
	if rep2.Days != 30 {
		t.Fatalf("days 兜底失效: %d", rep2.Days)
	}
}
