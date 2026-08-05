// Package store 提供控制面的 PostgreSQL 持久化：runs（run 生命周期）与
// events（append-only 事实账本，回放源）。本阶段用 pgx 直接手写查询；
// 若后续需要可平滑换成 sqlc 生成（不影响接口）。
package store

import (
	"context"
	"embed"
	"fmt"
	"sort"

	"github.com/jackc/pgx/v5/pgxpool"
)

//go:embed migrations/*.sql
var migrationsFS embed.FS

// NewPool 创建并校验一个 pgx 连接池。
func NewPool(ctx context.Context, dsn string) (*pgxpool.Pool, error) {
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		return nil, fmt.Errorf("store: new pool: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("store: ping: %w", err)
	}
	return pool, nil
}

// Migrate 按文件名顺序执行内嵌的迁移 SQL（幂等，均为 IF NOT EXISTS）。
// 本阶段不做版本追踪表，保持最小；后续可换 golang-migrate。
func Migrate(ctx context.Context, pool *pgxpool.Pool) error {
	entries, err := migrationsFS.ReadDir("migrations")
	if err != nil {
		return fmt.Errorf("store: read migrations: %w", err)
	}
	names := make([]string, 0, len(entries))
	for _, e := range entries {
		if !e.IsDir() {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)
	for _, name := range names {
		sqlBytes, err := migrationsFS.ReadFile("migrations/" + name)
		if err != nil {
			return fmt.Errorf("store: read %s: %w", name, err)
		}
		if _, err := pool.Exec(ctx, string(sqlBytes)); err != nil {
			return fmt.Errorf("store: exec %s: %w", name, err)
		}
	}
	return nil
}
