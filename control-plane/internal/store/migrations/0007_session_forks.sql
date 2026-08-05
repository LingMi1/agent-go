-- 会话分叉/时间旅行（docs/14）：session_forks 登记表。
-- 分叉 = 新会话：new session（PK）指向父会话与分叉锚点 run；继承历史是读时投影
-- （ListSessions/ListRunsBySession LEFT JOIN/上溯），events/runs 绝不复制（账本纯度 +
-- 成本不重复计数）。父会话删除后本表行保留，继承段自然查空（LEFT JOIN 无 runs，不报错）。
CREATE TABLE IF NOT EXISTS session_forks (
    session_id        TEXT PRIMARY KEY,
    parent_session_id TEXT NOT NULL,
    fork_after_run_id TEXT NOT NULL,
    owner_id          TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_session_forks_parent ON session_forks (parent_session_id);
