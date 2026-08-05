-- docs/16 · Proactive 连接器：triggers 表（触发规则层）。
-- 一条规则 = 事件类型 + 可选过滤（repo/label）→ query 模板 → 起一个 run（走既有 dispatch.Run）。
-- needs_approval 命中时，触发的 run 会被引导对高危动作走 M11 HITL 审批闸。
CREATE TABLE IF NOT EXISTS triggers (
    trigger_id     TEXT PRIMARY KEY,
    owner_id       TEXT NOT NULL,
    connector_id   TEXT NOT NULL,
    event_type     TEXT NOT NULL,                  -- 'issue' / 'pull_request' / 'mention'
    filter         JSONB,                          -- 可选过滤（repo / label）；NULL=不过滤
    query_template TEXT NOT NULL,                  -- 含 {{title}}/{{body}}/{{url}}/{{repo}} 占位
    agent_type     TEXT NOT NULL DEFAULT 'react',
    needs_approval BOOLEAN NOT NULL DEFAULT FALSE,
    enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_triggers_connector ON triggers (connector_id);
