-- D3 · 多用户登录 + RBAC（docs/17）：users（账号 + 角色）与 auth_sessions（server 端 token）。
-- 关键取舍（docs/17 §3.2）：user_id == 现有 owner_id 字符串（用户名）——历史 runs/kb/uploads
-- 的 owner_id 本就是用户名，播种 users 后**零迁移**即归属账号；代价是 user_id 一经创建不可变。
-- 全 IF NOT EXISTS 幂等（Migrate 全量重放，见 store.go）。
CREATE TABLE IF NOT EXISTS users (
    user_id       TEXT PRIMARY KEY,                       -- == owner_id 字符串（不可变）
    username      TEXT UNIQUE NOT NULL,                   -- 登录名/展示名（MVP 与 user_id 同值）
    password_hash TEXT NOT NULL,                          -- bcrypt（golang.org/x/crypto/bcrypt）
    role          TEXT NOT NULL DEFAULT 'user'
                  CHECK (role IN ('user', 'admin')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- auth_sessions：随机 token → user_id，服务端可吊销（logout/过期即失效）。
-- 否掉 JWT：单实例平台无需无状态签名密钥管理，服务端吊销更简单（docs/17 §3.2）。
CREATE TABLE IF NOT EXISTS auth_sessions (
    token      TEXT PRIMARY KEY,                          -- crypto/rand 32 字节 hex
    user_id    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions (user_id);
