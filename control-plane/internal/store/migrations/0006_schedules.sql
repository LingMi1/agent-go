-- M11 · 定时触发（Proactive）：schedules 表。
-- 固定 per-schedule session（列表单条目、runCount 增长、LangGraph thread 记忆延续）；
-- next_run_at 由调度器原子认领推进（UPDATE ... WHERE next_run_at <= now() RETURNING）。
CREATE TABLE IF NOT EXISTS schedules (
    schedule_id      TEXT PRIMARY KEY,
    owner_id         TEXT NOT NULL,
    session_id       TEXT NOT NULL,
    query_text       TEXT NOT NULL,
    agent_type       TEXT NOT NULL DEFAULT 'react',
    interval_seconds INT  NOT NULL CHECK (interval_seconds >= 60),
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    next_run_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_run_id      TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_schedules_owner ON schedules (owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules (next_run_at) WHERE enabled;
