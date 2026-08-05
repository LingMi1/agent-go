package store

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// ListSessions 的 limit 边界：<=0 用默认值，超上限截断（防一次拉全表）。
const (
	defaultSessionLimit = 50
	maxSessionLimit     = 200
)

// maxForkDepth 是 fork 链上溯的深度上限（配合 visited 环护栏双保险）。
// 链条由人手动逐次分叉形成，属短链；上限只防病态数据，不是业务边界。
const maxForkDepth = 32

// ErrForkNotFound 表示按 sessionID（+owner）找不到 fork 登记。
var ErrForkNotFound = errors.New("store: session fork not found")

// SessionSummary 是 GET /sessions 的一行：runs 表按 (owner_id, session_id) 聚合的
// 只读投影。会话没有独立表——标题/entryAgent 取会话首条 run（M7 取舍：零新表、
// 单一事实源；将来要"重命名会话"再建覆盖表，见 docs/08 §2）。
// docs/14：分叉会话（session_forks 有行）即便 0 own-run 也出现在列表——标题/entryAgent
// 回退取父会话首条 run，runCount 只数 own runs，ForkedFrom 携带父会话 id。
type SessionSummary struct {
	SessionID    string
	Title        string    // 会话首条 run 的 query_text（展示截断由前端做）；分叉且无 own run 时取父会话首问
	EntryAgent   string    // 会话首条 run 的 entry_agent（分叉且无 own run 时取父）
	RunCount     int       // 只数 own runs（继承段不计，成本/轮次不重复计数）
	CreatedAt    time.Time // 首条 own run 时间；无 own run 的分叉会话取 fork 创建时间
	LastActiveAt time.Time // greatest(fork 创建时间, 最近一条 own run 时间)
	ForkedFrom   string    // 父会话 id（非分叉会话为空）
}

// SessionFork 是 session_forks 表的一行（分叉登记：新会话 → 父会话 + 分叉锚点 run）。
type SessionFork struct {
	SessionID       string
	ParentSessionID string
	ForkAfterRunID  string
	OwnerID         string
	CreatedAt       time.Time
}

// SessionRepository 是会话投影端口（与 RunRepository 分开：调用方只依赖读能力，
// 既有 RunRepository 的 fake 实现不受影响）。
type SessionRepository interface {
	ListSessions(ctx context.Context, ownerID string, limit int) ([]SessionSummary, error)
	ListRunsBySession(ctx context.Context, ownerID, sessionID string) ([]Run, error)
	// DeleteSession 删除某 owner 的整段会话（其全部 runs 及 events）。
	// 返回删除的 run 数（0 表示无此会话/非本人——调用方据此判 404 不泄露他人会话存在性）。
	DeleteSession(ctx context.Context, ownerID, sessionID string) (int64, error)
	// CreateFork 登记一次分叉（业务校验——run 归属/终态/会话归属——在 api 层完成）。
	CreateFork(ctx context.Context, f SessionFork) error
	// GetFork 取某会话的分叉登记（owner 过滤写进 SQL：他人/不存在统一 ErrForkNotFound）。
	GetFork(ctx context.Context, ownerID, sessionID string) (SessionFork, error)
}

type pgSessionRepo struct{ pool *pgxpool.Pool }

// NewSessionRepository 返回基于 pgx 的 SessionRepository。
func NewSessionRepository(pool *pgxpool.Pool) SessionRepository { return &pgSessionRepo{pool: pool} }

