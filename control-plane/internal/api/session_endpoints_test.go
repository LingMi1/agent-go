package api_test

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"my-agent/control-plane/internal/store"
)

// M7 会话端点端到端：两次 run 同 sessionId → 列表聚合 runCount=2 / run 明细升序 / owner 隔离 404。
func TestEndToEnd_SessionsEndpoints(t *testing.T) {
	env := newE2EEnv(t)
	router := env.router

	post := func(user, body string) string {
		t.Helper()
		req := httptest.NewRequest(http.MethodPost, "/runs", strings.NewReader(body))
		req.Header.Set("X-User-Id", user)
		rec := httptest.NewRecorder()
		router.ServeHTTP(rec, req)
		runID := rec.Header().Get("X-Run-Id")
		if runID == "" {
			t.Fatalf("missing X-Run-Id for %s", body)
		}
		return runID
	}
	get := func(user, path string) (*httptest.ResponseRecorder, map[string]any) {
		t.Helper()
		req := httptest.NewRequest(http.MethodGet, path, nil)
		req.Header.Set("X-User-Id", user)
		rec := httptest.NewRecorder()
		router.ServeHTTP(rec, req)
		var body map[string]any
		_ = json.Unmarshal(rec.Body.Bytes(), &body)
		return rec, body
	}

	// u1 同一会话两次 run（续聊），u2 另一会话一次 run。
	run1 := post("u1", `{"query":"第一问：算 2*(3+4)","sessionId":"sess-m7"}`)
	run2 := post("u1", `{"query":"第二问：再算一次","sessionId":"sess-m7"}`)
	_ = post("u2", `{"query":"别人的问题","sessionId":"sess-other"}`)

	// 1) GET /sessions：u1 只见 sess-m7，runCount=2，标题=首条 query。
	rec, body := get("u1", "/sessions")
	if rec.Code != http.StatusOK {
		t.Fatalf("GET /sessions: %d %s", rec.Code, rec.Body.String())
	}
	list, _ := body["sessions"].([]any)
	if len(list) != 1 {
		t.Fatalf("expected 1 session for u1, got %d: %v", len(list), body)
	}
	s0, _ := list[0].(map[string]any)
	if s0["sessionId"] != "sess-m7" || s0["runCount"] != float64(2) {
		t.Fatalf("session summary wrong: %v", s0)
	}
	if s0["title"] != "第一问：算 2*(3+4)" || s0["entryAgent"] != "react" {
		t.Fatalf("title/entryAgent wrong: %v", s0)
	}
	if s0["createdAt"] == "" || s0["lastActiveAt"] == "" {
		t.Fatalf("timestamps missing: %v", s0)
	}

	// 2) GET /sessions/{id}/runs：升序两条，runId 与发起时一致，终态 SUCCESS。
	rec, body = get("u1", "/sessions/sess-m7/runs")
	if rec.Code != http.StatusOK || body["sessionId"] != "sess-m7" {
		t.Fatalf("GET /sessions/{id}/runs: %d %v", rec.Code, body)
	}
	runList, _ := body["runs"].([]any)
	if len(runList) != 2 {
		t.Fatalf("expected 2 runs, got %d: %v", len(runList), body)
	}
	r0, _ := runList[0].(map[string]any)
	r1, _ := runList[1].(map[string]any)
	if r0["runId"] != run1 || r1["runId"] != run2 {
		t.Fatalf("run order wrong: %v %v (want %s %s)", r0["runId"], r1["runId"], run1, run2)
	}
	if r0["query"] != "第一问：算 2*(3+4)" || r0["status"] != store.StatusSuccess {
		t.Fatalf("run fields wrong: %v", r0)
	}

	// 3) owner 隔离：u2 列表只见自己的会话；访问他人会话 404（不泄露存在性）。
	_, body = get("u2", "/sessions")
	l2, _ := body["sessions"].([]any)
	if len(l2) != 1 || l2[0].(map[string]any)["sessionId"] != "sess-other" {
		t.Fatalf("u2 isolation broken: %v", body)
	}
	if rec, _ := get("u2", "/sessions/sess-m7/runs"); rec.Code != http.StatusNotFound {
		t.Fatalf("expected 404 for other owner, got %d", rec.Code)
	}
	if rec, _ := get("u1", "/sessions/ghost/runs"); rec.Code != http.StatusNotFound {
		t.Fatalf("expected 404 for unknown session, got %d", rec.Code)
	}

	// 4) limit 校验：非法 limit → 400；limit=1 正常。
	if rec, _ := get("u1", "/sessions?limit=abc"); rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400 for bad limit, got %d", rec.Code)
	}
	if rec, body := get("u1", "/sessions?limit=1"); rec.Code != http.StatusOK {
		t.Fatalf("limit=1 failed: %d %v", rec.Code, body)
	}
}
