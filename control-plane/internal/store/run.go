package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// run 状态常量（与迁移里的 CHECK 一致）。
const (
	StatusRunning = "RUNNING"
	StatusSuccess = "SUCCESS"
	StatusFailed  = "FAILED"
	StatusStopped = "STOPPED"
	StatusTimeout = "TIMEOUT"
)

// ErrRunNotFound 表示按 runID 找不到 run。
var ErrRunNotFound = errors.New("store: run not found")

// Run 是 runs 表的一行。
type Run struct {
	RunID            string
	SessionID        string
	OwnerID          string
	EntryAgent       string
	QueryText        string
	Status           string
	FinalSummaryText *string
	ErrorMsg         *string
	// Attachments 是本轮请求附带的附件引用数组（AttachmentRef JSON，原样落库原样返还，
	// 供 GET /sessions/{id}/runs 回放还原用户气泡附件 chips 与工作区「上传内容」段）。
	// nil=该轮无附件（列为 NULL）。
	Attachments []byte
	CreatedAt   time.Time
	FinishedAt  *time.Time
	// Inherited 不是 runs 表的列：ListRunsBySession 沿 fork 链上溯拼装 timeline 时，
	// 祖先会话的继承段标 true（原 run_id 原事件，只读投影，回放端点零改动；docs/14 §4.2）。
	Inherited bool
}

// CreateRunParams 创建 run（状态默认 RUNNING）。
type CreateRunParams struct {
	RunID      string
	SessionID  string
	OwnerID    string
	EntryAgent string
	QueryText  string
	// Attachments 是请求附带的附件引用数组的原始 JSON（api 层原样 marshal）；
	// nil/空=无附件，写 NULL。headless 路径（scheduler/poller）恒传 nil。
	Attachments []byte
}

// FinishRunParams 收口 run。FinalSummaryText/ErrorMsg 为空串时写入 NULL。
type FinishRunParams struct {
	RunID            string
	Status           string
	FinalSummaryText string
	ErrorMsg         string
	// M11：终态 RESULT 附带的全 run token 用量（零值=未知/无模型调用）。
	InputTokens  int64
	OutputTokens int64
	ModelCalls   int32
}

// RunRepository 是 run 生命周期的写读端口。
type RunRepository interface {
	CreateRun(ctx context.Context, p CreateRunParams) error
	FinishRun(ctx context.Context, p FinishRunParams) error
	GetRun(ctx context.Context, runID string) (Run, error)
}

// AllRunsLister 是 admin 后台的**跨 owner** 只读端口（docs/17 §3.3）。
// 刻意与 RunRepository 分开：既有 RunRepository 的 fake（api 测试）零改，且普通用户端点
// 绝不经此路径——admin 跨 owner 读用新方法，既有 WHERE owner_id 过滤一处不弱化。
// pgRunRepo 同时实现它；api 层对已装配的 RunRepository 做类型断言取用（fake/nil 自然降级 503）。
// SessionAttachmentsReader 是"会话历史附件聚合"的只读端口：dispatch 在起新 run 时把
// 本会话此前各轮的 runs.attachments 合并去重，经认知面 metadata 下发为附件白名单——
// 续轮（如「把 HTML 改成滑块对比」）无需用户重传原图。与 AllRunsLister 同款独立接口：
// 既有 RunRepository fake 零改，dispatch 侧类型断言取用（fake/nil 自然降级为"无历史"）。
type SessionAttachmentsReader interface {
	// SessionAttachmentsJSON 返回该会话（owner 隔离）excludeRunID 之外各轮附件合并去重后的
	// JSON 数组（按 resourceKey 去重、时间升序首见优先）；无附件返回 nil。
	SessionAttachmentsJSON(ctx context.Context, sessionID, ownerID, excludeRunID string) ([]byte, error)
}

type AllRunsLister interface {
	ListAllRuns(ctx context.Context, limit int, before time.Time) ([]Run, error)
}