func (r *pgSessionRepo) ListSessions(ctx context.Context, ownerID string, limit int) ([]SessionSummary, error) {
	if limit <= 0 {
		limit = defaultSessionLimit
	}
	if limit > maxSessionLimit {
		limit = maxSessionLimit
	}
	// own：own runs 聚合（title/entry_agent 取会话内 created_at 最早一行，run_id 决平局）。
	// forks：本 owner 的分叉登记。FULL OUTER JOIN 让"有 own run 的普通/分叉会话"与
	// "0 own-run 的分叉会话"（own 侧为 NULL）都出现；标题/entryAgent 对后者回退父会话
	// 首条 run（parent_first）；父会话已删除则回退占位文案（继承段查空是刻意语义，不报错）。
	rows, err := r.pool.Query(ctx, `
		WITH own AS (
			SELECT session_id,
			       (array_agg(query_text  ORDER BY created_at ASC, run_id ASC))[1] AS title,
			       (array_agg(entry_agent ORDER BY created_at ASC, run_id ASC))[1] AS entry_agent,
			       count(*)::int   AS run_count,
			       min(created_at) AS created_at,
			       max(created_at) AS last_active_at
			  FROM runs
			 WHERE owner_id = $1
			 GROUP BY session_id
		),
		forks AS (
			SELECT session_id, parent_session_id, created_at AS fork_created_at
			  FROM session_forks
			 WHERE owner_id = $1
		),
		parent_first AS (
			SELECT f.session_id,
			       (array_agg(r.query_text  ORDER BY r.created_at ASC, r.run_id ASC))[1] AS title,
			       (array_agg(r.entry_agent ORDER BY r.created_at ASC, r.run_id ASC))[1] AS entry_agent
			  FROM forks f
			  JOIN runs r ON r.session_id = f.parent_session_id AND r.owner_id = $1
			 GROUP BY f.session_id
		)
		SELECT COALESCE(o.session_id, f.session_id)                    AS session_id,
		       COALESCE(o.title, pf.title, '（分叉会话）')             AS title,
		       COALESCE(o.entry_agent, pf.entry_agent, 'react')        AS entry_agent,
		       COALESCE(o.run_count, 0)                                AS run_count,
		       COALESCE(o.created_at, f.fork_created_at)               AS created_at,
		       GREATEST(COALESCE(o.last_active_at, f.fork_created_at),
		                COALESCE(f.fork_created_at, o.last_active_at)) AS last_active_at,
		       COALESCE(f.parent_session_id, '')                       AS forked_from
		  FROM own o
		  FULL OUTER JOIN forks f ON f.session_id = o.session_id
		  LEFT JOIN parent_first pf ON pf.session_id = f.session_id
		 ORDER BY last_active_at DESC
		 LIMIT $2`, ownerID, limit)
	if err != nil {
		return nil, fmt.Errorf("store: list sessions: %w", err)
	}
	defer rows.Close()

	out := make([]SessionSummary, 0, limit)
	for rows.Next() {
		var s SessionSummary
		if err := rows.Scan(&s.SessionID, &s.Title, &s.EntryAgent, &s.RunCount, &s.CreatedAt, &s.LastActiveAt, &s.ForkedFrom); err != nil {
			return nil, fmt.Errorf("store: scan session: %w", err)
		}
		out = append(out, s)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("store: list sessions rows: %w", err)
	}
	return out, nil
}

