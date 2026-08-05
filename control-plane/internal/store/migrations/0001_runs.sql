-- runs：run 生命周期/状态（Go 控制面拥有的事实）
CREATE TABLE IF NOT EXISTS runs (
    run_id             TEXT PRIMARY KEY,
    session_id         TEXT NOT NULL,
    owner_id           TEXT NOT NULL,
    entry_agent        TEXT NOT NULL DEFAULT 'react',
    query_text         TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'RUNNING'
                       CHECK (status IN ('RUNNING', 'SUCCESS', 'FAILED', 'STOPPED', 'TIMEOUT')),
    final_summary_text TEXT,
    error_msg          TEXT,
    schema_version     TEXT NOT NULL DEFAULT 'v1',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_runs_session ON runs (session_id, created_at DESC);
