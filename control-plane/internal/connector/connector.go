// Package connector 定义 Proactive 事件源的三法接口（Authorize/Poll/Normalize，docs/16 §3.3）
// 与规则匹配/模板渲染纯函数。当前仅 GitHub PAT 轮询实现（github.go）。
//
// 分层：本包不 import store——Poll 收到的是 Conn（含解密后的瞬时 PAT + 游标），
// 由 poller 从 store.Connector 解密装配。这样加密/持久化留在 poller/store，
// 连接器只关心"怎么拉外部 API + 怎么规整成内部事件"。
package connector

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
)

// RawEvent 是连接器拉回的一条原始事件（GitHub 通知/issue/pr 的规整前投影）。
type RawEvent struct {
	ID          string // 事件唯一 id（去重/last_poll_id 用）
	SubjectType string // GitHub subject.type：'Issue' / 'PullRequest'
	Reason      string // GitHub notification reason：'mention' / 'assign' / ...
	Title       string
	URL         string // subject.url（API url）
	Repo        string // repository.full_name，如 'octocat/hello'
	UpdatedAt   string // RFC3339；游标推进用
}

// InternalEvent 是规整后的内部事件（docs/16 §3.3：{type, fields{title,body,url,repo,author}}）。
type InternalEvent struct {
	Type   string
	Fields map[string]string
}

// Conn 是连接器视角的连接器配置（PAT 已解密、瞬时——用完即弃，绝不回写库）。
type Conn struct {
	Kind   string
	PAT    string
	Cursor string
}

// Connector 是事件源三法接口（对齐 docs/10 的 authorize/subscribe/normalize 轮询版）。
type Connector interface {
	// Authorize 校验凭据有效性（GitHub：GET /user）；成功即可 Seal 入库。
	Authorize(ctx context.Context, ownerID, pat string) error
	// Poll 增量拉取（cursor 游标）；返回原始事件、推进后的新游标、错误。
	Poll(ctx context.Context, conn Conn) ([]RawEvent, string, error)
	// Normalize 把原始事件规整成内部事件。
	Normalize(raw RawEvent) InternalEvent
}

// RenderTemplate 是 query 模板渲染纯函数：把 {{key}} 替换成 fields[key]。
// 缺失键替换成空串（不残留 {{key}} 字面量）。未闭合的 "{{" 原样保留。
func RenderTemplate(tmpl string, fields map[string]string) string {
	var b strings.Builder
	for {
		i := strings.Index(tmpl, "{{")
		if i < 0 {
			b.WriteString(tmpl)
			break
		}
		j := strings.Index(tmpl[i:], "}}")
		if j < 0 {
			b.WriteString(tmpl) // 未闭合：原样输出剩余
			break
		}
		b.WriteString(tmpl[:i])
		key := strings.TrimSpace(tmpl[i+2 : i+j])
		b.WriteString(fields[key]) // 缺失键 → 空串
		tmpl = tmpl[i+j+2:]
	}
	return b.String()
}

// MatchEvent 判断内部事件是否命中一条触发规则的 event_type + filter。
//   - eventType 空 → 不限事件类型；否则须逐字相等。
//   - filter 是 JSONB（可空）：其每个键值对都须与 ev.Fields[键] 相等（AND 语义）；
//     非字符串值转成字符串比较；filter 空/nil → 只按 event_type 匹配。
func MatchEvent(eventType string, filter json.RawMessage, ev InternalEvent) bool {
	if eventType != "" && eventType != ev.Type {
		return false
	}
	if len(filter) == 0 {
		return true
	}
	var m map[string]any
	if err := json.Unmarshal(filter, &m); err != nil || m == nil {
		return true // 过滤器损坏 → 不阻断（宽松：仍按 event_type 命中）
	}
	for k, v := range m {
		if filterValueString(v) != ev.Fields[k] {
			return false
		}
	}
	return true
}

// filterValueString 把过滤器值转成字符串以比对事件字段：字符串直用，
// 其它（数字/布尔）走 %v（JSON 数字为 float64，5 → "5"）。
func filterValueString(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return fmt.Sprintf("%v", v)
}
