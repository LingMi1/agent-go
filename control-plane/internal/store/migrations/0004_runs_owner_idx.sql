-- M7 会话列表：GET /sessions 按 owner 聚合 runs，需要 owner 维度索引（幂等）。
CREATE INDEX IF NOT EXISTS idx_runs_owner_created ON runs (owner_id, created_at DESC);
