package store

// D3 多用户 + RBAC（docs/17）：users（账号/角色）与 auth_sessions（server 端 token）读写。
// 两个独立端口（UserRepository / SessionTokenRepository）——照 schedule.go 的 port+pg 范式；
// api 层用它们做注册/登录与 resolveIdentity 中间件的 token→身份解析（api/auth.go）。

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

// 角色常量（与迁移 0008 的 CHECK 一致）。
const (
	RoleUser  = "user"
	RoleAdmin = "admin"
)

var (
	// ErrUserExists 表示用户名已被占用（唯一约束冲突）。
	ErrUserExists = errors.New("store: user already exists")
	// ErrUserNotFound 表示按用户名/ID 找不到用户。
	ErrUserNotFound = errors.New("store: user not found")
	// ErrTokenNotFound 表示 token 不存在或已过期（LookupToken 校验未过期）。
	ErrTokenNotFound = errors.New("store: auth token not found or expired")
)

// User 是 users 表的一行。RunCount 不是表列——ListUsers 用 LEFT JOIN runs 聚合填充
// （admin 用户列表展示每个账号的 run 数），其他读路径为 0。PasswordHash 仅在按名登录时读取。
type User struct {
	UserID       string    `json:"userId"`
	Username     string    `json:"username"`
	PasswordHash string    `json:"-"`
	Role         string    `json:"role"`
	CreatedAt    time.Time `json:"createdAt"`
	RunCount     int64     `json:"runCount"`
}

// UserRepository 是账号读写端口。
type UserRepository interface {
	// CreateUser 落库账号；用户名唯一冲突返回 ErrUserExists。
	CreateUser(ctx context.Context, u User) error
	// GetUserByName 按用户名取账号（含 password_hash 供登录校验）；不存在返回 ErrUserNotFound。
	GetUserByName(ctx context.Context, username string) (User, error)
	// ListUsers 列出全部账号 + 每账号 run 计数（admin 后台，跨 owner，不含 password_hash）。
	ListUsers(ctx context.Context) ([]User, error)
	// SetRole 改角色；用户不存在返回 ErrUserNotFound（业务校验——不能给自己降权——在 api 层）。
	SetRole(ctx context.Context, userID, role string) error
}

// SessionTokenRepository 是 auth_sessions 读写端口（server 端可吊销 token）。
type SessionTokenRepository interface {
	CreateToken(ctx context.Context, token, userID string, expiresAt time.Time) error
	// LookupToken 校验 token 未过期并 JOIN users 返回 (userID, role)；无效/过期返回 ErrTokenNotFound。
	LookupToken(ctx context.Context, token string) (userID, role string, err error)
	DeleteToken(ctx context.Context, token string) error
}

type pgUserRepo struct{ pool *pgxpool.Pool }

// NewUserRepository 返回基于 pgx 的 UserRepository。
func NewUserRepository(pool *pgxpool.Pool) UserRepository { return &pgUserRepo{pool: pool} }

func (r *pgUserRepo) CreateUser(ctx context.Context, u User) error {
	role := u.Role
	if role == "" {
		role = RoleUser
	}
	_, err := r.pool.Exec(ctx, `
		INSERT INTO users (user_id, username, password_hash, role)
		VALUES ($1, $2, $3, $4)`,
		u.UserID, u.Username, u.PasswordHash, role)
	if err != nil {
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == "23505" { // unique_violation
			return ErrUserExists
		}
		return fmt.Errorf("store: create user: %w", err)
	}
	return nil
}

func (r *pgUserRepo) GetUserByName(ctx context.Context, username string) (User, error) {
	var u User
	err := r.pool.QueryRow(ctx, `
		SELECT user_id, username, password_hash, role, created_at
		  FROM users WHERE username = $1`, username).
		Scan(&u.UserID, &u.Username, &u.PasswordHash, &u.Role, &u.CreatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return User{}, ErrUserNotFound
	}
	if err != nil {
		return User{}, fmt.Errorf("store: get user: %w", err)
	}
	return u, nil
}

func (r *pgUserRepo) ListUsers(ctx context.Context) ([]User, error) {
	// LEFT JOIN runs 以 owner_id == user_id 聚合 run 计数（docs/17：user_id 即历史 owner_id）。
	rows, err := r.pool.Query(ctx, `
		SELECT u.user_id, u.username, u.role, u.created_at, COUNT(r.run_id)
		  FROM users u
		  LEFT JOIN runs r ON r.owner_id = u.user_id
		 GROUP BY u.user_id, u.username, u.role, u.created_at
		 ORDER BY u.created_at ASC, u.user_id ASC`)
	if err != nil {
		return nil, fmt.Errorf("store: list users: %w", err)
	}
	defer rows.Close()
	out := []User{}
	for rows.Next() {
		var u User
		if err := rows.Scan(&u.UserID, &u.Username, &u.Role, &u.CreatedAt, &u.RunCount); err != nil {
			return nil, fmt.Errorf("store: scan user: %w", err)
		}
		out = append(out, u)
	}
	return out, rows.Err()
}

func (r *pgUserRepo) SetRole(ctx context.Context, userID, role string) error {
	tag, err := r.pool.Exec(ctx, `UPDATE users SET role = $2 WHERE user_id = $1`, userID, role)
	if err != nil {
		return fmt.Errorf("store: set role: %w", err)
	}
	if tag.RowsAffected() == 0 {
		return ErrUserNotFound
	}
	return nil
}

type pgSessionTokenRepo struct{ pool *pgxpool.Pool }

// NewSessionTokenRepository 返回基于 pgx 的 SessionTokenRepository。
func NewSessionTokenRepository(pool *pgxpool.Pool) SessionTokenRepository {
	return &pgSessionTokenRepo{pool: pool}
}

func (r *pgSessionTokenRepo) CreateToken(ctx context.Context, token, userID string, expiresAt time.Time) error {
	_, err := r.pool.Exec(ctx, `
		INSERT INTO auth_sessions (token, user_id, expires_at) VALUES ($1, $2, $3)`,
		token, userID, expiresAt)
	if err != nil {
		return fmt.Errorf("store: create token: %w", err)
	}
	return nil
}

func (r *pgSessionTokenRepo) LookupToken(ctx context.Context, token string) (string, string, error) {
	var userID, role string
	err := r.pool.QueryRow(ctx, `
		SELECT s.user_id, u.role
		  FROM auth_sessions s
		  JOIN users u ON u.user_id = s.user_id
		 WHERE s.token = $1 AND s.expires_at > now()`, token).
		Scan(&userID, &role)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", "", ErrTokenNotFound
	}
	if err != nil {
		return "", "", fmt.Errorf("store: lookup token: %w", err)
	}
	return userID, role, nil
}

func (r *pgSessionTokenRepo) DeleteToken(ctx context.Context, token string) error {
	_, err := r.pool.Exec(ctx, `DELETE FROM auth_sessions WHERE token = $1`, token)
	if err != nil {
		return fmt.Errorf("store: delete token: %w", err)
	}
	return nil
}
