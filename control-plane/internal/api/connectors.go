package api

// Proactive 连接器管理端点（docs/16）。owner 域内 CRUD；轮询由 internal/poller 负责。
// 降级：连接器仓库 nil **或** 主密钥未配置（SECRET_MASTER_KEY 空）→ 503（PAT 无从加密，
// 功能整体不可用）。PAT 明文只在本文件 Seal 一次即入密文列，响应绝不回传（Connector/Trigger
// 的密文与 owner 字段 json:"-"）。

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"

	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"

	"my-agent/control-plane/internal/secret"
	"my-agent/control-plane/internal/store"
)

const (
	minPollIntervalS      = 60
	maxConnectorsPerOwner = 20
	maxTriggersPerOwner   = 50
	maxPatBytes           = 512  // GitHub PAT 远短于此，防异常大 body 进加密
	maxQueryTemplateBytes = 4000 // query 模板上限
)

// connectorsReady 判断连接器功能是否可用：仓库已装配且主密钥已配置。
func (h *handlers) connectorsReady(w http.ResponseWriter) bool {
	if h.connectors == nil || len(h.secretKey) == 0 {
		writeProblem(w, http.StatusServiceUnavailable, "no_connectors", "连接器未启用（需配置 SECRET_MASTER_KEY）")
		return false
	}
	return true
}

func (h *handlers) triggersReady(w http.ResponseWriter) bool {
	if h.triggers == nil || len(h.secretKey) == 0 {
		writeProblem(w, http.StatusServiceUnavailable, "no_connectors", "连接器未启用（需配置 SECRET_MASTER_KEY）")
		return false
	}
	return true
}

// —— 连接器 CRUD ——

func (h *handlers) listConnectors(w http.ResponseWriter, r *http.Request) {
	if !h.connectorsReady(w) {
		return
	}
	items, err := h.connectors.ListByOwner(r.Context(), ownerOf(r))
	if err != nil {
		h.log.Error("list connectors failed", "err", err)
		writeProblem(w, http.StatusInternalServerError, "internal", "查询失败")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"connectors": items})
}

type createConnectorRequest struct {
	Kind          string `json:"kind"`
	PAT           string `json:"pat"`
	PollIntervalS int    `json:"pollIntervalS"`
}

func (h *handlers) createConnector(w http.ResponseWriter, r *http.Request) {
	if !h.connectorsReady(w) {
		return
	}
	var body createConnectorRequest
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeProblem(w, http.StatusBadRequest, "bad_request", "参数错误")
		return
	}
	kind := body.Kind
	if kind == "" {
		kind = "github"
	}
	if kind != "github" {
		writeProblem(w, http.StatusBadRequest, "bad_request", "暂仅支持 kind=github")
		return
	}
	pat := strings.TrimSpace(body.PAT)
	if pat == "" || len(pat) > maxPatBytes {
		writeProblem(w, http.StatusBadRequest, "bad_request", "pat 必填且长度合法")
		return
	}
	if body.PollIntervalS < minPollIntervalS {
		writeProblem(w, http.StatusBadRequest, "bad_request", "轮询间隔最短 60 秒")
		return
	}
	owner := ownerOf(r)
	// cap：查询失败保守拒绝（同 schedules 评审#12——否则 list error 时上限形同虚设）。
	existing, err := h.connectors.ListByOwner(r.Context(), owner)
	if err != nil {
		h.log.Error("list connectors for cap failed", "err", err)
		writeProblem(w, http.StatusInternalServerError, "internal", "创建失败")
		return
	}
	if len(existing) >= maxConnectorsPerOwner {
		writeProblem(w, http.StatusBadRequest, "too_many", "连接器过多，请先清理")
		return
	}
	// PAT 明文在此 Seal 成密文即弃：绝不落明文列/日志。
	ciphertext, err := secret.Seal(h.secretKey, []byte(pat))
	if err != nil {
		h.log.Error("seal pat failed", "err", err)
		writeProblem(w, http.StatusInternalServerError, "internal", "创建失败")
		return
	}
	id := uuid.NewString()
	c := store.Connector{
		ConnectorID: id, OwnerID: owner, Kind: kind,
		TokenCiphertext: ciphertext, PollIntervalS: body.PollIntervalS, Enabled: true,
	}
	if err := h.connectors.Create(r.Context(), c); err != nil {
		h.log.Error("create connector failed", "err", err)
		writeProblem(w, http.StatusInternalServerError, "internal", "创建失败")
		return
	}
	writeJSON(w, http.StatusOK, c) // 密文/owner 字段 json:"-"，PAT 绝不回传
}

