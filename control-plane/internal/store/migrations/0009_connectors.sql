-- docs/16 · Proactive 连接器（GitHub 轮询版）：connectors 表。
-- 事件源层：每行是一个外部数据源（当前仅 kind='github'，PAT 轮询）。
-- PAT 绝不明文落列——token_ciphertext 存 AES-GCM(nonce||ct)（internal/secret，主密钥 SECRET_MASTER_KEY）。
-- next_poll_at 由独立 poller 原子认领推进（UPDATE ... WHERE next_poll_at<=now()），语义同 schedules.next_run_at。
CREATE TABLE IF NOT EXISTS connectors (
    connector_id     TEXT PRIMARY KEY,
    owner_id         TEXT NOT NULL,
    kind             TEXT NOT NULL,                 -- 'github'
    token_ciphertext BYTEA NOT NULL,                -- AES-GCM(nonce||ct)，明文 PAT 绝不落列
    cursor           TEXT,                          -- 增量游标（notifications since 时间戳）
    poll_interval_s  INT  NOT NULL CHECK (poll_interval_s >= 60),
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    next_poll_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_poll_id     TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_connectors_owner ON connectors (owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_connectors_due ON connectors (next_poll_at) WHERE enabled;
