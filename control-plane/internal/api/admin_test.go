package api_test

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"
	"time"

	"my-agent/control-plane/internal/api"
	"my-agent/control-plane/internal/store"
)

// fakeAdminRuns 实现 RunRepository + AllRunsLister + AllRunsPager（跨 owner 返回全部）。
type fakeAdminRuns struct{ all []store.Run }

func (f *fakeAdminRuns) CreateRun(context.Context, store.CreateRunParams) error { return nil }
func (f *fakeAdminRuns) FinishRun(context.Context, store.FinishRunParams) error { return nil }
func (f *fakeAdminRuns) GetRun(_ context.Context, id string) (store.Run, error) {
	for _, r := range f.all {
		if r.RunID == id {
			return r, nil
		}
	}
	return store.Run{}, store.ErrRunNotFound
}
func (f *fakeAdminRuns) ListAllRuns(_ context.Context, limit int, _ time.Time) ([]store.Run, error) {
	if limit > 0 && limit < len(f.all) {
		return f.all[:limit], nil
	}
	return f.all, nil
}

// ListAllRunsPaged：admin.go 现走复合游标端口（#10）；fake 忽略游标返回前 limit 条即可。
func (f *fakeAdminRuns) ListAllRunsPaged(_ context.Context, limit int, _ time.Time, _ string) ([]store.Run, error) {
	if limit > 0 && limit < len(f.all) {
		return f.all[:limit], nil
	}
	return f.all, nil
}

// fakeAdminStats 实现 StatsRepository + AdminStatsReporter。
type fakeAdminStats struct{ admin store.UsageReport }

func (f *fakeAdminStats) UsageReport(context.Context, string, int) (store.UsageReport, error) {
	return store.UsageReport{}, nil
}
func (f *fakeAdminStats) AdminUsageReport(context.Context, int) (store.UsageReport, error) {
	return f.admin, nil
}

func adminRouter(users store.UserRepository, tokens store.SessionTokenRepository, runs store.RunRepository, stats store.StatsRepository) http.Handler {
	return api.NewRouter(nil, runs, nil, nil, nil, nil, nil, nil, stats, nil, nil, users, tokens, nil, nil, time.Minute, "", discardLogger())
}

