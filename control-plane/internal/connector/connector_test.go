package connector_test

import (
	"encoding/json"
	"testing"

	"my-agent/control-plane/internal/connector"
)

// query 模板渲染纯函数：占位替换、缺失键→空、未闭合原样保留、多次出现。
func TestRenderTemplate(t *testing.T) {
	fields := map[string]string{"title": "登录报错", "url": "https://x/1", "repo": "o/r"}
	cases := []struct{ tmpl, want string }{
		{"新 issue：{{title}}（{{repo}}）\n{{url}}", "新 issue：登录报错（o/r）\nhttps://x/1"},
		{"缺失：[{{author}}]", "缺失：[]"},               // 缺失键 → 空串（不残留字面量）
		{"重复 {{repo}} 和 {{repo}}", "重复 o/r 和 o/r"}, // 多次出现
		{"带空格 {{ title }}", "带空格 登录报错"},            // 键两侧空格容忍
		{"无占位", "无占位"},
		{"未闭合 {{title", "未闭合 {{title"}, // 未闭合原样保留
	}
	for _, c := range cases {
		if got := connector.RenderTemplate(c.tmpl, fields); got != c.want {
			t.Errorf("RenderTemplate(%q)=%q want %q", c.tmpl, got, c.want)
		}
	}
}

func TestMatchEvent(t *testing.T) {
	ev := connector.InternalEvent{
		Type:   "issue",
		Fields: map[string]string{"repo": "o/r", "reason": "mention"},
	}
	filterRepo := json.RawMessage(`{"repo":"o/r"}`)
	filterOther := json.RawMessage(`{"repo":"o/other"}`)
	filterMulti := json.RawMessage(`{"repo":"o/r","reason":"mention"}`)

	cases := []struct {
		name      string
		eventType string
		filter    json.RawMessage
		want      bool
	}{
		{"类型+repo 命中", "issue", filterRepo, true},
		{"类型不符", "pull_request", filterRepo, false},
		{"repo 不符", "issue", filterOther, false},
		{"多键全中", "issue", filterMulti, true},
		{"空类型不限 + 空过滤", "", nil, true},
		{"空类型 + repo 过滤命中", "", filterRepo, true},
		{"坏过滤器不阻断", "issue", json.RawMessage(`{bad`), true},
	}
	for _, c := range cases {
		if got := connector.MatchEvent(c.eventType, c.filter, ev); got != c.want {
			t.Errorf("%s: MatchEvent=%v want %v", c.name, got, c.want)
		}
	}
}