// AllRunsPager 是带**复合游标**（created_at + run_id tie-breaker，全精度 before）的
// admin 只读端口（#10）。旧 AllRunsLister 的 `WHERE created_at < before` 缺 run_id 平手项，
// 且调用方（api/admin.go）把 before 以 unix 毫秒还原（time.UnixMilli），毫秒截断微秒精度的
// created_at——两者叠加会在页边界静默丢 run（落在同一毫秒/同一时刻但 run_id 更小者被整段排除）。
// 本端口的游标携带上一页末项的完整 created_at + run_id，查询用 (created_at, run_id) < ($1, $2)
// 复合比较（对齐 artifact_list.go / ListRunsBySession 的 (ts,id) 范式），杜绝丢/重。
// pgRunRepo 同时实现它；api/admin.go 应迁移到本端口并停止毫秒截断（传 RFC3339Nano/微秒 before
// + beforeKey=上一页末项 run_id）以端到端闭合 #10——该迁移在 api 包（本次改动范围外）。
type AllRunsPager interface {
	ListAllRunsPaged(ctx context.Context, limit int, before time.Time, beforeKey string) ([]Run, error)
}

type pgRunRepo struct{ pool *pgxpool.Pool }

// NewRunRepository 返回基于 pgx 的 RunRepository。
func NewRunRepository(pool *pgxpool.Pool) RunRepository { return &pgRunRepo{pool: pool} }

func (r *pgRunRepo) CreateRun(ctx context.Context, p CreateRunParams) error {
	entryAgent := p.EntryAgent
	if entryAgent == "" {
		entryAgent = "react"
	}
	// attachments NULL 安全：pgx 对 jsonb 参数把 nil []byte 编码为 SQL NULL；
	// 空切片统一归一成 nil，杜绝空串写进 jsonb 报错。
	atts := p.Attachments
	if len(atts) == 0 {
		atts = nil
	}
	_, err := r.pool.Exec(ctx, `
		INSERT INTO runs (run_id, session_id, owner_id, entry_agent, query_text, status, attachments)
		VALUES ($1, $2, $3, $4, $5, 'RUNNING', $6)`,
		p.RunID, p.SessionID, p.OwnerID, entryAgent, p.QueryText, atts)
	if err != nil {
		return fmt.Errorf("store: create run: %w", err)
	}
	return nil
}

func (r *pgRunRepo) FinishRun(ctx context.Context, p FinishRunParams) error {
	tag, err := r.pool.Exec(ctx, `
		UPDATE runs
		   SET status = $2,
		       final_summary_text = NULLIF($3, ''),
		       error_msg = NULLIF($4, ''),
		       input_tokens = $5,
		       output_tokens = $6,
		       model_calls = $7,
		       finished_at = now()
		 WHERE run_id = $1`,
		p.RunID, p.Status, p.FinalSummaryText, p.ErrorMsg, p.InputTokens, p.OutputTokens, p.ModelCalls)
	if err != nil {
		return fmt.Errorf("store: finish run: %w", err)
	}
	if tag.RowsAffected() == 0 {
		return ErrRunNotFound
	}
	return nil
}

// runColumns 是 Run 结构的 SELECT 列清单，与 scanRun 的字段顺序一一对应——
// 加字段时两处同文件同步改，避免 GetRun / ListRunsBySession 各自漂移。
const runColumns = `run_id, session_id, owner_id, entry_agent, query_text, status,
       final_summary_text, error_msg, attachments, created_at, finished_at`

// scanRun 按 runColumns 顺序扫描一行（QueryRow 的 Row 与 Query 的 Rows 均适用）。
func scanRun(row interface{ Scan(dest ...any) error }) (Run, error) {
	var run Run
	err := row.Scan(&run.RunID, &run.SessionID, &run.OwnerID, &run.EntryAgent, &run.QueryText, &run.Status,
		&run.FinalSummaryText, &run.ErrorMsg, &run.Attachments, &run.CreatedAt, &run.FinishedAt)
	return run, err
}

func (r *pgRunRepo) GetRun(ctx context.Context, runID string) (Run, error) {
	run, err := scanRun(r.pool.QueryRow(ctx,
		`SELECT `+runColumns+` FROM runs WHERE run_id = $1`, runID))
	if errors.Is(err, pgx.ErrNoRows) {
		return Run{}, ErrRunNotFound
	}
	if err != nil {
		return Run{}, fmt.Errorf("store: get run: %w", err)
	}
	return run, nil
}

