package api_test

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"my-agent/control-plane/internal/store"
)

// docs/14 会话分叉 e2e：登记 → 投影（列表/timeline 继承段）→ 首 run 附 fork metadata
// 两键 → 第二 run 仍附带（播种失败重试自愈，幂等由认知面 checkpoint 闸兜底）→
// 从继承轮再分叉时播种源指向锚点 run 实际执行的会话 → 校验矩阵（404/403 同缸/409/400）。
func TestEndToEnd_ForkFlow(t *testing.T) {
	env := newE2EEnv(t)
	router := env.router

	post := func(user, body string) (string, *httptest.ResponseRecorder) {
		t.Helper()
		req := httptest.NewRequest(http.MethodPost, "/runs", strings.NewReader(body))
		req.Header.Set("X-User-Id", user)
		rec := httptest.NewRecorder()
		router.ServeHTTP(rec, req)
		return rec.Header().Get("X-Run-Id"), rec
	}
	fork := func(user, sessionID, body string) (*httptest.ResponseRecorder, map[string]any) {
		t.Helper()
		req := httptest.NewRequest(http.MethodPost, "/sessions/"+sessionID+"/fork", strings.NewReader(body))
		req.Header.Set("X-User-Id", user)
		rec := httptest.NewRecorder()
		router.ServeHTTP(rec, req)
		var out map[string]any
		_ = json.Unmarshal(rec.Body.Bytes(), &out)
		return rec, out
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

	// 源会话两轮（fakeCog 默认 react 事件，落 SUCCESS）。
	run1, _ := post("u1", `{"query":"我叫小明","sessionId":"s-fork-src"}`)
	run2, _ := post("u1", `{"query":"我在北京","sessionId":"s-fork-src"}`)
	if run1 == "" || run2 == "" {
		t.Fatal("源会话 run 未启动")
	}

	// 1) 分叉登记：从轮 1 之后分叉 → 201 + 新 sessionId。
	rec, out := fork("u1", "s-fork-src", `{"afterRunId":"`+run1+`"}`)
	if rec.Code != http.StatusCreated {
		t.Fatalf("fork: expected 201, got %d %s", rec.Code, rec.Body.String())
	}
	newSession, _ := out["sessionId"].(string)
	if newSession == "" || newSession == "s-fork-src" {
		t.Fatalf("fork 未返回新 sessionId: %v", out)
	}

	// 2) 列表投影：0 own-run 分叉会话可见（标题继承父首问 + forkedFrom）。
	rec, body := get("u1", "/sessions")
	if rec.Code != http.StatusOK {
		t.Fatalf("GET /sessions: %d", rec.Code)
	}
	var forkRow map[string]any
	for _, it := range body["sessions"].([]any) {
		s := it.(map[string]any)
		if s["sessionId"] == newSession {
			forkRow = s
		} else if s["forkedFrom"] != nil && s["forkedFrom"] != "" {
			t.Fatalf("非分叉会话不该带 forkedFrom: %v", s)
		}
	}
	if forkRow == nil {
		t.Fatalf("0 own-run 分叉会话未出现在列表: %v", body)
	}
	if forkRow["forkedFrom"] != "s-fork-src" || forkRow["runCount"] != float64(0) || forkRow["title"] != "我叫小明" {
		t.Fatalf("分叉会话行不对: %v", forkRow)
	}

	// 3) timeline 投影：继承段截至分叉点（含 run1、不含 run2），原 runId + inherited 标记。
	rec, body = get("u1", "/sessions/"+newSession+"/runs")
	if rec.Code != http.StatusOK {
		t.Fatalf("GET fork session runs: %d %s", rec.Code, rec.Body.String())
	}
	runList := body["runs"].([]any)
	if len(runList) != 1 {
		t.Fatalf("继承段应只含分叉点前的轮: %v", runList)
	}
	inheritedRun := runList[0].(map[string]any)
	if inheritedRun["runId"] != run1 || inheritedRun["inherited"] != true {
		t.Fatalf("继承轮应保留原 runId 且标 inherited: %v", inheritedRun)
	}

	// 4) 分叉会话首 run：gRPC metadata 两键贯通。
	if runID, rec := post("u1", `{"query":"我叫什么","sessionId":"`+newSession+`"}`); runID == "" {
		t.Fatalf("fork 首 run 未启动: %d %s", rec.Code, rec.Body.String())
	}
	md := env.cog.lastReq().GetMetadata()
	if md["fork_from_session_id"] != "s-fork-src" || md["fork_from_run_id"] != run1 {
		t.Fatalf("fork metadata 未贯通: %v", md)
	}

	// 5) 第二 run **仍附带** fork 两键：run 行在播种前就已落库，若按"无 own run 才附键"
	//    做一次性闸，首条消息任何失败都会永久关死播种（静默空记忆，docs/14 §2 红线）；
	//    恒附键让重试自愈，幂等由认知面"目标 thread 已有 checkpoint 即跳过"闸兜底。
	if runID, rec := post("u1", `{"query":"再聊一句","sessionId":"`+newSession+`"}`); runID == "" {
		t.Fatalf("fork 第二 run 未启动: %d %s", rec.Code, rec.Body.String())
	}
	md2 := env.cog.lastReq().GetMetadata()
	if md2["fork_from_session_id"] != "s-fork-src" || md2["fork_from_run_id"] != run1 {
		t.Fatalf("第二 run 仍应附 fork metadata（播种失败重试自愈）: %v", md2)
	}

	// 5b) 分叉的分叉·锚在继承轮：newSession 的 timeline 里 run1 是继承轮（实际执行于
	//     s-fork-src）。从它再分叉出孙会话后，播种源必须指向锚点 run 实际执行所在的
	//     会话（其 checkpoint 所在 thread=祖父会话），而非直接父会话 newSession——
	//     否则认知面在错误 thread 里按 run_id 定位必然落空。
	rec, out = fork("u1", newSession, `{"afterRunId":"`+run1+`"}`)
	if rec.Code != http.StatusCreated {
		t.Fatalf("从继承轮分叉: expected 201, got %d %s", rec.Code, rec.Body.String())
	}
	grandChild, _ := out["sessionId"].(string)
	if grandChild == "" || grandChild == newSession {
		t.Fatalf("从继承轮分叉未返回新 sessionId: %v", out)
	}
	if runID, rec := post("u1", `{"query":"孙会话首问","sessionId":"`+grandChild+`"}`); runID == "" {
		t.Fatalf("孙会话首 run 未启动: %d %s", rec.Code, rec.Body.String())
	}
	mdG := env.cog.lastReq().GetMetadata()
	if mdG["fork_from_session_id"] != "s-fork-src" || mdG["fork_from_run_id"] != run1 {
		t.Fatalf("孙会话播种源应指向锚点 run 实际执行的祖父会话: %v", mdG)
	}

	// 6) 校验矩阵。
	// 6a) afterRunId 不属于该会话（属于分叉会话的 own run）→ 404。
	otherRun, _ := post("u1", `{"query":"别的会话","sessionId":"s-unrelated"}`)
	if rec, _ := fork("u1", "s-fork-src", `{"afterRunId":"`+otherRun+`"}`); rec.Code != http.StatusNotFound {
		t.Fatalf("跨会话锚点应 404: %d", rec.Code)
	}
	// 6b) 他人会话 → 404（不泄露存在性）。
	if rec, _ := fork("intruder", "s-fork-src", `{"afterRunId":"`+run1+`"}`); rec.Code != http.StatusNotFound {
		t.Fatalf("他人分叉应 404: %d", rec.Code)
	}
	// 6c) RUNNING 轮 → 409（直接造一条未收口的 run）。
	if err := env.runs.CreateRun(env.ctx, store.CreateRunParams{
		RunID: "r-running", SessionID: "s-fork-src", OwnerID: "u1", QueryText: "运行中",
	}); err != nil {
		t.Fatalf("CreateRun: %v", err)
	}
	if rec, _ := fork("u1", "s-fork-src", `{"afterRunId":"r-running"}`); rec.Code != http.StatusConflict {
		t.Fatalf("RUNNING 轮分叉应 409: %d", rec.Code)
	}
	// 6d) 缺 afterRunId → 400。
	if rec, _ := fork("u1", "s-fork-src", `{}`); rec.Code != http.StatusBadRequest {
		t.Fatalf("缺 afterRunId 应 400: %d", rec.Code)
	}
}

// 父会话删除后，0-own-run 分叉会话的读路径必须与 ListSessions 的存在性判定一致：
// 列表仍列出它（forks 表驱动），打开（GET /sessions/{id}/runs）应返回 200 + 空数组
// 而非 404——否则侧栏可见、点击即报"会话不存在"（幽灵条目）。
func TestEndToEnd_ForkedSessionReadableAfterParentDeleted(t *testing.T) {
	env := newE2EEnv(t)
	router := env.router

	do := func(method, path, body string) *httptest.ResponseRecorder {
		t.Helper()
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		req.Header.Set("X-User-Id", "u1")
		rec := httptest.NewRecorder()
		router.ServeHTTP(rec, req)
		return rec
	}

	// 父会话一轮 → 分叉（不发任何消息）→ 删除父会话。
	rec := do(http.MethodPost, "/runs", `{"query":"父会话一问","sessionId":"s-del-src"}`)
	anchorRun := rec.Header().Get("X-Run-Id")
	if anchorRun == "" {
		t.Fatalf("父会话 run 未启动: %d %s", rec.Code, rec.Body.String())
	}
	rec = do(http.MethodPost, "/sessions/s-del-src/fork", `{"afterRunId":"`+anchorRun+`"}`)
	if rec.Code != http.StatusCreated {
		t.Fatalf("fork: expected 201, got %d %s", rec.Code, rec.Body.String())
	}
	var out map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &out)
	forked, _ := out["sessionId"].(string)
	if forked == "" {
		t.Fatalf("fork 未返回 sessionId: %v", out)
	}
	if rec := do(http.MethodDelete, "/sessions/s-del-src", ""); rec.Code != http.StatusOK {
		t.Fatalf("删除父会话: expected 200, got %d %s", rec.Code, rec.Body.String())
	}

	// 列表仍可见该分叉会话（存在性判定的另一端，对照基准）。
	rec = do(http.MethodGet, "/sessions", "")
	var listBody map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &listBody)
	listed := false
	for _, it := range listBody["sessions"].([]any) {
		if it.(map[string]any)["sessionId"] == forked {
			listed = true
		}
	}
	if !listed {
		t.Fatalf("删父后 0-own-run 分叉会话应仍在列表: %v", listBody)
	}

	// 打开读路径：200 + 空 runs 数组（而非 404），与列表判定一致。
	rec = do(http.MethodGet, "/sessions/"+forked+"/runs", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("删父后打开 0-own-run 分叉会话应 200，得到 %d %s", rec.Code, rec.Body.String())
	}
	var runsBody map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &runsBody)
	runs, ok := runsBody["runs"].([]any)
	if !ok || len(runs) != 0 {
		t.Fatalf("期望空 runs 数组，得到: %s", rec.Body.String())
	}

	// 无 fork 登记的不存在会话维持 404（存在性判定未放宽）。
	if rec := do(http.MethodGet, "/sessions/no-such-session/runs", ""); rec.Code != http.StatusNotFound {
		t.Fatalf("不存在的会话应仍 404: %d", rec.Code)
	}
}
