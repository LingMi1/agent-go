package store

import (
	"context"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"my-agent/control-plane/internal/event"
)

// ErrDuplicateSeq 表示同一 run 的 seq 重复（违反 (run_id, seq) 唯一）。
// 它是 seq 完整性闸门的一部分：Python 分配 seq，Go 用唯一约束兜底。
var ErrDuplicateSeq = errors.New("store: duplicate (run_id, seq)")

// EventRepository 是 append-only 事实账本的写读端口（回放源）。
type EventRepository interface {
	Append(ctx context.Context, e event.Envelope) error
	ListByRun(ctx context.Context, runID string) ([]event.Envelope, error)
}

type pgEventRepo struct{ pool *pgxpool.Pool }

// NewEventRepository 返回基于 pgx 的 EventRepository。
func NewEventRepository(pool *pgxpool.Pool) EventRepository { return &pgEventRepo{pool: pool} }

func (r *pgEventRepo) Append(ctx context.Context, e event.Envelope) error {
	payload, err := e.MarshalPayload()
	if err != nil {
		return fmt.Errorf("store: marshal payload: %w", err)
	}
	_, err = r.pool.Exec(ctx, `
		INSERT INTO events (run_id, seq, message_id, message_type, is_final, finish, payload, ts_unix_ms)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
		e.RunID, e.Seq, e.MessageID, string(e.Type), e.IsFinal, e.Finish, payload, e.TSUnixMs)
	if err != nil {
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == "23505" { // unique_violation
			return ErrDuplicateSeq
		}
		return fmt.Errorf("store: append event: %w", err)
	}
	return nil
}

func (r *pgEventRepo) ListByRun(ctx context.Context, runID string) ([]event.Envelope, error) {
	rows, err := r.pool.Query(ctx, `
		SELECT run_id, seq, message_id, message_type, is_final, finish, payload, ts_unix_ms
		  FROM events WHERE run_id = $1 ORDER BY seq ASC`, runID)
	if err != nil {
		return nil, fmt.Errorf("store: list events: %w", err)
	}
	defer rows.Close()

	var out []event.Envelope
	for rows.Next() {
		var (
			e          event.Envelope
			msgType    string
			payload    []byte
		)
		if err := rows.Scan(&e.RunID, &e.Seq, &e.MessageID, &msgType, &e.IsFinal, &e.Finish, &payload, &e.TSUnixMs); err != nil {
			return nil, fmt.Errorf("store: scan event: %w", err)
		}
		e.SchemaVersion = event.SchemaVersion
		e.Type = event.MessageType(msgType)
		if err := e.UnmarshalPayload(payload); err != nil {
			return nil, fmt.Errorf("store: rebuild payload (seq=%d): %w", e.Seq, err)
		}
		out = append(out, e)
	}
	if err := rows.Err(); err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, fmt.Errorf("store: rows: %w", err)
	}
	return out, nil
}