// ListAllRuns 返回**跨 owner** 的最近 runs（created_at 降序），供 admin 后台。
// before 非零时做 keyset 分页。limit 上限 200 防拉全表。
// 与 GetRun 不同：不做任何 owner 过滤——调用方（api/admin.go）已挂 requireAdmin 门控。
//
// 兼容垫片：委托到 ListAllRunsPaged（beforeKey=""）。因 run_id 恒非空 UUID，
// (created_at, run_id) < (before, 空串) 恰等价于旧的 created_at < before——本方法行为逐字不变
// （零回归，保 api/admin.go 与既有 fake 编译）；带 run_id 平手项的正确分页走 ListAllRunsPaged。
func (r *pgRunRepo) ListAllRuns(ctx context.Context, limit int, before time.Time) ([]Run, error) {
	return r.ListAllRunsPaged(ctx, limit, before, "")
}

// ListAllRunsPaged 用复合游标做 keyset 分页（#10）：before 非零时
// WHERE (created_at, run_id) < ($1, $2)，带 run_id tie-breaker，游标为**全精度** created_at
// + 上一页末项 run_id。杜绝页边界同时刻（或同一毫秒）多 run 被静默丢弃。
// ORDER BY created_at DESC, run_id DESC 与游标复合序严格一致。
func (r *pgRunRepo) ListAllRunsPaged(ctx context.Context, limit int, before time.Time, beforeKey string) ([]Run, error) {
	if limit <= 0 || limit > 200 {
		limit = 100
	}
	var rows pgx.Rows
	var err error
	if before.IsZero() {
		rows, err = r.pool.Query(ctx,
			`SELECT `+runColumns+` FROM runs ORDER BY created_at DESC, run_id DESC LIMIT $1`, limit)
	} else {
		rows, err = r.pool.Query(ctx,
			`SELECT `+runColumns+` FROM runs
			  WHERE (created_at, run_id) < ($1, $2)
			  ORDER BY created_at DESC, run_id DESC LIMIT $3`,
			before, beforeKey, limit)
	}
	if err != nil {
		return nil, fmt.Errorf("store: list all runs: %w", err)
	}
	defer rows.Close()
	out := []Run{}
	for rows.Next() {
		run, err := scanRun(rows)
		if err != nil {
			return nil, fmt.Errorf("store: scan run: %w", err)
		}
		out = append(out, run)
	}
	return out, rows.Err()
}

// SessionAttachmentsJSON 实现 SessionAttachmentsReader（聚合与去重在 Go 侧做，SQL 只取原料）。
func (r *pgRunRepo) SessionAttachmentsJSON(ctx context.Context, sessionID, ownerID, excludeRunID string) ([]byte, error) {
	rows, err := r.pool.Query(ctx, `
		SELECT attachments FROM runs
		WHERE session_id = $1 AND owner_id = $2 AND run_id <> $3 AND attachments IS NOT NULL
		ORDER BY created_at ASC`, sessionID, ownerID, excludeRunID)
	if err != nil {
		return nil, fmt.Errorf("store: session attachments: %w", err)
	}
	defer rows.Close()
	seen := map[string]bool{}
	merged := []json.RawMessage{}
	for rows.Next() {
		var raw []byte
		if err := rows.Scan(&raw); err != nil {
			return nil, fmt.Errorf("store: scan session attachments: %w", err)
		}
		var items []map[string]json.RawMessage
		if err := json.Unmarshal(raw, &items); err != nil {
			continue // 单轮附件 JSON 异常不拖垮整体（历史数据容错）
		}
		for _, it := range items {
			var key string
			if rk, ok := it["resourceKey"]; ok {
				_ = json.Unmarshal(rk, &key)
			}
			if key == "" || seen[key] {
				continue
			}
			seen[key] = true
			b, err := json.Marshal(it)
			if err != nil {
				continue
			}
			merged = append(merged, b)
		}
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("store: session attachments rows: %w", err)
	}
	if len(merged) == 0 {
		return nil, nil
	}
	return json.Marshal(merged)
}
