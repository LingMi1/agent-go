package store_test

import (
	"context"
	"errors"
	"os"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"my-agent/control-plane/internal/store"
)

// userTestPool 建池并清空 D3 相关表（users/auth_sessions）+ runs（run 计数/跨 owner 读用）。
func userTestPool(t *testing.T) *pgxpool.Pool {
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
	if _, err := pool.Exec(ctx, `TRUNCATE users, auth_sessions, events, runs CASCADE`); err != nil {
		t.Fatalf("truncate: %v", err)
	}
	t.Cleanup(pool.Close)
	return pool
}

// 注册/唯一约束/角色变更 + ListUsers 的 run 计数。
func TestUserStore(t *testing.T) {
	pool := userTestPool(t)
	ctx := context.Background()
	users := store.NewUserRepository(pool)

	if err := users.CreateUser(ctx, store.User{UserID: "alice", Username: "alice", PasswordHash: "h1"}); err != nil {
		t.Fatalf("CreateUser: %v", err)
	}
	// 默认角色 user。
	got, err := users.GetUserByName(ctx, "alice")
	if err != nil || got.Role != store.RoleUser || got.PasswordHash != "h1" {
		t.Fatalf("GetUserByName alice: %+v err=%v", got, err)
	}
	// 唯一约束：同名再建 → ErrUserExists。
	if err := users.CreateUser(ctx, store.User{UserID: "alice", Username: "alice", PasswordHash: "h2"}); !errors.Is(err, store.ErrUserExists) {
		t.Fatalf("重复用户名应 ErrUserExists, got %v", err)
	}
	// 不存在。
	if _, err := users.GetUserByName(ctx, "nobody"); !errors.Is(err, store.ErrUserNotFound) {
		t.Fatalf("未知用户应 ErrUserNotFound, got %v", err)
	}
	// 改角色。
	if err := users.SetRole(ctx, "alice", store.RoleAdmin); err != nil {
		t.Fatalf("SetRole: %v", err)
	}
	if got, _ := users.GetUserByName(ctx, "alice"); got.Role != store.RoleAdmin {
		t.Fatalf("角色应升为 admin, got %s", got.Role)
	}
	if err := users.SetRole(ctx, "ghost", store.RoleAdmin); !errors.Is(err, store.ErrUserNotFound) {
		t.Fatalf("改不存在用户应 ErrUserNotFound, got %v", err)
	}

	// ListUsers 的 run 计数：给 alice 造 2 条 run、bob 造 1 条。
	_ = users.CreateUser(ctx, store.User{UserID: "bob", Username: "bob", PasswordHash: "h3"})
	runs := store.NewRunRepository(pool)
	_ = runs.CreateRun(ctx, store.CreateRunParams{RunID: "r-a1", SessionID: "s", OwnerID: "alice", QueryText: "q"})
	_ = runs.CreateRun(ctx, store.CreateRunParams{RunID: "r-a2", SessionID: "s", OwnerID: "alice", QueryText: "q"})
	_ = runs.CreateRun(ctx, store.CreateRunParams{RunID: "r-b1", SessionID: "s", OwnerID: "bob", QueryText: "q"})
	list, err := users.ListUsers(ctx)
	if err != nil || len(list) != 2 {
		t.Fatalf("ListUsers: n=%d err=%v", len(list), err)
	}
	counts := map[string]int64{}
	for _, u := range list {
		counts[u.UserID] = u.RunCount
	}
	if counts["alice"] != 2 || counts["bob"] != 1 {
		t.Fatalf("run 计数不对: %+v", counts)
	}
}

