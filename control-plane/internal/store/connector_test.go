package store_test

import (
	"context"
	"encoding/json"
	"os"
	"testing"
	"time"

	"my-agent/control-plane/internal/store"
)

// 连接器 CRUD + owner 隔离 + ListDue/Claim 认领语义 + UpdateCursor 推进游标。
func TestConnectorRepository(t *testing.T) {
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
	if _, err := pool.Exec(ctx, `TRUNCATE connectors, triggers`); err != nil {
		t.Fatalf("truncate: %v", err)
	}

	repo := store.NewConnectorRepository(pool)
	c := store.Connector{
		ConnectorID: "c1", OwnerID: "u1", Kind: "github",
		TokenCiphertext: []byte{1, 2, 3, 4}, PollIntervalS: 60, Enabled: true,
	}
	if err := repo.Create(ctx, c); err != nil {
		t.Fatalf("create: %v", err)
	}
	// 他人连接器（owner 隔离）。
	if err := repo.Create(ctx, store.Connector{ConnectorID: "c2", OwnerID: "u2", Kind: "github", TokenCiphertext: []byte{9}, PollIntervalS: 60, Enabled: true}); err != nil {
		t.Fatalf("create c2: %v", err)
	}

	list, err := repo.ListByOwner(ctx, "u1")
	if err != nil || len(list) != 1 || list[0].ConnectorID != "c1" {
		t.Fatalf("owner 隔离失败: %+v %v", list, err)
	}
	if string(list[0].TokenCiphertext) != string([]byte{1, 2, 3, 4}) {
		t.Fatalf("密文往返不对: %v", list[0].TokenCiphertext)
	}

	// ListDue：默认 next_poll_at=now() → 到期；Claim 后推进 interval，秒内不可再认领。
	due, err := repo.ListDue(ctx, 10)
	if err != nil || len(due) != 2 {
		t.Fatalf("due: %d %v", len(due), err)
	}
	ok, err := repo.Claim(ctx, "c1")
	if err != nil || !ok {
		t.Fatalf("claim: %v %v", ok, err)
	}
	if ok2, _ := repo.Claim(ctx, "c1"); ok2 {
		t.Fatal("推进后不应再次认领")
	}

	// UpdateCursor 推进游标 + last_poll_id。
	if err := repo.UpdateCursor(ctx, "c1", "2026-07-05T10:00:00Z", "n9"); err != nil {
		t.Fatalf("update cursor: %v", err)
	}
	got, _ := repo.ListByOwner(ctx, "u1")
	if got[0].Cursor != "2026-07-05T10:00:00Z" || got[0].LastPollID != "n9" || !got[0].NextPollAt.After(time.Now()) {
		t.Fatalf("游标/next_poll_at 不对: %+v", got[0])
	}

	// 禁用后即使到期也不可认领。
	if err := repo.SetEnabled(ctx, "u1", "c1", false); err != nil {
		t.Fatalf("disable: %v", err)
	}
	if _, err := pool.Exec(ctx, `UPDATE connectors SET next_poll_at = now() - interval '1 minute' WHERE connector_id='c1'`); err != nil {
		t.Fatal(err)
	}
	if ok3, _ := repo.Claim(ctx, "c1"); ok3 {
		t.Fatal("禁用后不应认领")
	}
	// owner 隔离删除。
	if err := repo.Delete(ctx, "intruder", "c1"); err == nil {
		t.Fatal("他人删除应报错")
	}
	if err := repo.Delete(ctx, "u1", "c1"); err != nil {
		t.Fatalf("本人删除: %v", err)
	}
}

