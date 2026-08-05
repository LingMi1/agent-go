package connector_test

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"my-agent/control-plane/internal/connector"
)

// Authorize：fake GET /user 200 → 成功；401 → 失败。透传 Authorization: token <PAT>。
func TestGitHubAuthorize(t *testing.T) {
	var gotAuth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		if r.URL.Path != "/user" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		if gotAuth != "token good-pat" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"login":"octocat"}`))
	}))
	defer srv.Close()
	gh := connector.NewGitHub(connector.WithBaseURL(srv.URL), connector.WithHTTPClient(srv.Client()))

	if err := gh.Authorize(context.Background(), "u1", "good-pat"); err != nil {
		t.Fatalf("有效 PAT 应通过: %v", err)
	}
	if gotAuth != "token good-pat" {
		t.Fatalf("Authorization 头不对: %q", gotAuth)
	}
	if err := gh.Authorize(context.Background(), "u1", "bad-pat"); err == nil {
		t.Fatal("无效 PAT 应失败")
	}
	if err := gh.Authorize(context.Background(), "u1", "  "); err == nil {
		t.Fatal("空 PAT 应失败")
	}
}

const notificationsJSON = `[
  {"id":"n1","reason":"mention","updated_at":"2026-07-05T10:00:00Z",
   "subject":{"title":"登录报错","url":"https://api.github.com/repos/o/r/issues/5","type":"Issue"},
   "repository":{"full_name":"o/r"}},
  {"id":"n2","reason":"assign","updated_at":"2026-07-05T11:30:00Z",
   "subject":{"title":"新 PR","url":"https://api.github.com/repos/o/r/pulls/9","type":"PullRequest"},
   "repository":{"full_name":"o/r"}}
]`

// Poll：fake /notifications 返回固定 JSON → 解析成 RawEvent + 推进游标（最大 updated_at）。
// 带 cursor 时透传 since 查询参数（增量）。
func TestGitHubPoll(t *testing.T) {
	var gotSince string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/notifications" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		gotSince = r.URL.Query().Get("since")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(notificationsJSON))
	}))
	defer srv.Close()
	gh := connector.NewGitHub(connector.WithBaseURL(srv.URL), connector.WithHTTPClient(srv.Client()))

	events, cursor, err := gh.Poll(context.Background(), connector.Conn{PAT: "p", Cursor: "2026-07-01T00:00:00Z"})
	if err != nil {
		t.Fatalf("poll: %v", err)
	}
	if gotSince != "2026-07-01T00:00:00Z" {
		t.Fatalf("since 未透传: %q", gotSince)
	}
	if len(events) != 2 {
		t.Fatalf("应 2 事件，得 %d", len(events))
	}
	if cursor != "2026-07-05T11:30:00Z" {
		t.Fatalf("游标应推进到最大 updated_at，得 %q", cursor)
	}
	if events[0].ID != "n1" || events[0].SubjectType != "Issue" || events[0].Repo != "o/r" {
		t.Fatalf("事件0 解析不对: %+v", events[0])
	}

	// Normalize：Issue→issue，PullRequest→pull_request；API url 转网页 url。
	iev := gh.Normalize(events[0])
	if iev.Type != "issue" || iev.Fields["title"] != "登录报错" || iev.Fields["repo"] != "o/r" {
		t.Fatalf("Normalize issue 不对: %+v", iev)
	}
	if !strings.Contains(iev.Fields["url"], "github.com/o/r/issues/5") {
		t.Fatalf("url 未转网页形态: %q", iev.Fields["url"])
	}
	pev := gh.Normalize(events[1])
	if pev.Type != "pull_request" || !strings.Contains(pev.Fields["url"], "github.com/o/r/pull/9") {
		t.Fatalf("Normalize pr 不对: %+v", pev)
	}
}

// Poll 翻页（#5）：/notifications 返回两页（第 1 页 Link 头 rel="next" 指向第 2 页），
// Poll 应跟随 next 拉完两页、累积全部事件、游标取**全页**最大 updated_at——单页时第 2 页
// 及更旧事件会被 since 游标永久跳过（丢事件）。
func TestGitHubPollPaginates(t *testing.T) {
	page1 := `[
	  {"id":"n1","reason":"mention","updated_at":"2026-07-05T09:00:00Z",
	   "subject":{"title":"最旧但在第1页尾","url":"https://api.github.com/repos/o/r/issues/1","type":"Issue"},
	   "repository":{"full_name":"o/r"}},
	  {"id":"n2","reason":"mention","updated_at":"2026-07-05T12:00:00Z",
	   "subject":{"title":"最新","url":"https://api.github.com/repos/o/r/issues/2","type":"Issue"},
	   "repository":{"full_name":"o/r"}}
	]`
	// 第 2 页事件 updated_at 更旧（降序分页的尾部）——正是单页版会丢的那批。
	page2 := `[
	  {"id":"n3","reason":"assign","updated_at":"2026-07-05T08:30:00Z",
	   "subject":{"title":"更旧-第2页","url":"https://api.github.com/repos/o/r/issues/3","type":"Issue"},
	   "repository":{"full_name":"o/r"}},
	  {"id":"n4","reason":"assign","updated_at":"2026-07-05T08:00:00Z",
	   "subject":{"title":"最旧-第2页","url":"https://api.github.com/repos/o/r/pulls/4","type":"PullRequest"},
	   "repository":{"full_name":"o/r"}}
	]`
	var page1Path string
	var perPageSeen, sinceSeen string
	var hits int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/notifications" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		hits++
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Query().Get("page") == "2" {
			_, _ = w.Write([]byte(page2)) // 第 2 页：无 next Link，翻页在此终止
			return
		}
		// 第 1 页：记录首页参数，回 Link 头指向自身第 2 页（httptest 绝对 URL，Poll 跟随之）。
		perPageSeen = r.URL.Query().Get("per_page")
		sinceSeen = r.URL.Query().Get("since")
		page1Path = r.URL.Path
		next := "http://" + r.Host + "/notifications?all=false&per_page=50&page=2"
		w.Header().Set("Link", "<"+next+`>; rel="next", <`+next+`>; rel="last"`)
		_, _ = w.Write([]byte(page1))
	}))
	defer srv.Close()
	gh := connector.NewGitHub(connector.WithBaseURL(srv.URL), connector.WithHTTPClient(srv.Client()))

	events, cursor, err := gh.Poll(context.Background(), connector.Conn{PAT: "p", Cursor: "2026-07-01T00:00:00Z"})
	if err != nil {
		t.Fatalf("poll: %v", err)
	}
	if hits != 2 {
		t.Fatalf("应发两次请求（跟随 next 翻页），得 %d", hits)
	}
	if page1Path != "/notifications" || perPageSeen != "50" || sinceSeen != "2026-07-01T00:00:00Z" {
		t.Fatalf("首页参数不对: path=%q per_page=%q since=%q", page1Path, perPageSeen, sinceSeen)
	}
	// 两页 4 条全部返回，一条不丢。
	if len(events) != 4 {
		t.Fatalf("应累积两页共 4 事件，得 %d：%+v", len(events), events)
	}
	ids := map[string]bool{}
	for _, e := range events {
		ids[e.ID] = true
	}
	for _, want := range []string{"n1", "n2", "n3", "n4"} {
		if !ids[want] {
			t.Fatalf("事件 %s 被丢弃（翻页丢事件）：%+v", want, events)
		}
	}
	// 游标取全部页最大 updated_at（n2=12:00），而非某一页局部最大。
	if cursor != "2026-07-05T12:00:00Z" {
		t.Fatalf("游标应为全页最大 updated_at 2026-07-05T12:00:00Z，得 %q", cursor)
	}
}

// parseNextLink 单元：仅在存在 rel="next" 时返回其 URL，否则空串。
func TestPollNextLinkTermination(t *testing.T) {
	// 末页只有 rel="prev"/rel="first" → 无 next → Poll 应止于本页（不无限翻页）。
	var hits int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits++
		prev := "http://" + r.Host + "/notifications?page=1"
		w.Header().Set("Link", "<"+prev+`>; rel="prev", <`+prev+`>; rel="first"`)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`[{"id":"only","updated_at":"2026-07-05T01:00:00Z","subject":{"type":"Issue"},"repository":{"full_name":"o/r"}}]`))
	}))
	defer srv.Close()
	gh := connector.NewGitHub(connector.WithBaseURL(srv.URL), connector.WithHTTPClient(srv.Client()))
	events, _, err := gh.Poll(context.Background(), connector.Conn{PAT: "p"})
	if err != nil {
		t.Fatalf("poll: %v", err)
	}
	if hits != 1 || len(events) != 1 {
		t.Fatalf("无 next 应止于单页: hits=%d n=%d", hits, len(events))
	}
}

// Poll 错误码 → 报错且不推进游标；304 → 空批沿用旧游标。
func TestGitHubPollStatus(t *testing.T) {
	code := http.StatusForbidden
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(code)
	}))
	defer srv.Close()
	gh := connector.NewGitHub(connector.WithBaseURL(srv.URL), connector.WithHTTPClient(srv.Client()))

	if _, cur, err := gh.Poll(context.Background(), connector.Conn{PAT: "p", Cursor: "c0"}); err == nil || cur != "c0" {
		t.Fatalf("403 应报错且不推进游标: cur=%q err=%v", cur, err)
	}
	code = http.StatusNotModified
	ev, cur, err := gh.Poll(context.Background(), connector.Conn{PAT: "p", Cursor: "c0"})
	if err != nil || len(ev) != 0 || cur != "c0" {
		t.Fatalf("304 应空批沿用游标: n=%d cur=%q err=%v", len(ev), cur, err)
	}
}