func (h *handlers) deleteConnector(w http.ResponseWriter, r *http.Request) {
	if !h.connectorsReady(w) {
		return
	}
	err := h.connectors.Delete(r.Context(), ownerOf(r), chi.URLParam(r, "connectorID"))
	if errors.Is(err, store.ErrRunNotFound) {
		writeProblem(w, http.StatusNotFound, "not_found", "连接器不存在")
		return
	}
	if err != nil {
		writeProblem(w, http.StatusInternalServerError, "internal", "删除失败")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

type toggleRequest struct {
	Enabled bool `json:"enabled"`
}

func (h *handlers) toggleConnector(w http.ResponseWriter, r *http.Request) {
	if !h.connectorsReady(w) {
		return
	}
	var body toggleRequest
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeProblem(w, http.StatusBadRequest, "bad_request", "参数错误")
		return
	}
	err := h.connectors.SetEnabled(r.Context(), ownerOf(r), chi.URLParam(r, "connectorID"), body.Enabled)
	if errors.Is(err, store.ErrRunNotFound) {
		writeProblem(w, http.StatusNotFound, "not_found", "连接器不存在")
		return
	}
	if err != nil {
		writeProblem(w, http.StatusInternalServerError, "internal", "更新失败")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

// —— 触发规则 CRUD ——

func (h *handlers) listTriggers(w http.ResponseWriter, r *http.Request) {
	if !h.triggersReady(w) {
		return
	}
	items, err := h.triggers.ListByOwner(r.Context(), ownerOf(r))
	if err != nil {
		h.log.Error("list triggers failed", "err", err)
		writeProblem(w, http.StatusInternalServerError, "internal", "查询失败")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"triggers": items})
}

type createTriggerRequest struct {
	ConnectorID   string            `json:"connectorId"`
	EventType     string            `json:"eventType"`
	Filter        map[string]string `json:"filter"`
	QueryTemplate string            `json:"queryTemplate"`
	AgentType     string            `json:"agentType"`
	NeedsApproval bool              `json:"needsApproval"`
}

func (h *handlers) createTrigger(w http.ResponseWriter, r *http.Request) {
	if !h.triggersReady(w) {
		return
	}
	var body createTriggerRequest
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeProblem(w, http.StatusBadRequest, "bad_request", "参数错误")
		return
	}
	if strings.TrimSpace(body.ConnectorID) == "" || strings.TrimSpace(body.EventType) == "" {
		writeProblem(w, http.StatusBadRequest, "bad_request", "connectorId 与 eventType 必填")
		return
	}
	qt := strings.TrimSpace(body.QueryTemplate)
	if qt == "" || len(qt) > maxQueryTemplateBytes {
		writeProblem(w, http.StatusBadRequest, "bad_request", "queryTemplate 必填且长度合法")
		return
	}
	owner := ownerOf(r)
	// connectorId 归属校验：只能对自己的连接器建规则（防挂到他人连接器）。
	if h.connectors != nil {
		conns, err := h.connectors.ListByOwner(r.Context(), owner)
		if err != nil {
			writeProblem(w, http.StatusInternalServerError, "internal", "创建失败")
			return
		}
		owns := false
		for _, c := range conns {
			if c.ConnectorID == body.ConnectorID {
				owns = true
				break
			}
		}
		if !owns {
			writeProblem(w, http.StatusBadRequest, "bad_request", "connectorId 不存在或非本人")
			return
		}
	}
	// cap。
	existing, err := h.triggers.ListByOwner(r.Context(), owner)
	if err != nil {
		writeProblem(w, http.StatusInternalServerError, "internal", "创建失败")
		return
	}
	if len(existing) >= maxTriggersPerOwner {
		writeProblem(w, http.StatusBadRequest, "too_many", "触发规则过多，请先清理")
		return
	}
	agentType := body.AgentType
	switch agentType {
	case "plan_solve", "deep_research":
	default:
		agentType = "react"
	}
	var filter json.RawMessage
	if len(body.Filter) > 0 {
		b, _ := json.Marshal(body.Filter)
		filter = b
	}
	t := store.Trigger{
		TriggerID: uuid.NewString(), OwnerID: owner, ConnectorID: body.ConnectorID,
		EventType: body.EventType, Filter: filter, QueryTemplate: qt,
		AgentType: agentType, NeedsApproval: body.NeedsApproval, Enabled: true,
	}
	if err := h.triggers.Create(r.Context(), t); err != nil {
		h.log.Error("create trigger failed", "err", err)
		writeProblem(w, http.StatusInternalServerError, "internal", "创建失败")
		return
	}
	writeJSON(w, http.StatusOK, t)
}

func (h *handlers) deleteTrigger(w http.ResponseWriter, r *http.Request) {
	if !h.triggersReady(w) {
		return
	}
	err := h.triggers.Delete(r.Context(), ownerOf(r), chi.URLParam(r, "triggerID"))
	if errors.Is(err, store.ErrRunNotFound) {
		writeProblem(w, http.StatusNotFound, "not_found", "触发规则不存在")
		return
	}
	if err != nil {
		writeProblem(w, http.StatusInternalServerError, "internal", "删除失败")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (h *handlers) toggleTrigger(w http.ResponseWriter, r *http.Request) {
	if !h.triggersReady(w) {
		return
	}
	var body toggleRequest
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeProblem(w, http.StatusBadRequest, "bad_request", "参数错误")
		return
	}
	err := h.triggers.SetEnabled(r.Context(), ownerOf(r), chi.URLParam(r, "triggerID"), body.Enabled)
	if errors.Is(err, store.ErrRunNotFound) {
		writeProblem(w, http.StatusNotFound, "not_found", "触发规则不存在")
		return
	}
	if err != nil {
		writeProblem(w, http.StatusInternalServerError, "internal", "更新失败")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}
