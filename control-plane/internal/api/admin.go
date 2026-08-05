package api

// D3 管理后台（docs/17 §3.3）：/admin/* 全挂 requireAdmin（在 api.go 用 chi.Group 装配）。
// 跨 owner 读一律走 store 的新方法（AllRunsLister / AdminStatsReporter），既有 owner 过滤零弱化。

import (
	"encoding/json"
	"net/http"
	"strconv"
	"time"

	"github.com/go-chi/chi/v5"

	"my-agent/control-plane/internal/store"
)

// adminListUsers：GET /admin/users → 全部账号（含 role/created_at/run 计数）。
func (h *handlers) adminListUsers(w http.ResponseWriter, r *http.Request) {
	if h.users == nil {
		writeProblem(w, http.StatusServiceUnavailable, "no_auth", "鉴权未启用")
		return
	}
	users, err := h.users.ListUsers(r.Context())
	if err != nil {
		h.log.Error("admin list users failed", "err", err)
		writeProblem(w, http.StatusInternalServerError, "internal", "用户列表查询失败")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"users": users})
}

type setRoleRequest struct {
	Role string `json:"role"`
}

// adminSetRole：PATCH /admin/users/{id}/role {role} → 改角色。
// 不能给自己降权（防把最后一个 admin 锁死成 user）。
func (h *handlers) adminSetRole(w http.ResponseWriter, r *http.Request) {
	if h.users == nil {
		writeProblem(w, http.StatusServiceUnavailable, "no_auth", "鉴权未启用")
		return
	}
	targetID := chi.URLParam(r, "id")
	var body setRoleRequest
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeProblem(w, http.StatusBadRequest, "bad_request", "请求体解析失败")
		return
	}
	if body.Role != store.RoleUser && body.Role != store.RoleAdmin {
		writeProblem(w, http.StatusBadRequest, "bad_request", "role 必须是 user 或 admin")
		return
	}
	if id, ok := identityFrom(r); ok && id.userID == targetID && body.Role != store.RoleAdmin {
		writeProblem(w, http.StatusBadRequest, "cannot_self_demote", "不能给自己降权")
		return
	}
	err := h.users.SetRole(r.Context(), targetID, body.Role)
	if err == store.ErrUserNotFound {
		writeProblem(w, http.StatusNotFound, "not_found", "用户不存在")
		return
	}
	if err != nil {
		h.log.Error("admin set role failed", "err", err)
		writeProblem(w, http.StatusInternalServerError, "internal", "改角色失败")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"userId": targetID, "role": body.Role})
}

// adminRunJSON 是 GET /admin/runs 的响应行（跨 owner，含 ownerId）。
type adminRunJSON struct {
	RunID      string     `json:"runId"`
	SessionID  string     `json:"sessionId"`
	OwnerID    string     `json:"ownerId"`
	AgentType  string     `json:"agentType"`
	Query      string     `json:"query"`
	Status     string     `json:"status"`
	CreatedAt  time.Time  `json:"createdAt"`
	FinishedAt *time.Time `json:"finishedAt,omitempty"`
}

// adminListRuns：GET /admin/runs?limit=&before=&beforeKey= → 跨 owner 全部 runs（复合游标 keyset 分页）。
// before 为上一页末项 created_at（RFC3339Nano，全精度——评审 #10：unix 毫秒会截断微秒列致边界丢 run）；
// beforeKey 为上一页末项 run_id（平手项 tie-breaker）。二者取自上一页 JSON 的 createdAt/runId 原样回传。
func (h *handlers) adminListRuns(w http.ResponseWriter, r *http.Request) {
	pager, ok := h.runs.(store.AllRunsPager)
	if !ok {
		writeProblem(w, http.StatusServiceUnavailable, "no_runs", "运行存储未配置")
		return
	}
	limit := 100
	if v := r.URL.Query().Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			limit = n
		}
	}
	var before time.Time
	beforeKey := r.URL.Query().Get("beforeKey")
	if v := r.URL.Query().Get("before"); v != "" {
		// 全精度优先（RFC3339Nano）；兼容旧调用方传的 unix 毫秒。
		if t, err := time.Parse(time.RFC3339Nano, v); err == nil {
			before = t
		} else if n, err := strconv.ParseInt(v, 10, 64); err == nil && n > 0 {
			before = time.UnixMilli(n)
		}
	}
	runs, err := pager.ListAllRunsPaged(r.Context(), limit, before, beforeKey)
	if err != nil {
		h.log.Error("admin list runs failed", "err", err)
		writeProblem(w, http.StatusInternalServerError, "internal", "运行列表查询失败")
		return
	}
	out := make([]adminRunJSON, 0, len(runs))
	for _, run := range runs {
		out = append(out, adminRunJSON{
			RunID: run.RunID, SessionID: run.SessionID, OwnerID: run.OwnerID,
			AgentType: run.EntryAgent, Query: run.QueryText, Status: run.Status,
			CreatedAt: run.CreatedAt, FinishedAt: run.FinishedAt,
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"runs": out})
}

// adminStats：GET /admin/stats?days=30 → 系统级用量（跨 owner 聚合）。
func (h *handlers) adminStats(w http.ResponseWriter, r *http.Request) {
	reporter, ok := h.stats.(store.AdminStatsReporter)
	if !ok {
		writeProblem(w, http.StatusServiceUnavailable, "no_stats", "统计未启用")
		return
	}
	days := 30
	if v := r.URL.Query().Get("days"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			days = n
		}
	}
	report, err := reporter.AdminUsageReport(r.Context(), days)
	if err != nil {
		h.log.Error("admin stats failed", "err", err)
		writeProblem(w, http.StatusInternalServerError, "internal", "统计查询失败")
		return
	}
	writeJSON(w, http.StatusOK, report)
}
