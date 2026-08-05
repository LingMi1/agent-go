package event

// NUL 净化（22P05 回归）：web_fetch 抓到含 NUL 页面曾毒死整个 run。
// PostgreSQL jsonb 拒绝字符串中的 U+0000 转义（"unsupported Unicode escape sequence"）；
// FromProto 入口净化（实时/账本同源，replay ≡ live 保持），MarshalPayload 出口兜底
//（覆盖未逐字段净化的生产者，如 Approval.Input map）。

import (
	"bytes"
	"encoding/json"
	"testing"

	agentv1 "my-agent/control-plane/internal/genproto/agent/v1"
)

// nulEscape 是 JSON 里 NUL 的转义形态（6 个 ASCII 字符），jsonb 落库的违禁项。
var nulEscape = []byte{'\\', 'u', '0', '0', '0', '0'}

func TestFromProtoSanitizesNUL(t *testing.T) {
	ev := &agentv1.Event{
		RunId: "r", Seq: 1, MessageId: "m",
		Type: agentv1.EventType_EVENT_TYPE_TOOL_RESULT,
		Payload: &agentv1.Event_ToolResult{ToolResult: &agentv1.ToolPayload{
			ToolCallId: "c1", ToolName: "web_fetch",
			ToolResult: "前\x00后", Summary: "s\x00",
		}},
	}
	env, err := FromProto(ev)
	if err != nil {
		t.Fatalf("FromProto: %v", err)
	}
	if env.Tool.ToolResult != "前�后" || env.Tool.Summary != "s�" {
		t.Fatalf("NUL not sanitized: %q %q", env.Tool.ToolResult, env.Tool.Summary)
	}
	payload, err := env.MarshalPayload()
	if err != nil {
		t.Fatalf("MarshalPayload: %v", err)
	}
	if bytes.Contains(payload, nulEscape) {
		t.Fatalf("payload still contains NUL escape: %s", payload)
	}
}

func TestMarshalPayloadSanitizesNULFallback(t *testing.T) {
	env := Envelope{
		Type:   TypeResult,
		Result: &ResultPayload{Text: "x\x00y"}, // 绕过 FromProto 直接构造（兜底路径）
	}
	payload, err := env.MarshalPayload()
	if err != nil {
		t.Fatalf("MarshalPayload: %v", err)
	}
	if bytes.Contains(payload, nulEscape) {
		t.Fatalf("payload still contains NUL escape: %s", payload)
	}
	if !bytes.Contains(payload, []byte("x�y")) {
		t.Fatalf("not replaced with U+FFFD: %s", payload)
	}
}

// —— 22P02 回归：字面文本（反斜杠+u0000）与真 NUL 转义的奇偶性区分 ——
// 网页正文里的字面 6 字符序列经 Go 序列化为双反斜杠形态；朴素 ReplaceAll 会把其中
// 子串换成 �，留下孤立反斜杠 → 整条 payload 非法 JSON、run 落库即死。

func TestSanitizeKeepsLiteralBackslashU0000Text(t *testing.T) {
	lit := "\\" + "u0000" // 字面文本：反斜杠 + u0000
	env := Envelope{Type: TypeResult, Result: &ResultPayload{Text: "code: " + lit + " end"}}
	payload, err := env.MarshalPayload()
	if err != nil {
		t.Fatalf("MarshalPayload: %v", err)
	}
	if !json.Valid(payload) {
		t.Fatalf("payload not valid json: %s", payload)
	}
	var back ResultPayload
	if err := json.Unmarshal(payload, &back); err != nil {
		t.Fatalf("round-trip: %v", err)
	}
	if back.Text != "code: "+lit+" end" {
		t.Fatalf("literal text mutated: %q", back.Text)
	}
}

func TestSanitizeMixedLiteralBackslashAndRawNUL(t *testing.T) {
	lit := "\\" // 一个字面反斜杠，后跟真 NUL
	env := Envelope{Type: TypeResult, Result: &ResultPayload{Text: "x" + lit + "\x00y"}}
	payload, err := env.MarshalPayload()
	if err != nil {
		t.Fatalf("MarshalPayload: %v", err)
	}
	if !json.Valid(payload) {
		t.Fatalf("payload not valid json: %s", payload)
	}
	var back ResultPayload
	if err := json.Unmarshal(payload, &back); err != nil {
		t.Fatalf("round-trip: %v", err)
	}
	if back.Text != "x"+lit+"\ufffdy" {
		t.Fatalf("mixed case wrong: %q", back.Text)
	}
}