// #11：删除连接器应在同一事务内级联删除其触发规则（triggers 无外键，靠应用层级联）。
// 否则孤儿触发规则悬空——ListByOwner 仍返回且 enabled，UI 误显示为「已启用」却永不触发，
// 且仍占 maxTriggersPerOwner 配额。同时验证 owner 隔离：删他人连接器失败时不得误删其 triggers。
func TestDeleteConnectorCascadesTriggers(t *testing.T) {
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
	if _, err := pool.Exec(ctx, `TRUNCATE connectors, triggers`); err != nil {
		t.Fatalf("truncate: %v", err)
	}

	conns := store.NewConnectorRepository(pool)
	trigs := store.NewTriggerRepository(pool)

	// u1 有连接器 c1（挂 t1/t2）与 c2（挂 t3，作对照——不应被 c1 的删除波及）。
	for _, c := range []store.Connector{
		{ConnectorID: "c1", OwnerID: "u1", Kind: "github", TokenCiphertext: []byte{1}, PollIntervalS: 60, Enabled: true},
		{ConnectorID: "c2", OwnerID: "u1", Kind: "github", TokenCiphertext: []byte{2}, PollIntervalS: 60, Enabled: true},
	} {
		if err := conns.Create(ctx, c); err != nil {
			t.Fatalf("create connector %s: %v", c.ConnectorID, err)
		}
	}
	for _, tg := range []store.Trigger{
		{TriggerID: "t1", OwnerID: "u1", ConnectorID: "c1", EventType: "issue", QueryTemplate: "a", AgentType: "react", Enabled: true},
		{TriggerID: "t2", OwnerID: "u1", ConnectorID: "c1", EventType: "pull_request", QueryTemplate: "b", AgentType: "react", Enabled: true},
		{TriggerID: "t3", OwnerID: "u1", ConnectorID: "c2", EventType: "issue", QueryTemplate: "c", AgentType: "react", Enabled: true},
	} {
		if err := trigs.Create(ctx, tg); err != nil {
			t.Fatalf("create trigger %s: %v", tg.TriggerID, err)
		}
	}

	// 删他人连接器失败 → 不得误删任何 triggers（回滚）。
	if err := conns.Delete(ctx, "intruder", "c1"); err == nil {
		t.Fatal("他人删除应报错")
	}
	if got, _ := trigs.ListByConnector(ctx, "c1"); len(got) != 2 {
		t.Fatalf("失败的删除不应动 triggers，c1 触发规则应仍为 2，得 %d", len(got))
	}

	// 本人删除 c1 → c1 的 t1/t2 级联删除；c2 的 t3 保留。
	if err := conns.Delete(ctx, "u1", "c1"); err != nil {
		t.Fatalf("本人删除 c1: %v", err)
	}
	if got, _ := trigs.ListByConnector(ctx, "c1"); len(got) != 0 {
		t.Fatalf("删连接器后其触发规则应级联清空，c1 仍剩 %d 条（悬空）", len(got))
	}
	if got, _ := trigs.ListByConnector(ctx, "c2"); len(got) != 1 {
		t.Fatalf("不应波及其它连接器的触发规则，c2 应仍为 1，得 %d", len(got))
	}
	// ListByOwner 也不再返回孤儿规则（不占配额、UI 不再误显示启用）。
	byOwner, err := trigs.ListByOwner(ctx, "u1")
	if err != nil {
		t.Fatalf("ListByOwner: %v", err)
	}
	if len(byOwner) != 1 || byOwner[0].TriggerID != "t3" {
		t.Fatalf("owner 剩余触发规则应仅 t3，得 %+v", byOwner)
	}
}

// 触发规则 CRUD + owner 隔离 + ListByConnector + filter JSONB 往返。
func TestTriggerRepository(t *testing.T) {
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
	if _, err := pool.Exec(ctx, `TRUNCATE triggers`); err != nil {
		t.Fatalf("truncate: %v", err)
	}

	repo := store.NewTriggerRepository(pool)
	filter := json.RawMessage(`{"repo": "o/r"}`)
	t1 := store.Trigger{
		TriggerID: "t1", OwnerID: "u1", ConnectorID: "c1", EventType: "issue",
		Filter: filter, QueryTemplate: "回复 {{title}}", AgentType: "react", NeedsApproval: true, Enabled: true,
	}
	if err := repo.Create(ctx, t1); err != nil {
		t.Fatalf("create: %v", err)
	}
	// 无 filter 的规则（NULL JSONB）。
	if err := repo.Create(ctx, store.Trigger{TriggerID: "t2", OwnerID: "u1", ConnectorID: "c1", EventType: "pull_request", QueryTemplate: "看 {{url}}", AgentType: "react", Enabled: true}); err != nil {
		t.Fatalf("create t2: %v", err)
	}
	// 他人规则。
	if err := repo.Create(ctx, store.Trigger{TriggerID: "t3", OwnerID: "u2", ConnectorID: "c9", EventType: "issue", QueryTemplate: "x", AgentType: "react", Enabled: true}); err != nil {
		t.Fatalf("create t3: %v", err)
	}

	byOwner, err := repo.ListByOwner(ctx, "u1")
	if err != nil || len(byOwner) != 2 {
		t.Fatalf("owner 隔离: %d %v", len(byOwner), err)
	}
	// filter JSONB 往返（语义相等）。
	var found *store.Trigger
	for i := range byOwner {
		if byOwner[i].TriggerID == "t1" {
			found = &byOwner[i]
		}
	}
	if found == nil || !found.NeedsApproval {
		t.Fatalf("t1 未找到或 needs_approval 丢失: %+v", found)
	}
	if !jsonSemEqual(t, found.Filter, filter) {
		t.Fatalf("filter 往返不对: %s", found.Filter)
	}

	// ListByConnector：c1 有 t1/t2；c9 有 t3。
	byConn, err := repo.ListByConnector(ctx, "c1")
	if err != nil || len(byConn) != 2 {
		t.Fatalf("ListByConnector c1: %d %v", len(byConn), err)
	}

	// SetEnabled owner 隔离 + Delete owner 隔离。
	if err := repo.SetEnabled(ctx, "intruder", "t1", false); err == nil {
		t.Fatal("他人 toggle 应报错")
	}
	if err := repo.SetEnabled(ctx, "u1", "t1", false); err != nil {
		t.Fatalf("本人 toggle: %v", err)
	}
	if err := repo.Delete(ctx, "intruder", "t1"); err == nil {
		t.Fatal("他人删除应报错")
	}
	if err := repo.Delete(ctx, "u1", "t1"); err != nil {
		t.Fatalf("本人删除: %v", err)
	}
}
