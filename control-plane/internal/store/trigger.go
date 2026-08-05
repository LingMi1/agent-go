package store

// Proactive 连接器（docs/16）：triggers 表读写。规则 = 事件类型 + 可选过滤 → query 模板。
// owner 域 CRUD 同 schedule.go；ListByConnector 供 poller 匹配（按 connector_id，不判 owner——
// 连接器自身已归属校验，触发的 run OwnerID = 连接器 owner）。

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Trigger 是一条触发规则。Filter 是可选 JSONB（repo/label），NULL 时为空。
type Trigger struct {
	TriggerID     string          `json:"triggerId"`
	OwnerID       string          `json:"-"`
	ConnectorID   string          `json:"connectorId"`
	EventType     string          `json:"eventType"`
	Filter        json.RawMessage `json:"filter,omitempty"`
	QueryTemplate string          `json:"queryTemplate"`
	AgentType     string          `json:"agentType"`
	NeedsApproval bool            `json:"needsApproval"`
	Enabled       bool            `json:"enabled"`
	CreatedAt     time.Time       `json:"createdAt"`
}

// TriggerRepository 是触发规则的读写端口。
type TriggerRepository interface {
	Create(ctx context.Context, t Trigger) error
	ListByOwner(ctx context.Context, ownerID string) ([]Trigger, error)
	ListByConnector(ctx context.Context, connectorID string) ([]Trigger, error)
	Delete(ctx context.Context, ownerID, triggerID string) error
	SetEnabled(ctx context.Context, ownerID, triggerID string, enabled bool) error
}

type pgTriggersRepo struct{ pool *pgxpool.Pool }

// NewTriggerRepository 构造触发规则仓库。
func NewTriggerRepository(pool *pgxpool.Pool) TriggerRepository {
	return &pgTriggersRepo{pool: pool}
}

const triggerColumns = `trigger_id, owner_id, connector_id, event_type, filter,
	query_template, agent_type, needs_approval, enabled, created_at`

func scanTrigger(row pgx.Row) (Trigger, error) {
	var t Trigger
	var filter []byte
	err := row.Scan(&t.TriggerID, &t.OwnerID, &t.ConnectorID, &t.EventType, &filter,
		&t.QueryTemplate, &t.AgentType, &t.NeedsApproval, &t.Enabled, &t.CreatedAt)
	if filter != nil {
		t.Filter = json.RawMessage(filter)
	}
	return t, err
}

func (r *pgTriggersRepo) Create(ctx context.Context, t Trigger) error {
	var filter any
	if len(t.Filter) > 0 {
		filter = []byte(t.Filter)
	}
	_, err := r.pool.Exec(ctx, `
		INSERT INTO triggers (trigger_id, owner_id, connector_id, event_type, filter, query_template, agent_type, needs_approval, enabled)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)`,
		t.TriggerID, t.OwnerID, t.ConnectorID, t.EventType, filter,
		t.QueryTemplate, t.AgentType, t.NeedsApproval, t.Enabled)
	if err != nil {
		return fmt.Errorf("store: create trigger: %w", err)
	}
	return nil
}

func (r *pgTriggersRepo) listWhere(ctx context.Context, where string, arg string) ([]Trigger, error) {
	rows, err := r.pool.Query(ctx,
		`SELECT `+triggerColumns+` FROM triggers WHERE `+where+` ORDER BY created_at DESC`, arg)
	if err != nil {
		return nil, fmt.Errorf("store: list triggers: %w", err)
	}
	defer rows.Close()
	out := []Trigger{}
	for rows.Next() {
		t, err := scanTrigger(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, t)
	}
	return out, rows.Err()
}

func (r *pgTriggersRepo) ListByOwner(ctx context.Context, ownerID string) ([]Trigger, error) {
	return r.listWhere(ctx, "owner_id = $1", ownerID)
}

func (r *pgTriggersRepo) ListByConnector(ctx context.Context, connectorID string) ([]Trigger, error) {
	return r.listWhere(ctx, "connector_id = $1", connectorID)
}

func (r *pgTriggersRepo) Delete(ctx context.Context, ownerID, triggerID string) error {
	tag, err := r.pool.Exec(ctx,
		`DELETE FROM triggers WHERE trigger_id = $1 AND owner_id = $2`, triggerID, ownerID)
	if err != nil {
		return fmt.Errorf("store: delete trigger: %w", err)
	}
	if tag.RowsAffected() == 0 {
		return ErrRunNotFound
	}
	return nil
}

func (r *pgTriggersRepo) SetEnabled(ctx context.Context, ownerID, triggerID string, enabled bool) error {
	tag, err := r.pool.Exec(ctx,
		`UPDATE triggers SET enabled = $3 WHERE trigger_id = $1 AND owner_id = $2`,
		triggerID, ownerID, enabled)
	if err != nil {
		return fmt.Errorf("store: toggle trigger: %w", err)
	}
	if tag.RowsAffected() == 0 {
		return ErrRunNotFound
	}
	return nil
}