// token 落库/JOIN 取角色/过期不返回/删除。
func TestSessionTokenStore(t *testing.T) {
	pool := userTestPool(t)
	ctx := context.Background()
	users := store.NewUserRepository(pool)
	tokens := store.NewSessionTokenRepository(pool)

	if err := users.CreateUser(ctx, store.User{UserID: "carol", Username: "carol", PasswordHash: "h", Role: store.RoleAdmin}); err != nil {
		t.Fatalf("CreateUser: %v", err)
	}
	// 有效 token → JOIN 出角色。
	if err := tokens.CreateToken(ctx, "tok-good", "carol", time.Now().Add(time.Hour)); err != nil {
		t.Fatalf("CreateToken: %v", err)
	}
	uid, role, err := tokens.LookupToken(ctx, "tok-good")
	if err != nil || uid != "carol" || role != store.RoleAdmin {
		t.Fatalf("LookupToken: uid=%s role=%s err=%v", uid, role, err)
	}
	// 过期 token → ErrTokenNotFound。
	if err := tokens.CreateToken(ctx, "tok-expired", "carol", time.Now().Add(-time.Minute)); err != nil {
		t.Fatalf("CreateToken expired: %v", err)
	}
	if _, _, err := tokens.LookupToken(ctx, "tok-expired"); !errors.Is(err, store.ErrTokenNotFound) {
		t.Fatalf("过期 token 应 ErrTokenNotFound, got %v", err)
	}
	// 未知 token。
	if _, _, err := tokens.LookupToken(ctx, "tok-unknown"); !errors.Is(err, store.ErrTokenNotFound) {
		t.Fatalf("未知 token 应 ErrTokenNotFound, got %v", err)
	}
	// 删除后失效。
	if err := tokens.DeleteToken(ctx, "tok-good"); err != nil {
		t.Fatalf("DeleteToken: %v", err)
	}
	if _, _, err := tokens.LookupToken(ctx, "tok-good"); !errors.Is(err, store.ErrTokenNotFound) {
		t.Fatalf("删除后应 ErrTokenNotFound, got %v", err)
	}
}

// admin 跨 owner 读：ListAllRuns 见到全部 owner；AdminUsageReport 跨 owner 聚合。
// 对照既有 owner 域方法（ListRunsBySession/UsageReport）仍只见自己——隔离不被弱化。
func TestAdminCrossOwnerReads(t *testing.T) {
	pool := userTestPool(t)
	ctx := context.Background()
	runs := store.NewRunRepository(pool)
	stats := store.NewStatsRepository(pool)

	_ = runs.CreateRun(ctx, store.CreateRunParams{RunID: "r1", SessionID: "s1", OwnerID: "u1", QueryText: "q", EntryAgent: "react"})
	_ = runs.CreateRun(ctx, store.CreateRunParams{RunID: "r2", SessionID: "s2", OwnerID: "u2", QueryText: "q", EntryAgent: "react"})
	_ = runs.FinishRun(ctx, store.FinishRunParams{RunID: "r1", Status: store.StatusSuccess, InputTokens: 10, OutputTokens: 5, ModelCalls: 1})
	_ = runs.FinishRun(ctx, store.FinishRunParams{RunID: "r2", Status: store.StatusSuccess, InputTokens: 20, OutputTokens: 7, ModelCalls: 2})

	// ListAllRuns（跨 owner）见到两条。
	lister, ok := runs.(store.AllRunsLister)
	if !ok {
		t.Fatal("pgRunRepo 应实现 AllRunsLister")
	}
	all, err := lister.ListAllRuns(ctx, 100, time.Time{})
	if err != nil || len(all) != 2 {
		t.Fatalf("ListAllRuns: n=%d err=%v", len(all), err)
	}
	owners := map[string]bool{}
	for _, r := range all {
		owners[r.OwnerID] = true
	}
	if !owners["u1"] || !owners["u2"] {
		t.Fatalf("ListAllRuns 应跨 owner: %+v", owners)
	}

	// AdminUsageReport（跨 owner）合计 = 两 owner 之和。
	reporter, ok := stats.(store.AdminStatsReporter)
	if !ok {
		t.Fatal("pgStatsRepo 应实现 AdminStatsReporter")
	}
	rep, err := reporter.AdminUsageReport(ctx, 30)
	if err != nil {
		t.Fatalf("AdminUsageReport: %v", err)
	}
	if rep.Totals.Runs != 2 || rep.Totals.InputTokens != 30 || rep.Totals.OutputTokens != 12 || rep.Totals.ModelCalls != 3 {
		t.Fatalf("跨 owner 合计不对: %+v", rep.Totals)
	}

	// 对照：owner 域 UsageReport 仍只见自己（隔离不被弱化）。
	own, err := stats.UsageReport(ctx, "u1", 30)
	if err != nil || own.Totals.Runs != 1 || own.Totals.InputTokens != 10 {
		t.Fatalf("owner 域仍应只见自己: %+v err=%v", own.Totals, err)
	}
}
