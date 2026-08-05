package store

// 定时触发（M11 Proactive）：schedules 表读写。
// 认领语义：Claim 用单条 UPDATE ... WHERE due AND enabled RETURNING 原子推进
// next_run_at 并记 last_run_id——先 Admit 后 Claim（反序会在满载时静默丢一拍）。

import (
	"context"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Schedule 是一条定时任务。
type Schedule struct {
	ScheduleID      string    `json:"scheduleId"`
	OwnerID         string    `json:"-"`
	SessionID       string    `json:"sessionId"`
	QueryText       string    `json:"query"`
	AgentType       string    `json:"agentType"`
	IntervalSeconds int       `json:"intervalSeconds"`
	Enabled         bool      `json:"enabled"`
	NextRunAt       time.Time `json:"nextRunAt"`
	LastRunID       string    `json:"lastRunId,omitempty"`
	CreatedAt       time.Time `json:"createdAt"`
}

// SchedulesRepository 是定时任务的读写端口。
type SchedulesRepository interface {
	Create(ctx context.Context, s Schedule) error
	ListByOwner(ctx context.Context, ownerID string) ([]Schedule, error)
	Delete(ctx context.Context, ownerID, scheduleID string) error
	SetEnabled(ctx context.Context, ownerID, scheduleID string, enabled bool) error
	// ListDue 列出到期且启用的候选（不认领——认领在 Admit 成功后逐条做）。
	ListDue(ctx context.Context, limit int) ([]Schedule, error)
	// Claim 原子认领一条：推进 next_run_at、记 last_run_id；已被禁用/未到期返回 false。
	Claim(ctx context.Context, scheduleID, runID string) (bool, error)
}

type pgSchedulesRepo struct{ pool *pgxpool.Pool }

// NewSchedulesRepository 构造定时任务仓库。
func NewSchedulesRepository(pool *pgxpool.Pool) SchedulesRepository {
	return &pgSchedulesRepo{pool: pool}
}

const scheduleColumns = `schedule_id, owner_id, session_id, query_text, agent_type,
	interval_seconds, enabled, next_run_at, COALESCE(last_run_id, ''), created_at`

func scanSchedule(row pgx.Row) (Schedule, error) {
	var s Schedule
	err := row.Scan(&s.ScheduleID, &s.OwnerID, &s.SessionID, &s.QueryText, &s.AgentType,
		&s.IntervalSeconds, &s.Enabled, &s.NextRunAt, &s.LastRunID, &s.CreatedAt)
	return s, err
}

func (r *pgSchedulesRepo) Create(ctx context.Context, s Schedule) error {
	_, err := r.pool.Exec(ctx, `
		INSERT INTO schedules (schedule_id, owner_id, session_id, query_text, agent_type, interval_seconds, enabled)
		VALUES ($1, $2, $3, $4, $5, $6, $7)`,
		s.ScheduleID, s.OwnerID, s.SessionID, s.QueryText, s.AgentType, s.IntervalSeconds, s.Enabled)
	if err != nil {
		return fmt.Errorf("store: create schedule: %w", err)
	}
	return nil
}

func (r *pgSchedulesRepo) ListByOwner(ctx context.Context, ownerID string) ([]Schedule, error) {
	rows, err := r.pool.Query(ctx,
		`SELECT `+scheduleColumns+` FROM schedules WHERE owner_id = $1 ORDER BY created_at DESC`, ownerID)
	if err != nil {
		return nil, fmt.Errorf("store: list schedules: %w", err)
	}
	defer rows.Close()
	out := []Schedule{}
	for rows.Next() {
		s, err := scanSchedule(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, s)
	}
	return out, nil
}

func (r *pgSchedulesRepo) Delete(ctx context.Context, ownerID, scheduleID string) error {
	tag, err := r.pool.Exec(ctx,
		`DELETE FROM schedules WHERE schedule_id = $1 AND owner_id = $2`, scheduleID, ownerID)
	if err != nil {
		return fmt.Errorf("store: delete schedule: %w", err)
	}
	if tag.RowsAffected() == 0 {
		return ErrRunNotFound // 复用 not-found 语义（不存在或非本人）
	}
	return nil
}

func (r *pgSchedulesRepo) SetEnabled(ctx context.Context, ownerID, scheduleID string, enabled bool) error {
	tag, err := r.pool.Exec(ctx,
		`UPDATE schedules SET enabled = $3 WHERE schedule_id = $1 AND owner_id = $2`,
		scheduleID, ownerID, enabled)
	if err != nil {
		return fmt.Errorf("store: toggle schedule: %w", err)
	}
	if tag.RowsAffected() == 0 {
		return ErrRunNotFound
	}
	return nil
}

func (r *pgSchedulesRepo) ListDue(ctx context.Context, limit int) ([]Schedule, error) {
	rows, err := r.pool.Query(ctx,
		`SELECT `+scheduleColumns+` FROM schedules
		  WHERE enabled AND next_run_at <= now()
		  ORDER BY next_run_at LIMIT $1`, limit)
	if err != nil {
		return nil, fmt.Errorf("store: list due: %w", err)
	}
	defer rows.Close()
	out := []Schedule{}
	for rows.Next() {
		s, err := scanSchedule(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, s)
	}
	return out, nil
}

func (r *pgSchedulesRepo) Claim(ctx context.Context, scheduleID, runID string) (bool, error) {
	tag, err := r.pool.Exec(ctx, `
		UPDATE schedules
		   SET next_run_at = now() + make_interval(secs => interval_seconds),
		       last_run_id = $2
		 WHERE schedule_id = $1 AND enabled AND next_run_at <= now()`,
		scheduleID, runID)
	if err != nil {
		return false, fmt.Errorf("store: claim schedule: %w", err)
	}
	return tag.RowsAffected() == 1, nil
}
