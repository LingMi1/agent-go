package api

// 定时任务管理端点（M11 Proactive）。owner 域内 CRUD；触发由 internal/scheduler 负责。
// 固定 per-schedule session：会话列表单条目、runCount 增长、LangGraph thread 记忆延续。

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"

	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"

	"my-agent/control-plane/internal/store"
)

const (
	minScheduleIntervalS = 60
	maxSchedulesPerOwner = 20 // 防失控堆积（单用户演示平台的朴素上限）
)

type createScheduleRequest struct {
	Query           string `json:"query"`
	AgentType       string `json:"agentType"`
	IntervalSeconds int    `json:"intervalSeconds"`
}

func (h *handlers) listSchedules(w http.ResponseWriter, r *http.Request) {
	if h.schedules == nil {
		writeProblem(w, http.StatusServiceUnavailable, "no_schedules", "定时任务未启用")
		return
	}
	items, err := h.schedules.ListByOwner(r.Context(), ownerOf(r))
	if err != nil {
		h.log.Error("list schedules failed", "err", err)
		writeProblem(w, http.StatusInternalServerError, "internal", "查询失败")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"schedules": items})
}

func (h *handlers) createSchedule(w http.ResponseWriter, r *http.Request) {
	if h.schedules == nil {
		writeProblem(w, http.StatusServiceUnavailable, "no_schedules", "定时任务未启用")
		return
	}
	var body createScheduleRequest
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || strings.TrimSpace(body.Query) == "" {
		writeProblem(w, http.StatusBadRequest, "bad_request", "query 必填")
		return
	}
	if body.IntervalSeconds < minScheduleIntervalS {
		writeProblem(w, http.StatusBadRequest, "bad_request", "间隔最短 60 秒")
		return
	}
	agentType := body.AgentType
	switch agentType {
	case "plan_solve", "deep_research":
	default:
		agentType = "react"
	}
	owner := ownerOf(r)
	// cap：查询失败保守拒绝（不放行——否则 list error 时上限形同虚设，评审#12）。
	existing, err := h.schedules.ListByOwner(r.Context(), owner)
	if err != nil {
		h.log.Error("list schedules for cap failed", "err", err)
		writeProblem(w, http.StatusInternalServerError, "internal", "创建失败")
		return
	}
	if len(existing) >= maxSchedulesPerOwner {
		writeProblem(w, http.StatusBadRequest, "too_many", "定时任务过多，请先清理")
		return
	}
	id := uuid.NewString()
	// session_id 恒服务端生成（评审#10：客户端传入不校验归属可写入他人 thread，
	// checkpointer 按 thread_id 恢复 → 跨会话上下文注入）。不接受 body.SessionID。
	sched := store.Schedule{
		ScheduleID: id, OwnerID: owner,
		SessionID:       "sched-" + id[:8], // 固定会话：thread 记忆延续 + 列表单条目
		QueryText:       strings.TrimSpace(body.Query),
		AgentType:       agentType,
		IntervalSeconds: body.IntervalSeconds,
		Enabled:         true,
	}
	if err := h.schedules.Create(r.Context(), sched); err != nil {
		h.log.Error("create schedule failed", "err", err)
		writeProblem(w, http.StatusInternalServerError, "internal", "创建失败")
		return
	}
	writeJSON(w, http.StatusOK, sched)
}

func (h *handlers) deleteSchedule(w http.ResponseWriter, r *http.Request) {
	if h.schedules == nil {
		writeProblem(w, http.StatusServiceUnavailable, "no_schedules", "定时任务未启用")
		return
	}
	err := h.schedules.Delete(r.Context(), ownerOf(r), chi.URLParam(r, "scheduleID"))
	if errors.Is(err, store.ErrRunNotFound) {
		writeProblem(w, http.StatusNotFound, "not_found", "定时任务不存在")
		return
	}
	if err != nil {
		writeProblem(w, http.StatusInternalServerError, "internal", "删除失败")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

type toggleScheduleRequest struct {
	Enabled bool `json:"enabled"`
}

func (h *handlers) toggleSchedule(w http.ResponseWriter, r *http.Request) {
	if h.schedules == nil {
		writeProblem(w, http.StatusServiceUnavailable, "no_schedules", "定时任务未启用")
		return
	}
	var body toggleScheduleRequest
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeProblem(w, http.StatusBadRequest, "bad_request", "参数错误")
		return
	}
	err := h.schedules.SetEnabled(r.Context(), ownerOf(r), chi.URLParam(r, "scheduleID"), body.Enabled)
	if errors.Is(err, store.ErrRunNotFound) {
		writeProblem(w, http.StatusNotFound, "not_found", "定时任务不存在")
		return
	}
	if err != nil {
		writeProblem(w, http.StatusInternalServerError, "internal", "更新失败")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}
