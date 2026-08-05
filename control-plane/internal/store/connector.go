package store

// Proactive 连接器（docs/16）：connectors 表读写。范式同 schedule.go——
// port 接口 + connectorColumns/scanConnector + owner 域 WHERE 隔离 +
// Claim 原子推进 next_poll_at（先 Admit 后 Claim 的存储侧保证）。
// token_ciphertext 是 AES-GCM 密文（internal/secret），本层只搬运字节，绝不解密。

import (
	"context"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Connector 是一个外部事件源（当前仅 kind='github'）。
// TokenCiphertext 是密文（nonce||ct），json 标签 "-"：明文与密文都绝不出 API。
type Connector struct {
	ConnectorID     string    `json:"connectorId"`
	OwnerID         string    `json:"-"`
	Kind            string    `json:"kind"`
	TokenCiphertext []byte    `json:"-"`
	Cursor          string    `json:"cursor,omitempty"`
	PollIntervalS   int       `json:"pollIntervalS"`
	Enabled         bool      `json:"enabled"`
	NextPollAt      time.Time `json:"nextPollAt"`
	LastPollID      string    `json:"-"`
	CreatedAt       time.Time `json:"createdAt"`
}

// ConnectorRepository 是连接器的读写端口。
type ConnectorRepository interface {
	Create(ctx context.Context, c Connector) error
	ListByOwner(ctx context.Context, ownerID string) ([]Connector, error)
	Delete(ctx context.Context, ownerID, connectorID string) error
	SetEnabled(ctx context.Context, ownerID, connectorID string, enabled bool) error
	// ListDue 列出到期且启用的候选（不认领——认领在 Admit 成功后逐条做）。
	ListDue(ctx context.Context, limit int) ([]Connector, error)
	// Claim 原子认领一条：推进 next_poll_at；已被禁用/未到期返回 false。
	Claim(ctx context.Context, connectorID string) (bool, error)
	// UpdateCursor 轮询后推进增量游标与 last_poll_id（不判 owner——poller 用连接器 id 定位）。
	UpdateCursor(ctx context.Context, connectorID, cursor, lastPollID string) error
}

type pgConnectorsRepo struct{ pool *pgxpool.Pool }

// NewConnectorRepository 构造连接器仓库。
func NewConnectorRepository(pool *pgxpool.Pool) ConnectorRepository {
	return &pgConnectorsRepo{pool: pool}
}

const connectorColumns = `connector_id, owner_id, kind, token_ciphertext, COALESCE(cursor, ''),
	poll_interval_s, enabled, next_poll_at, COALESCE(last_poll_id, ''), created_at`

func scanConnector(row pgx.Row) (Connector, error) {
	var c Connector
	err := row.Scan(&c.ConnectorID, &c.OwnerID, &c.Kind, &c.TokenCiphertext, &c.Cursor,
		&c.PollIntervalS, &c.Enabled, &c.NextPollAt, &c.LastPollID, &c.CreatedAt)
	return c, err
}

func (r *pgConnectorsRepo) Create(ctx context.Context, c Connector) error {
	_, err := r.pool.Exec(ctx, `
		INSERT INTO connectors (connector_id, owner_id, kind, token_ciphertext, cursor, poll_interval_s, enabled)
		VALUES ($1, $2, $3, $4, NULLIF($5, ''), $6, $7)`,
		c.ConnectorID, c.OwnerID, c.Kind, c.TokenCiphertext, c.Cursor, c.PollIntervalS, c.Enabled)
	if err != nil {
		return fmt.Errorf("store: create connector: %w", err)
	}
	return nil
}

func (r *pgConnectorsRepo) ListByOwner(ctx context.Context, ownerID string) ([]Connector, error) {
	rows, err := r.pool.Query(ctx,
		`SELECT `+connectorColumns+` FROM connectors WHERE owner_id = $1 ORDER BY created_at DESC`, ownerID)
	if err != nil {
		return nil, fmt.Errorf("store: list connectors: %w", err)
	}
	defer rows.Close()
	out := []Connector{}
	for rows.Next() {
		c, err := scanConnector(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}

// Delete 删除连接器，并在**同一事务内级联删除该连接器的触发规则**（#11）。
// triggers.connector_id 无外键（迁移已交付，不改），故应用层显式级联——否则删连接器后
// 触发规则悬空：ListByOwner 仍返回且 enabled=true（UI 误显示为「已启用」），但 poller 只对
// 仍存在的连接器 ListByConnector，孤儿规则永不触发；且悬空规则仍计入 maxTriggersPerOwner 配额。
// 先删连接器并按 owner 判 not-found（不存在/非本人即整体回滚，不误删他人 triggers），
// 再按 connector_id 清掉其全部触发规则，最后提交（同 session→events 事务级联范式）。
func (r *pgConnectorsRepo) Delete(ctx context.Context, ownerID, connectorID string) error {
	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("store: delete connector begin: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }() // Commit 后为 no-op（ErrTxClosed 忽略）

	tag, err := tx.Exec(ctx,
		`DELETE FROM connectors WHERE connector_id = $1 AND owner_id = $2`, connectorID, ownerID)
	if err != nil {
		return fmt.Errorf("store: delete connector: %w", err)
	}
	if tag.RowsAffected() == 0 {
		return ErrRunNotFound // 复用 not-found 语义（不存在或非本人）——回滚，不动 triggers
	}
	if _, err := tx.Exec(ctx,
		`DELETE FROM triggers WHERE connector_id = $1`, connectorID); err != nil {
		return fmt.Errorf("store: delete connector triggers: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return fmt.Errorf("store: delete connector commit: %w", err)
	}
	return nil
}

func (r *pgConnectorsRepo) SetEnabled(ctx context.Context, ownerID, connectorID string, enabled bool) error {
	tag, err := r.pool.Exec(ctx,
		`UPDATE connectors SET enabled = $3 WHERE connector_id = $1 AND owner_id = $2`,
		connectorID, ownerID, enabled)
	if err != nil {
		return fmt.Errorf("store: toggle connector: %w", err)
	}
	if tag.RowsAffected() == 0 {
		return ErrRunNotFound
	}
	return nil
}

func (r *pgConnectorsRepo) ListDue(ctx context.Context, limit int) ([]Connector, error) {
	rows, err := r.pool.Query(ctx,
		`SELECT `+connectorColumns+` FROM connectors
		  WHERE enabled AND next_poll_at <= now()
		  ORDER BY next_poll_at LIMIT $1`, limit)
	if err != nil {
		return nil, fmt.Errorf("store: list due connectors: %w", err)
	}
	defer rows.Close()
	out := []Connector{}
	for rows.Next() {
		c, err := scanConnector(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}

func (r *pgConnectorsRepo) Claim(ctx context.Context, connectorID string) (bool, error) {
	tag, err := r.pool.Exec(ctx, `
		UPDATE connectors
		   SET next_poll_at = now() + make_interval(secs => poll_interval_s)
		 WHERE connector_id = $1 AND enabled AND next_poll_at <= now()`,
		connectorID)
	if err != nil {
		return false, fmt.Errorf("store: claim connector: %w", err)
	}
	return tag.RowsAffected() == 1, nil
}

func (r *pgConnectorsRepo) UpdateCursor(ctx context.Context, connectorID, cursor, lastPollID string) error {
	_, err := r.pool.Exec(ctx,
		`UPDATE connectors SET cursor = NULLIF($2, ''), last_poll_id = NULLIF($3, '') WHERE connector_id = $1`,
		connectorID, cursor, lastPollID)
	if err != nil {
		return fmt.Errorf("store: update connector cursor: %w", err)
	}
	return nil
}
