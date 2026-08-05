package store_test

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"my-agent/control-plane/internal/store"
)

// mkRun 建一个 run 并把 created_at 精确改成给定时刻（微秒精度，供 keyset 页边界断言）。
func mkRun(t *testing.T, pool *pgxpool.Pool, runID, owner string, createdAt time.Time) {
	t.Helper()
	ctx := context.Background()
	runs := store.NewRunRepository(pool)
	if err := runs.CreateRun(ctx, store.CreateRunParams{
		RunID: runID, SessionID: "s-" + runID, OwnerID: owner, QueryText: "q", EntryAgent: "react",
	}); err != nil {
		t.Fatalf("CreateRun %s: %v", runID, err)
	}
	if _, err := pool.Exec(ctx, `UPDATE runs SET created_at = $1 WHERE run_id = $2`, createdAt, runID); err != nil {
		t.Fatalf("set created_at %s: %v", runID, err)
	}
}

func pagerOf(t *testing.T, pool *pgxpool.Pool) store.AllRunsPager {
	t.Helper()
	p, ok := store.NewRunRepository(pool).(store.AllRunsPager)
	if !ok {
		t.Fatal("pgRunRepo 应实现 AllRunsPager")
	}
	return p
}

// #10：同一 created_at 的多 run 在 keyset 页边界不得静默丢失。
// 旧实现 `WHERE created_at < before` 缺 run_id 平手项，第 2 页会把同时刻但 run_id 更小者整段排除。
// 复合游标 (created_at, run_id) < ($1, $2) 修复：第 2 页应精确接续、一条不丢。
func TestListAllRunsPagedTieBreaker(t *testing.T) {
	pool := testPool(t)
	ctx := context.Background()
	ts := time.Date(2026, 7, 5, 10, 0, 0, 0, time.UTC)
	// 三个 run 同一时刻；ORDER BY created_at DESC, run_id DESC → run-c, run-b, run-a。
	mkRun(t, pool, "run-a", "u1", ts)
	mkRun(t, pool, "run-b", "u2", ts)
	mkRun(t, pool, "run-c", "u1", ts)

	pager := pagerOf(t, pool)

	// 第 1 页（limit=2）：run-c, run-b。
	page1, err := pager.ListAllRunsPaged(ctx, 2, time.Time{}, "")
	if err != nil {
		t.Fatalf("page1: %v", err)
	}
	if got := runIDs(page1); len(got) != 2 || got[0] != "run-c" || got[1] != "run-b" {
		t.Fatalf("第 1 页应为 [run-c run-b]，得 %v", got)
	}

	// 第 2 页游标 = 上一页末项（run-b）的全精度 created_at + run_id。
	last := page1[len(page1)-1]
	page2, err := pager.ListAllRunsPaged(ctx, 2, last.CreatedAt, last.RunID)
	if err != nil {
		t.Fatalf("page2: %v", err)
	}
	if got := runIDs(page2); len(got) != 1 || got[0] != "run-a" {
		t.Fatalf("第 2 页应精确接续为 [run-a]（同时刻的 run-a 不丢），得 %v", got)
	}

	// 对照：旧 3 参 ListAllRuns（无 run_id tie-breaker）在同一时刻游标下会丢掉 run-a。
	lister := store.NewRunRepository(pool).(store.AllRunsLister)
	old, err := lister.ListAllRuns(ctx, 2, last.CreatedAt)
	if err != nil {
		t.Fatalf("old lister: %v", err)
	}
	if len(old) != 0 {
		t.Fatalf("对照：旧 created_at<before 在同时刻应丢掉 run-a（返回空），得 %v", runIDs(old))
	}
}

// 会话轮附件持久化：CreateRun 带 attachments JSON → SessionRuns（ListRunsBySession）取回
// 逐字节一致；nil 附件（headless/无附件轮）取回应为 nil。
// jsonb 会把文本规范化（对象键短→长、同长按字节序重排；`": "`/`", "` 定格间隔），
// 故输入直接按该规范形书写，字节等价断言才成立；任意键序的语义等价由 jsonSemEqual 双保险。
func TestRunAttachmentsRoundtrip(t *testing.T) {
	pool := testPool(t)
	ctx := context.Background()
	runs := store.NewRunRepository(pool)
	sessions := store.NewSessionRepository(pool)

	// 键序即 jsonb 输出序：size(4) < fileName(8) < mimeType(8) < previewUrl(10) < downloadUrl(11) < resourceKey(11)。
	att := []byte(`[{"size": 7, "fileName": "cat.png", "mimeType": "image/png", "previewUrl": "/artifacts/uploads/o1/s-att/ab/cat.png", "downloadUrl": "/artifacts/uploads/o1/s-att/ab/cat.png", "resourceKey": "uploads/o1/s-att/ab/cat.png"}]`)
	if err := runs.CreateRun(ctx, store.CreateRunParams{
		RunID: "run-att-1", SessionID: "s-att", OwnerID: "o1", QueryText: "带附件的一轮", Attachments: att,
	}); err != nil {
		t.Fatalf("CreateRun with attachments: %v", err)
	}
	if err := runs.CreateRun(ctx, store.CreateRunParams{
		RunID: "run-att-2", SessionID: "s-att", OwnerID: "o1", QueryText: "无附件的一轮",
	}); err != nil {
		t.Fatalf("CreateRun nil attachments: %v", err)
	}

	got, err := sessions.ListRunsBySession(ctx, "o1", "s-att")
	if err != nil {
		t.Fatalf("ListRunsBySession: %v", err)
	}
	if ids := runIDs(got); len(got) != 2 || ids[0] != "run-att-1" || ids[1] != "run-att-2" {
		t.Fatalf("timeline 应为 [run-att-1 run-att-2]，得 %v", ids)
	}
	if !jsonSemEqual(t, got[0].Attachments, att) {
		t.Errorf("attachments 语义漂移\n got: %s\nwant: %s", got[0].Attachments, att)
	}
	if string(got[0].Attachments) != string(att) {
		t.Errorf("attachments 非逐字节一致\n got: %s\nwant: %s", got[0].Attachments, att)
	}
	if got[1].Attachments != nil {
		t.Errorf("无附件轮应取回 nil attachments，得 %s", got[1].Attachments)
	}

	// GetRun 与 SessionRuns 共用 runColumns/scanRun：单查同样带回 attachments。
	one, err := runs.GetRun(ctx, "run-att-1")
	if err != nil {
		t.Fatalf("GetRun: %v", err)
	}
	if !jsonSemEqual(t, one.Attachments, att) {
		t.Errorf("GetRun attachments 漂移: %s", one.Attachments)
	}
}