// ListRunsBySession 返回会话 timeline：[最远祖先继承段…, 父继承段, own runs]。
//
// docs/14 §4.2：继承历史是父会话 runs 的只读投影（原 run_id 原事件，Inherited=true），
// 绝不复制。fork 链在 Go 侧迭代上溯（人手动分叉属短链，代码可读优先于递归 CTE）：
//   - 深度上限 maxForkDepth + visited 集合双护栏，病态环数据下必然终止；
//   - 每个祖先取"其 own runs 中 (created_at, run_id) <= 分叉锚点"的前缀——锚点取
//     链上已见锚点的最早者（min）：分叉自继承轮（锚点 run 属更远祖先）时，中间祖先
//     的前缀自然为空、截断继续对更远祖先生效，正确实现"继承段的递归截断"；
//   - 锚点 run 已被删（父会话被删除）→ 链就地终止，timeline 只剩已解析段（不报错，
//     "继承历史随父删除而消失"是登记过的刻意语义）。
func (r *pgSessionRepo) ListRunsBySession(ctx context.Context, ownerID, sessionID string) ([]Run, error) {
	// own runs（owner 过滤写进 SQL：他人会话与不存在的会话同样返回空，上层统一 404，
	// 不泄露存在性）。
	own, err := r.listOwnRuns(ctx, ownerID, sessionID, nil)
	if err != nil {
		return nil, err
	}

	// 沿 fork 链上溯，把每个祖先的继承段（倒序发现，头插拼装）拼到 own 前面。
	var segments [][]Run
	visited := map[string]bool{sessionID: true}
	cur := sessionID
	var cutoff *Run // 链上已见锚点的最早者（run 序：created_at, run_id）
	for depth := 0; depth < maxForkDepth; depth++ {
		fork, err := r.GetFork(ctx, ownerID, cur)
		if errors.Is(err, ErrForkNotFound) {
			break // 非分叉会话（或链到头）
		}
		if err != nil {
			return nil, err
		}
		if visited[fork.ParentSessionID] {
			break // 环护栏：病态数据（父链兜圈）就地终止
		}
		visited[fork.ParentSessionID] = true

		// 锚点 run：owner 校验一并做（跨 owner 的 fork 行属病态数据，等同锚点丢失）。
		anchor, err := r.getOwnedRun(ctx, ownerID, fork.ForkAfterRunID)
		if errors.Is(err, ErrRunNotFound) {
			break // 父会话（或锚点 run）已删除 → 继承段到此为止
		}
		if err != nil {
			return nil, err
		}
		if cutoff == nil || runBefore(anchor, *cutoff) {
			cutoff = &anchor
		}
		seg, err := r.listOwnRuns(ctx, ownerID, fork.ParentSessionID, cutoff)
		if err != nil {
			return nil, err
		}
		for i := range seg {
			seg[i].Inherited = true
		}
		segments = append(segments, seg)
		cur = fork.ParentSessionID
	}

	// segments 是 [父, 祖父, …]，timeline 要 [最远祖先…, 父, own]。
	out := make([]Run, 0, len(own)+len(segments))
	for i := len(segments) - 1; i >= 0; i-- {
		out = append(out, segments[i]...)
	}
	out = append(out, own...)
	return out, nil
}

// listOwnRuns 取某会话自己的 runs（created_at 升序）；cutoff 非空时只取
// (created_at, run_id) <= 锚点的前缀（行序比较与 ORDER BY 平局规则一致，确定性截断）。
func (r *pgSessionRepo) listOwnRuns(ctx context.Context, ownerID, sessionID string, cutoff *Run) ([]Run, error) {
	q := `SELECT ` + runColumns + `
	  FROM runs
	 WHERE owner_id = $1 AND session_id = $2`
	args := []any{ownerID, sessionID}
	if cutoff != nil {
		q += ` AND (created_at, run_id) <= ($3, $4)`
		args = append(args, cutoff.CreatedAt, cutoff.RunID)
	}
	q += ` ORDER BY created_at ASC, run_id ASC`
	rows, err := r.pool.Query(ctx, q, args...)
	if err != nil {
		return nil, fmt.Errorf("store: list runs by session: %w", err)
	}
	defer rows.Close()

	var out []Run
	for rows.Next() {
		run, err := scanRun(rows)
		if err != nil {
			return nil, fmt.Errorf("store: scan run: %w", err)
		}
		out = append(out, run)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("store: list runs by session rows: %w", err)
	}
	return out, nil
}

// getOwnedRun 按 runID 取 run 且校验 owner（不匹配等同不存在）。
func (r *pgSessionRepo) getOwnedRun(ctx context.Context, ownerID, runID string) (Run, error) {
	run, err := scanRun(r.pool.QueryRow(ctx,
		`SELECT `+runColumns+` FROM runs WHERE run_id = $1 AND owner_id = $2`, runID, ownerID))
	if errors.Is(err, pgx.ErrNoRows) {
		return Run{}, ErrRunNotFound
	}
	if err != nil {
		return Run{}, fmt.Errorf("store: get owned run: %w", err)
	}
	return run, nil
}