func TestAdminEndpoints(t *testing.T) {
	ctx := context.Background()
	users := newFakeUsers()
	tokens := newFakeTokens(users)
	_ = users.CreateUser(ctx, store.User{UserID: "root", Username: "root", Role: store.RoleAdmin})
	_ = users.CreateUser(ctx, store.User{UserID: "u1", Username: "u1", Role: store.RoleUser})
	_ = users.CreateUser(ctx, store.User{UserID: "u2", Username: "u2", Role: store.RoleUser})
	users.counts["u1"] = 3
	_ = tokens.CreateToken(ctx, "t-root", "root", time.Now().Add(time.Hour))
	_ = tokens.CreateToken(ctx, "t-u1", "u1", time.Now().Add(time.Hour))
	_ = tokens.CreateToken(ctx, "t-u2", "u2", time.Now().Add(time.Hour)) // u2 保持普通用户，供 403 断言

	runs := &fakeAdminRuns{all: []store.Run{
		{RunID: "r1", OwnerID: "u1", SessionID: "s1", Status: "SUCCESS", EntryAgent: "react", QueryText: "q1"},
		{RunID: "r2", OwnerID: "u2", SessionID: "s2", Status: "SUCCESS", EntryAgent: "react", QueryText: "q2"},
	}}
	stats := &fakeAdminStats{admin: store.UsageReport{Days: 30, Totals: store.UsageTotals{Runs: 2, InputTokens: 30}}}
	router := adminRouter(users, tokens, runs, stats)

	// GET /admin/users → 3 用户 + run 计数。
	rec := doJSON(t, router, http.MethodGet, "/admin/users", "", "t-root", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("admin users: %d %s", rec.Code, rec.Body.String())
	}
	var ul struct {
		Users []struct {
			UserID   string `json:"userId"`
			Role     string `json:"role"`
			RunCount int64  `json:"runCount"`
		} `json:"users"`
	}
	_ = json.Unmarshal(rec.Body.Bytes(), &ul)
	if len(ul.Users) != 3 {
		t.Fatalf("应 3 用户: %d", len(ul.Users))
	}
	for _, u := range ul.Users {
		if u.UserID == "u1" && u.RunCount != 3 {
			t.Fatalf("u1 run 计数应 3: %d", u.RunCount)
		}
	}

	// GET /admin/runs → 跨 owner 两条。
	rec = doJSON(t, router, http.MethodGet, "/admin/runs", "", "t-root", "")
	var rl struct {
		Runs []struct {
			RunID   string `json:"runId"`
			OwnerID string `json:"ownerId"`
		} `json:"runs"`
	}
	_ = json.Unmarshal(rec.Body.Bytes(), &rl)
	if len(rl.Runs) != 2 || rl.Runs[0].OwnerID == rl.Runs[1].OwnerID {
		t.Fatalf("admin runs 应跨 owner 两条: %+v", rl.Runs)
	}

	// GET /admin/stats → 跨 owner 聚合。
	rec = doJSON(t, router, http.MethodGet, "/admin/stats", "", "t-root", "")
	var sr store.UsageReport
	_ = json.Unmarshal(rec.Body.Bytes(), &sr)
	if sr.Totals.Runs != 2 || sr.Totals.InputTokens != 30 {
		t.Fatalf("admin stats 聚合不对: %+v", sr.Totals)
	}

	// PATCH 改角色：u1 升 admin → 200。
	rec = doJSON(t, router, http.MethodPatch, "/admin/users/u1/role", `{"role":"admin"}`, "t-root", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("升角色应 200: %d %s", rec.Code, rec.Body.String())
	}
	if u, _ := users.GetUserByName(ctx, "u1"); u.Role != store.RoleAdmin {
		t.Fatalf("u1 应升为 admin: %s", u.Role)
	}
	// 不能给自己降权 → 400。
	rec = doJSON(t, router, http.MethodPatch, "/admin/users/root/role", `{"role":"user"}`, "t-root", "")
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("自我降权应 400: %d", rec.Code)
	}
	// 非法角色 → 400；不存在用户 → 404。
	if rec := doJSON(t, router, http.MethodPatch, "/admin/users/u2/role", `{"role":"superuser"}`, "t-root", ""); rec.Code != http.StatusBadRequest {
		t.Fatalf("非法角色应 400: %d", rec.Code)
	}
	if rec := doJSON(t, router, http.MethodPatch, "/admin/users/ghost/role", `{"role":"admin"}`, "t-root", ""); rec.Code != http.StatusNotFound {
		t.Fatalf("改不存在用户应 404: %d", rec.Code)
	}

	// 普通用户访问任一 admin 端点 → 403（隔离不被弱化：普通用户绝不见跨 owner）。
	// 用 u2（全程保持 user 角色；u1 上面已被升为 admin）。
	if rec := doJSON(t, router, http.MethodGet, "/admin/runs", "", "t-u2", ""); rec.Code != http.StatusForbidden {
		t.Fatalf("普通用户访问 admin runs 应 403: %d", rec.Code)
	}
}

// 降级：runs/stats 不实现 admin 接口（或 nil）→ admin 端点 503（而非泄露/崩溃）。
func TestAdminDegradesWithoutAdminRepos(t *testing.T) {
	ctx := context.Background()
	users := newFakeUsers()
	tokens := newFakeTokens(users)
	_ = users.CreateUser(ctx, store.User{UserID: "root", Username: "root", Role: store.RoleAdmin})
	_ = tokens.CreateToken(ctx, "t-root", "root", time.Now().Add(time.Hour))
	// runs=nil, stats=nil → 类型断言失败 → 503。
	router := adminRouter(users, tokens, nil, nil)
	if rec := doJSON(t, router, http.MethodGet, "/admin/runs", "", "t-root", ""); rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("无 AllRunsPager 应 503: %d", rec.Code)
	}
	if rec := doJSON(t, router, http.MethodGet, "/admin/stats", "", "t-root", ""); rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("无 AdminStatsReporter 应 503: %d", rec.Code)
	}
}