// #10：微秒精度 created_at 在同一毫秒内的多 run，用全精度复合游标不丢
// （admin.go 的毫秒截断是 API 层的另一半，本层保证收到全精度 before 时正确接续）。
func TestListAllRunsPagedSubMillisecond(t *testing.T) {
	pool := testPool(t)
	ctx := context.Background()
	base := time.Date(2026, 7, 5, 10, 0, 0, 0, time.UTC)
	// 同一毫秒内两条：hi=+600µs（较新）、lo=+100µs（较旧）。
	mkRun(t, pool, "run-lo", "u1", base.Add(100*time.Microsecond))
	mkRun(t, pool, "run-hi", "u1", base.Add(600*time.Microsecond))

	pager := pagerOf(t, pool)

	page1, err := pager.ListAllRunsPaged(ctx, 1, time.Time{}, "")
	if err != nil || len(page1) != 1 || page1[0].RunID != "run-hi" {
		t.Fatalf("第 1 页应为 [run-hi]，得 %v err=%v", runIDs(page1), err)
	}
	// 全精度游标续拉：同一毫秒内更旧的 run-lo 必须出现（不被亚毫秒截断吞掉）。
	last := page1[0]
	page2, err := pager.ListAllRunsPaged(ctx, 10, last.CreatedAt, last.RunID)
	if err != nil {
		t.Fatalf("page2: %v", err)
	}
	if got := runIDs(page2); len(got) != 1 || got[0] != "run-lo" {
		t.Fatalf("同毫秒内更旧的 run-lo 不应被丢，第 2 页应为 [run-lo]，得 %v", got)
	}
}

// 会话历史附件聚合（SessionAttachmentsReader）：跨轮合并、resourceKey 去重、排除当前轮、owner 隔离。
func TestSessionAttachmentsJSONMergesAndDedupes(t *testing.T) {
	pool := testPool(t)
	ctx := context.Background()
	runs := store.NewRunRepository(pool)

	sid := "s-hist-att"
	mk := func(runID, attJSON string) {
		var att []byte
		if attJSON != "" {
			att = []byte(attJSON)
		}
		if err := runs.CreateRun(ctx, store.CreateRunParams{
			RunID: runID, SessionID: sid, OwnerID: "o1", QueryText: "q", Attachments: att,
		}); err != nil {
			t.Fatalf("CreateRun(%s): %v", runID, err)
		}
	}
	mk("hist-r1", `[{"resourceKey": "uploads/o1/s/a.png", "fileName": "a.png"}]`)
	mk("hist-r2", `[{"resourceKey": "uploads/o1/s/a.png", "fileName": "a.png"}, {"resourceKey": "uploads/o1/s/b.jpeg", "fileName": "b.jpeg"}]`)
	mk("hist-r3", `[{"resourceKey": "uploads/o1/s/c.png", "fileName": "c.png"}]`) // 当前轮：应被排除

	reader, ok := runs.(store.SessionAttachmentsReader)
	if !ok {
		t.Fatal("RunRepository 未实现 SessionAttachmentsReader")
	}
	b, err := reader.SessionAttachmentsJSON(ctx, sid, "o1", "hist-r3")
	if err != nil {
		t.Fatalf("SessionAttachmentsJSON: %v", err)
	}
	var items []map[string]any
	if err := json.Unmarshal(b, &items); err != nil {
		t.Fatalf("unmarshal: %v (%s)", err, b)
	}
	names := make([]string, 0, len(items))
	for _, it := range items {
		names = append(names, it["fileName"].(string))
	}
	if len(names) != 2 || names[0] != "a.png" || names[1] != "b.jpeg" {
		t.Fatalf("合并去重结果错误: %v", names)
	}
	// owner 隔离：换 owner 查询 → 无历史（nil）
	if b2, _ := reader.SessionAttachmentsJSON(ctx, sid, "o2", "x"); b2 != nil {
		t.Fatalf("owner 隔离失效: %s", b2)
	}
}
