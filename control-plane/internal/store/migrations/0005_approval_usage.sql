-- M11 · HITL 审批事件类型 + 每 run token 用量列（幂等，循 0003 的 DROP/ADD 模式）。
-- 提前于认知面发新事件类型落库：消除"新类型被 CHECK 拒 → PERSIST_ERROR 杀 run"的窗口。
ALTER TABLE events DROP CONSTRAINT IF EXISTS events_message_type_check;
ALTER TABLE events ADD CONSTRAINT events_message_type_check
    CHECK (message_type IN ('tool_thought', 'tool_call', 'tool_result', 'result',
                            'plan_thought', 'plan', 'task', 'approval_request')) NOT VALID;

-- 用量随终态 RESULT 附带（mapper 全 run 聚合），FinishRun 单点写入。
ALTER TABLE runs ADD COLUMN IF NOT EXISTS input_tokens  BIGINT NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS output_tokens BIGINT NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS model_calls   INT    NOT NULL DEFAULT 0;
