-- 加性扩展 events.message_type CHECK，纳入 Plan-Execute 的新事件类型（幂等）。
-- NOT VALID：迁移集每次启动全量重放——后续迁移加宽类型后，重放本条不得对已有
-- 宽数据施加窄校验（只约束新写入；最终生效的是最后一条重建的最宽 CHECK）。
ALTER TABLE events DROP CONSTRAINT IF EXISTS events_message_type_check;
ALTER TABLE events ADD CONSTRAINT events_message_type_check
    CHECK (message_type IN (
        'tool_thought', 'tool_call', 'tool_result', 'result',
        'plan_thought', 'plan', 'task'
    )) NOT VALID;
