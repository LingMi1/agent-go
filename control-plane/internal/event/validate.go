package event

import "fmt"

// Validate 校验一个 Envelope 是否满足契约不变量。
// 这些不变量是“重放=实时”和前端正确归并的前提，故在入库前强制。
func (e Envelope) Validate() error {
	if e.Seq < 1 {
		return fmt.Errorf("event: seq must be >= 1, got %d", e.Seq)
	}
	if e.RunID == "" {
		return fmt.Errorf("event: runID is required")
	}
	if e.MessageID == "" {
		return fmt.Errorf("event: messageID is required")
	}

	// finish 仅在 result 为 true（整个 run 终态）。
	if (e.Type == TypeResult) != e.Finish {
		return fmt.Errorf("event: finish must be true iff type==result (type=%q finish=%v)", e.Type, e.Finish)
	}

	switch e.Type {
	case TypeToolThought:
		if e.Thought == nil {
			return fmt.Errorf("event: tool_thought requires thought payload")
		}
	case TypeToolCall:
		if e.Tool == nil {
			return fmt.Errorf("event: tool_call requires tool payload")
		}
		if e.MessageID != e.Tool.ToolCallID {
			return fmt.Errorf("event: tool_call messageID (%q) must equal toolCallID (%q)", e.MessageID, e.Tool.ToolCallID)
		}
		switch e.Tool.Status {
		case StatusRunning:
			if e.IsFinal {
				return fmt.Errorf("event: tool_call running must have isFinal=false")
			}
		case StatusSuccess, StatusFailed:
			if !e.IsFinal {
				return fmt.Errorf("event: tool_call %s must have isFinal=true", e.Tool.Status)
			}
		default:
			return fmt.Errorf("event: tool_call has invalid status %q", e.Tool.Status)
		}
	case TypeToolResult:
		if e.Tool == nil {
			return fmt.Errorf("event: tool_result requires tool payload")
		}
		if !e.IsFinal {
			return fmt.Errorf("event: tool_result must have isFinal=true")
		}
	case TypeResult:
		if e.Result == nil {
			return fmt.Errorf("event: result requires result payload")
		}
		if !e.IsFinal {
			return fmt.Errorf("event: result must have isFinal=true")
		}
	case TypePlanThought:
		if e.Thought == nil {
			return fmt.Errorf("event: plan_thought requires thought payload")
		}
	case TypePlan:
		if e.Plan == nil {
			return fmt.Errorf("event: plan requires plan payload")
		}
		if !e.IsFinal {
			return fmt.Errorf("event: plan must have isFinal=true")
		}
	case TypeTask:
		if e.Task == nil {
			return fmt.Errorf("event: task requires task payload")
		}
		if !e.IsFinal {
			return fmt.Errorf("event: task must have isFinal=true")
		}
	case TypeApprovalRequest:
		if e.Approval == nil {
			return fmt.Errorf("event: approval_request requires approval payload")
		}
		if !e.IsFinal {
			return fmt.Errorf("event: approval_request must have isFinal=true")
		}
		// (Type==Result)!=Finish 硬不变量已自动禁止 finish=true——审批=run 边界。
	default:
		return fmt.Errorf("event: invalid message type %q", e.Type)
	}
	return nil
}