// runBefore 判断 a 在 run 序（created_at, run_id）上早于 b。
func runBefore(a, b Run) bool {
	if a.CreatedAt.Equal(b.CreatedAt) {
		return a.RunID < b.RunID
	}
	return a.CreatedAt.Before(b.CreatedAt)
}

func (r *pgSessionRepo) CreateFork(ctx context.Context, f SessionFork) error {
	_, err := r.pool.Exec(ctx, `
		INSERT INTO session_forks (session_id, parent_session_id, fork_after_run_id, owner_id)
		VALUES ($1, $2, $3, $4)`,
		f.SessionID, f.ParentSessionID, f.ForkAfterRunID, f.OwnerID)
	if err != nil {
		return fmt.Errorf("store: create fork: %w", err)
	}
	return nil
}

func (r *pgSessionRepo) GetFork(ctx context.Context, ownerID, sessionID string) (SessionFork, error) {
	var f SessionFork
	err := r.pool.QueryRow(ctx, `
		SELECT session_id, parent_session_id, fork_after_run_id, owner_id, created_at
		  FROM session_forks
		 WHERE session_id = $1 AND owner_id = $2`, sessionID, ownerID).
		Scan(&f.SessionID, &f.ParentSessionID, &f.ForkAfterRunID, &f.OwnerID, &f.CreatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return SessionFork{}, ErrForkNotFound
	}
	if err != nil {
		return SessionFork{}, fmt.Errorf("store: get fork: %w", err)
	}
	return f, nil
}

// DeleteSession 在事务内删该 owner 会话的 events（先，events.run_id 外键引用 runs，
// 无 ON DELETE CASCADE）再删 runs。owner 过滤写进 WHERE：他人会话删 0 行→上层 404。
// 注：LangGraph checkpoint（thread_id=session_id）由认知面拥有，此处不清（无害孤儿，
// 新会话用新 UUID；彻底清理另作认知面任务，见 docs/10）。
// docs/14：本会话**作为子**的 session_forks 行一并删（否则 0 own-run 的分叉登记会让
// "已删"会话在列表里复活成幽灵条目）；本会话**作为父**的行保留——子会话继承段随锚点
// run 删除自然查空，模型记忆不受影响（checkpoint 已播种到子 thread），是登记过的语义。
// 返回删除的 run 数 + fork 登记数之和（0 = 无此会话/非本人 → 上层 404；零 own-run 的
// 分叉会话删的是登记行，同样能删成功）。
func (r *pgSessionRepo) DeleteSession(ctx context.Context, ownerID, sessionID string) (int64, error) {
	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return 0, fmt.Errorf("store: delete session begin: %w", err)
	}
	defer tx.Rollback(ctx) //nolint:errcheck — 提交后回滚是 no-op

	// 先删 events（子表）：只删属于本 owner+session 的 run 的事件。
	if _, err := tx.Exec(ctx, `
		DELETE FROM events
		 WHERE run_id IN (SELECT run_id FROM runs WHERE owner_id = $1 AND session_id = $2)`,
		ownerID, sessionID); err != nil {
		return 0, fmt.Errorf("store: delete session events: %w", err)
	}
	tag, err := tx.Exec(ctx, `DELETE FROM runs WHERE owner_id = $1 AND session_id = $2`, ownerID, sessionID)
	if err != nil {
		return 0, fmt.Errorf("store: delete session runs: %w", err)
	}
	forkTag, err := tx.Exec(ctx,
		`DELETE FROM session_forks WHERE owner_id = $1 AND session_id = $2`, ownerID, sessionID)
	if err != nil {
		return 0, fmt.Errorf("store: delete session fork: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return 0, fmt.Errorf("store: delete session commit: %w", err)
	}
	return tag.RowsAffected() + forkTag.RowsAffected(), nil
}
