-- events：append-only 事实账本（回放源）。心跳不入此表。
CREATE TABLE IF NOT EXISTS events (
    run_id         TEXT    NOT NULL REFERENCES runs (run_id),
    seq            BIGINT  NOT NULL,                 -- 每 run 单调、无空洞、从 1
    message_id     TEXT    NOT NULL,                 -- 原位更新键；tool_call 时 == toolCallId
    message_type   TEXT    NOT NULL
                   CHECK (message_type IN ('tool_thought', 'tool_call', 'tool_result', 'result')),
    is_final       BOOLEAN NOT NULL DEFAULT FALSE,
    finish         BOOLEAN NOT NULL DEFAULT FALSE,
    payload        JSONB   NOT NULL,                 -- 类型相关的 body（足以重建 SSE 帧）
    ts_unix_ms     BIGINT  NOT NULL,                 -- Python 发射时间 → SSE messageTime
    schema_version TEXT    NOT NULL DEFAULT 'v1',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, seq)
);

-- 可选：按 toolCallId 查工具相关事件
CREATE INDEX IF NOT EXISTS idx_events_toolcall
    ON events (run_id, (payload ->> 'toolCallId'))
    WHERE message_type IN ('tool_call', 'tool_result');
