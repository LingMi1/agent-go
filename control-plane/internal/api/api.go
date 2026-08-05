// Package api 暴露控制面的 HTTP/SSE 入口：发起 run（流式）、按事件回放历史、健康检查。
package api

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/google/uuid"

	"my-agent/control-plane/internal/artifact"
	"my-agent/control-plane/internal/cognition"
	"my-agent/control-plane/internal/config"
	"my-agent/control-plane/internal/dispatch"
	"my-agent/control-plane/internal/event"
	"my-agent/control-plane/internal/health"
	"my-agent/control-plane/internal/kb"
	"my-agent/control-plane/internal/metrics"
	"my-agent/control-plane/internal/secret"
	"my-agent/control-plane/internal/store"
)

type handlers struct {
	dispatcher     *dispatch.Dispatcher
	runs           store.RunRepository
	sessions       store.SessionRepository
	events         store.EventRepository
	artifacts      artifact.Store
	healthChecks   map[string]health.Check
	kb             kb.Store
	cog            cognition.Client
	stats          store.StatsRepository
	artifactList   store.ArtifactListRepository
	schedules      store.SchedulesRepository
	users          store.UserRepository         // D3：账号（注册/登录/admin 列表）；nil → /auth·/admin 降级
	authTokens     store.SessionTokenRepository // D3：server 端 token；nil → resolveIdentity 直接回退 X-User-Id
	authRequired   bool                         // D3：AUTH_REQUIRED；true 时受保护 API 无有效 token → 401
	connectors     store.ConnectorRepository    // D2 连接器；nil 或 secretKey 空 → /connectors·/triggers 降级 503
	triggers       store.TriggerRepository      // D2 触发规则
	secretKey      []byte                       // D2：SECRET_MASTER_KEY 解码后的 AES-GCM 主密钥；空 → 连接器功能降级
	runTimeout     time.Duration
	maxUploadBytes int64
	log            *slog.Logger
	mux            *chi.Mux // 本路由自身引用；resolveIdentity 反向白名单用它判定「是否已注册的 API 路由」
}

// NewRouter 装配路由与中间件。artifacts 可为 nil（仅 /artifacts 不可用）；
// sessions 可为 nil（仅 /sessions 不可用）；healthChecks 可为 nil（/healthz 退化为「进程存活即 200」）；
// webDir 非空时经 NotFound 托管前端静态资源 + SPA 回退（已注册 API 路由优先匹配，零冲突）。
// D3（docs/17）：新增 users/authTokens 两个 repo 参数——测试传 nil 则 /auth 端点 503 降级、
// resolveIdentity 直接回退 X-User-Id（既有测试零回归）。AUTH_REQUIRED 从 env 读取（同 MAX_UPLOAD_BYTES）。
// D2（docs/16）：新增 connectors/triggers 两个 repo 参数——测试传 nil 则 /connectors·/triggers 端点 503。
// SECRET_MASTER_KEY 从 env 读取（同上就地读横切开关先例）：空或非法则 secretKey 为空，
// 连接器功能整体降级 503（PAT 无从加密），既有测试零回归。
func NewRouter(d *dispatch.Dispatcher, runs store.RunRepository, sessions store.SessionRepository, events store.EventRepository, artifacts artifact.Store, healthChecks map[string]health.Check, kbStore kb.Store, cog cognition.Client, stats store.StatsRepository, artifactList store.ArtifactListRepository, schedules store.SchedulesRepository, users store.UserRepository, authTokens store.SessionTokenRepository, connectors store.ConnectorRepository, triggers store.TriggerRepository, runTimeout time.Duration, webDir string, log *slog.Logger) http.Handler {
	h := &handlers{
		dispatcher: d, runs: runs, sessions: sessions, events: events, artifacts: artifacts,
		healthChecks: healthChecks, kb: kbStore, cog: cog, stats: stats, artifactList: artifactList, schedules: schedules,
		users: users, authTokens: authTokens, authRequired: config.EnvBool("AUTH_REQUIRED"),
		connectors: connectors, triggers: triggers, runTimeout: runTimeout,
		maxUploadBytes: DefaultMaxUploadBytes, log: log,
	}
	if key, err := secret.DecodeMasterKey(os.Getenv("SECRET_MASTER_KEY")); err == nil {
		h.secretKey = key // 空/合法 → key（nil 或 32 字节）；非法（长度错/坏 base64）→ 保持 nil（降级）
	} else if log != nil {
		log.Warn("SECRET_MASTER_KEY invalid; connectors disabled", "err", err)
	}
	if v := os.Getenv("MAX_UPLOAD_BYTES"); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil && n > 0 {
			h.maxUploadBytes = n
		}
	}
	r := chi.NewRouter()
	h.mux = r // 供 resolveIdentity 反向白名单在请求时查路由树（构建完成后只读，并发安全）
	r.Use(middleware.RequestID)
	// 请求指标中间件挂在 Recoverer 外层：handler panic 时内层 Recoverer 写的 500 经
	// 指标中间件的 WrapResponseWriter 记为 status="500"——若挂内层，panic 会穿过计数
	// 代码，最严重的一类错误（panic 500）恰好对「流量与错误率」全盲。
	// docs/11 §3.3 红线不变：内部用 chi WrapResponseWriter 透传 Flusher，SSE 不受影响。
	r.Use(metrics.HTTPMiddleware)
	r.Use(middleware.Recoverer)
	// D3 身份解析（docs/17 §3.2）：读 Bearer token → context 存 (userID,role)；挂在
	// metrics/Recoverer 之后，故其 401 也被指标计数、panic 也被兜住。AUTH_REQUIRED 默认关时
	// 无 token 静默放行（ownerOf 回退 X-User-Id）——既有链路零行为变化。
	r.Use(h.resolveIdentity)
	r.Get("/healthz", h.healthz)
	r.Handle("/metrics", metrics.Handler()) // 与 /healthz 同级：只读、无副作用（docs/11 §3.1）
	r.Post("/runs", h.startRun)
	r.Get("/runs/{runID}/events", h.replay)
	r.Post("/runs/{runID}/approvals", h.resolveApproval) // M11 HITL：决议→恢复 run（SSE）
	r.Get("/sessions", h.listSessions)
	r.Delete("/sessions/{sessionID}", h.deleteSession)
	r.Get("/sessions/{sessionID}/runs", h.listSessionRuns)
	r.Post("/sessions/{sessionID}/fork", h.forkSession) // docs/14 会话分叉（时间旅行）
	r.Post("/uploads", h.upload)
	// UX-1 Files 面板：用户知识库管理（读/删直连 Qdrant；上传入库走认知面 gRPC）。
	r.Get("/kb/docs", h.listKbDocs)
	r.Post("/kb/docs", h.ingestKbDoc)
	r.Delete("/kb/docs", h.deleteKbDoc)
	r.Get("/stats/usage", h.usageStats) // M11 成本面板（owner 域聚合，纯读）
	// M11 定时任务（Proactive）：owner 域 CRUD；触发由 internal/scheduler。
	r.Get("/schedules", h.listSchedules)
	r.Post("/schedules", h.createSchedule)
	r.Delete("/schedules/{scheduleID}", h.deleteSchedule)
	r.Post("/schedules/{scheduleID}/toggle", h.toggleSchedule)
	// D2 Proactive 连接器（docs/16）：owner 域 CRUD；轮询由 internal/poller。
	// nil 仓库或 SECRET_MASTER_KEY 空 → 全部 503 降级（h.connectorsReady/triggersReady）。
	r.Get("/connectors", h.listConnectors)
	r.Post("/connectors", h.createConnector)
	r.Delete("/connectors/{connectorID}", h.deleteConnector)
	r.Post("/connectors/{connectorID}/toggle", h.toggleConnector)
	r.Get("/triggers", h.listTriggers)
	r.Post("/triggers", h.createTrigger)
	r.Delete("/triggers/{triggerID}", h.deleteTrigger)
	r.Post("/triggers/{triggerID}/toggle", h.toggleTrigger)
	r.Get("/artifacts", h.listArtifacts) // 跨会话产物画廊（owner 域）——与下面的对象代理精确路由共存
	r.Get("/artifacts/*", h.artifact)
	// D3 鉴权端点（docs/17 §3.3）：注册/登录/登出/我是谁。AUTH_REQUIRED 下 /auth/* 免 token。
	r.Post("/auth/register", h.register)
	r.Post("/auth/login", h.login)
	r.Post("/auth/logout", h.logout)
	r.Get("/auth/me", h.me)
	// D3 管理后台：requireAdmin 门控（role!=admin → 403）；跨 owner 读用 store 新方法。
	r.Group(func(ar chi.Router) {
		ar.Use(h.requireAdmin)
		ar.Get("/admin/users", h.adminListUsers)
		ar.Patch("/admin/users/{id}/role", h.adminSetRole)
		ar.Get("/admin/runs", h.adminListRuns)
		ar.Get("/admin/stats", h.adminStats)
	})
	if webDir != "" {
		r.NotFound(spaHandler(webDir))
	}
	return r
}

// healthz 并发探测已注入的依赖（PG / 认知面），聚合成单一判定。
func (h *handlers) healthz(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
	defer cancel()
	rep := health.RunChecks(ctx, h.healthChecks)
	writeJSON(w, rep.HTTPStatus, map[string]any{"healthy": rep.Healthy, "checks": rep.Body})
}

type attachmentRef struct {
	ResourceKey string `json:"resourceKey"`
	FileName    string `json:"fileName"`
	MimeType    string `json:"mimeType"`
	Size        int64  `json:"size"`
	// 上传响应（uploadFile）的两个 URL 字段：前端把 /uploads 返回体原样塞进 run 请求，
	// 这里显式声明才能在「原样 marshal 落库 → 回放返还」时不丢（会话轮附件持久化）。
	PreviewURL  string `json:"previewUrl,omitempty"`
	DownloadURL string `json:"downloadUrl,omitempty"`
}

type startRunRequest struct {
	Query        string          `json:"query"`
	SessionID    string          `json:"sessionId"`
	AgentType    string          `json:"agentType"`    // "react"(默认) | "plan_solve" | "deep_research"
	OutputFormat string          `json:"outputFormat"` // M9：html/docs/ppt/table（空=自由格式）
	ImageGen     bool            `json:"imageGen"`     // 生图开关：置位则注入生图指令
	Attachments  []attachmentRef `json:"attachments"`
}

func (h *handlers) startRun(w http.ResponseWriter, r *http.Request) {
	var body startRunRequest
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.Query == "" {
		writeProblem(w, http.StatusBadRequest, "bad_request", "query 必填")
		return
	}
	sessionID := body.SessionID
	if sessionID == "" {
		sessionID = uuid.NewString()
	}
	agentType := body.AgentType
	switch agentType {
	case "plan_solve", "deep_research": // 三档模式：快速(react)/深度思考/深度研究
	default:
		agentType = "react"
	}
	ownerID := ownerOf(r)
	// 附件 key 防伪造闸门：只接受当前用户自己的 uploads 对象——否则任何人可把
	// 他人 upload key 塞进 attachments，让认知面替他读取内容/写进知识库。
	atts := make([]dispatch.Attachment, 0, len(body.Attachments))
	for _, a := range body.Attachments {
		if !ValidateAttachmentKey(ownerID, a.ResourceKey) {
			writeProblem(w, http.StatusForbidden, "forbidden", "附件不属于当前用户："+a.ResourceKey)
			return
		}
		atts = append(atts, dispatch.Attachment{
			ResourceKey: a.ResourceKey, FileName: a.FileName, MimeType: a.MimeType, Size: a.Size,
		})
	}
	// 会话轮附件持久化：请求的 attachments 数组原样 marshal 落进 runs.attachments，
	// GET /sessions/{id}/runs 原样返还（回放还原附件 chips/上传内容段）。无附件不落（NULL）。
	var attsJSON []byte
	if len(body.Attachments) > 0 {
		b, err := json.Marshal(body.Attachments)
		if err != nil { // attachmentRef 全平铺字段，实际不可达；防御性 500 而非静默丢账
			writeProblem(w, http.StatusInternalServerError, "internal", err.Error())
			return
		}
		attsJSON = b
	}

	// docs/14 分叉播种触发：请求的会话是分叉登记的 → **每个 run 都附 fork 两键**，
	// 幂等由认知面「目标 thread 已有 checkpoint 即跳过」闸兜底（已播种后附键是无害
	// no-op）。曾用「own run 数==0 才附键」做第一道闸，但 run 行在播种前就已落库：
	// 首条消息任何失败（认知面不可用/定位失败/瞬时 DB 错）都会永久关死播种，会话
	// 静默降级成空记忆——恰是 docs/14 §2 明令禁止的最坏结果。恒附键让播种失败的
	// 下一轮自动重试自愈，定位恒久失败则每轮诚实报错。
	forkFromSessionID, forkFromRunID := "", ""
	if body.SessionID != "" && h.sessions != nil {
		fork, err := h.sessions.GetFork(r.Context(), ownerID, sessionID)
		switch {
		case err == nil:
			// 播种源必须是锚点 run **实际执行所在的会话**（runs 表该行的 session_id，
			// 即其 checkpoint 所在 thread——继承投影从不复制 run）：从继承轮再分叉
			// （分叉的分叉）时锚点属更远祖先，直接用 fork.ParentSessionID 会让认知面
			// 在错误 thread 里定位、播种必然失败。锚点 run 已删（父会话被删）则回退
			// 直接父——键仍附上，认知面按既有路径幂等跳过或诚实报错，绝不静默失忆。
			forkFromSessionID, forkFromRunID = fork.ParentSessionID, fork.ForkAfterRunID
			if h.runs != nil {
				anchor, aerr := h.runs.GetRun(r.Context(), fork.ForkAfterRunID)
				switch {
				case aerr == nil:
					forkFromSessionID = anchor.SessionID
				case errors.Is(aerr, store.ErrRunNotFound):
					// 保持回退值，交认知面处理。
				default:
					writeProblem(w, http.StatusInternalServerError, "internal", aerr.Error())
					return
				}
			}
		case errors.Is(err, store.ErrForkNotFound):
			// 非分叉会话：正常起跑。
		default:
			// 瞬时 DB 错误不得静默吞掉：无键 run 会在新 thread 落 checkpoint，认知面
			// 幂等闸从此永久拦死补种。宁失败勿降级。
			writeProblem(w, http.StatusInternalServerError, "internal", err.Error())
			return
		}
	}

	h.streamRun(w, r, dispatch.StartCommand{
		SessionID: sessionID, OwnerID: ownerID, Query: body.Query, AgentType: agentType, OutputFormat: body.OutputFormat, ImageGen: body.ImageGen,
		ForkFromSessionID: forkFromSessionID, ForkFromRunID: forkFromRunID,
		Attachments: atts, AttachmentsJSON: attsJSON,
	})
}

type forkSessionRequest struct {
	AfterRunID string `json:"afterRunId"`
}

// forkSession：POST /sessions/{sessionID}/fork —— 从某轮之后分叉出新会话（docs/14）。
// 校验链：afterRunId 必填 → 属于该会话 timeline（own 或继承轮皆可为锚，覆盖"分叉的
// 分叉"从继承轮再分）→ owner 匹配 → 已终态（RUNNING 快照未收口，409）。通过则登记
// session_forks 并返回新 sessionId；播种发生在新会话第一条 run（见 startRun）。
func (h *handlers) forkSession(w http.ResponseWriter, r *http.Request) {
	if h.sessions == nil {
		writeProblem(w, http.StatusServiceUnavailable, "no_session_store", "会话存储未配置")
		return
	}
	sessionID := chi.URLParam(r, "sessionID")
	var body forkSessionRequest
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.AfterRunID == "" {
		writeProblem(w, http.StatusBadRequest, "bad_request", "afterRunId 必填")
		return
	}
	ownerID := ownerOf(r)
	// timeline 成员校验一举三得：会话存在性、owner 隔离（SQL 内过滤，空=404 不泄露
	// 存在性）、锚点∈timeline（含继承轮）。
	runs, err := h.sessions.ListRunsBySession(r.Context(), ownerID, sessionID)
	if err != nil {
		writeProblem(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	var anchor *store.Run
	for i := range runs {
		if runs[i].RunID == body.AfterRunID {
			anchor = &runs[i]
			break
		}
	}
	if len(runs) == 0 || anchor == nil {
		writeProblem(w, http.StatusNotFound, "not_found", "run 不属于该会话或会话不存在")
		return
	}
	if anchor.Status == store.StatusRunning {
		// 快照未收口：该轮的 checkpoint 链还在演化，锚不住"这一轮结束时"的状态。
		writeProblem(w, http.StatusConflict, "run_not_finished", "该轮仍在运行，结束后才能分叉")
		return
	}
	newID := uuid.NewString()
	if err := h.sessions.CreateFork(r.Context(), store.SessionFork{
		SessionID: newID, ParentSessionID: sessionID, ForkAfterRunID: body.AfterRunID, OwnerID: ownerID,
	}); err != nil {
		h.log.Error("create fork failed", "err", err)
		writeProblem(w, http.StatusInternalServerError, "internal", "分叉登记失败")
		return
	}
	writeJSON(w, http.StatusCreated, map[string]string{"sessionId": newID})
}

// streamRun：准入 → SSE 头 + X-Run-Id → 以 SSE 流承载一次 run（startRun 与审批恢复共用）。
// cmd.RunID 留空则生成；准入必须在写任何响应头之前，满载干净回 429。
func (h *handlers) streamRun(w http.ResponseWriter, r *http.Request, cmd dispatch.StartCommand) {
	release, ok := h.dispatcher.Admit()
	if !ok {
		writeProblem(w, http.StatusTooManyRequests, "busy", "系统繁忙，请稍后重试")
		return
	}
	defer release()

	if cmd.RunID == "" {
		cmd.RunID = uuid.NewString()
	}
	writeSSEHeaders(w)
	w.Header().Set("X-Run-Id", cmd.RunID)
	w.WriteHeader(http.StatusOK)
	sink, err := newSSESink(w)
	if err != nil {
		h.log.Error("sse sink", "err", err)
		return
	}

	runCtx, cancel := context.WithTimeout(r.Context(), h.runTimeout)
	defer cancel()

	if err := h.dispatcher.Run(runCtx, cmd, sink); err != nil {
		h.log.Error("run failed", "runID", cmd.RunID, "err", err)
	}
}

type resolveApprovalRequest struct {
	ApprovalID string `json:"approvalId"`
	Approved   bool   `json:"approved"`
	Comment    string `json:"comment"`
}

// resolveApproval：POST /runs/{runID}/approvals —— 审批=run 边界的恢复端。
// 归属校验循 replay 模式；响应即新 run 的 SSE 流（与 POST /runs 同构，X-Run-Id 可取）。
// 决议乘 metadata 走既有 Run RPC；认知面校验 pending interrupt 匹配（伪造/过期→优雅收尾）。
func (h *handlers) resolveApproval(w http.ResponseWriter, r *http.Request) {
	runID := chi.URLParam(r, "runID")
	run, err := h.runs.GetRun(r.Context(), runID)
	if errors.Is(err, store.ErrRunNotFound) {
		writeProblem(w, http.StatusNotFound, "not_found", "run 不存在")
		return
	}
	if err != nil {
		writeProblem(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	if run.OwnerID != ownerOf(r) {
		writeProblem(w, http.StatusForbidden, "forbidden", "无权访问该 run")
		return
	}
	var body resolveApprovalRequest
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.ApprovalID == "" {
		writeProblem(w, http.StatusBadRequest, "bad_request", "approvalId 必填")
		return
	}
	verdict, decision := "通过", "approved"
	if !body.Approved {
		verdict, decision = "拒绝", "rejected"
	}
	query := "[审批] " + verdict
	if body.Comment != "" {
		query += "：" + body.Comment
	}
	h.streamRun(w, r, dispatch.StartCommand{
		SessionID: run.SessionID, OwnerID: run.OwnerID, Query: query, AgentType: "react",
		ApprovalResumeID: body.ApprovalID, ApprovalDecision: decision, ApprovalComment: body.Comment,
	})
}

func (h *handlers) replay(w http.ResponseWriter, r *http.Request) {
	runID := chi.URLParam(r, "runID")
	run, err := h.runs.GetRun(r.Context(), runID)
	if errors.Is(err, store.ErrRunNotFound) {
		writeProblem(w, http.StatusNotFound, "not_found", "run 不存在")
		return
	}
	if err != nil {
		writeProblem(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	if run.OwnerID != ownerOf(r) {
		writeProblem(w, http.StatusForbidden, "forbidden", "无权访问该 run")
		return
	}
	envelopes, err := h.events.ListByRun(r.Context(), runID)
	if err != nil {
		writeProblem(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	// 自证不变量：seq 无空洞、finish 仅 result 且至多一条。违反只告警、不阻断回放（可能是崩溃残留）。
	if verr := event.ValidateSequence(envelopes); verr != nil {
		h.log.Warn("replay sequence invariant violated", "runID", runID, "err", verr)
	}

	writeSSEHeaders(w)
	w.WriteHeader(http.StatusOK)
	sink, err := newSSESink(w)
	if err != nil {
		return
	}
	// 用与实时完全相同的编码器逐条重发 → 重放帧与实时逐字段一致。
	for _, e := range envelopes {
		if err := sink.WriteFrame(e); err != nil {
			return
		}
	}
}

// artifact 代理产物下载。两类 key、两种鉴权：
//   - uploads/{owner}/…（M8 上传对象）：owner 前置于 key，比对第二段即可，免查库
//     ——必须在 runID 反查之前特判（其首段 "uploads" 不是 runID）。
//   - {runId}/{toolCallId}/{file}（运行产物）：用首段 runId 反查 run 校验 owner。
func (h *handlers) artifact(w http.ResponseWriter, r *http.Request) {
	key := chi.URLParam(r, "*")
	if key == "" {
		writeProblem(w, http.StatusNotFound, "not_found", "缺少产物 key")
		return
	}
	if strings.HasPrefix(key, "uploads/") {
		if OwnerOfUploadKey(key) != ownerOf(r) {
			writeProblem(w, http.StatusForbidden, "forbidden", "无权访问该产物")
			return
		}
		h.serveObject(w, r, key)
		return
	}
	runID := key
	if i := strings.IndexByte(key, '/'); i > 0 {
		runID = key[:i]
	}
	run, err := h.runs.GetRun(r.Context(), runID)
	if errors.Is(err, store.ErrRunNotFound) {
		writeProblem(w, http.StatusNotFound, "not_found", "run 不存在")
		return
	}
	if err != nil {
		writeProblem(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	if run.OwnerID != ownerOf(r) {
		writeProblem(w, http.StatusForbidden, "forbidden", "无权访问该产物")
		return
	}
	h.serveObject(w, r, key)
}

// serveObject 鉴权通过后的公共回传路径（uploads 与运行产物共用）。
func (h *handlers) serveObject(w http.ResponseWriter, r *http.Request, key string) {
	if h.artifacts == nil {
		writeProblem(w, http.StatusServiceUnavailable, "no_artifact_store", "产物存储未配置")
		return
	}
	obj, err := h.artifacts.Open(r.Context(), key)
	if errors.Is(err, artifact.ErrNotFound) {
		writeProblem(w, http.StatusNotFound, "not_found", "产物不存在")
		return
	}
	if err != nil {
		writeProblem(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	defer obj.Body.Close()
	w.Header().Set("Content-Type", obj.ContentType)
	if obj.Size > 0 {
		w.Header().Set("Content-Length", strconv.FormatInt(obj.Size, 10))
	}
	w.WriteHeader(http.StatusOK)
	_, _ = io.Copy(w, obj.Body)
}

// —— M7 会话端点（只读投影，proto/SSE 零改动；设计与取舍见 docs/08 §2） ——

// sessionSummaryJSON 是 GET /sessions 的响应行（camelCase，与 SSE 帧字段风格一致）。
type sessionSummaryJSON struct {
	SessionID    string    `json:"sessionId"`
	Title        string    `json:"title"`
	EntryAgent   string    `json:"entryAgent"`
	RunCount     int       `json:"runCount"`
	CreatedAt    time.Time `json:"createdAt"`
	LastActiveAt time.Time `json:"lastActiveAt"`
	ForkedFrom   string    `json:"forkedFrom,omitempty"` // docs/14：父会话 id（非分叉为空缺省不序列化）
}

// listSessions 返回调用方的会话列表：runs 表按 session_id 聚合，lastActiveAt 降序。
func (h *handlers) listSessions(w http.ResponseWriter, r *http.Request) {
	if h.sessions == nil {
		writeProblem(w, http.StatusServiceUnavailable, "no_session_store", "会话存储未配置")
		return
	}
	limit := 0
	if v := r.URL.Query().Get("limit"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 0 {
			writeProblem(w, http.StatusBadRequest, "bad_request", "limit 必须是非负整数")
			return
		}
		limit = n
	}
	list, err := h.sessions.ListSessions(r.Context(), ownerOf(r), limit)
	if err != nil {
		writeProblem(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	out := make([]sessionSummaryJSON, 0, len(list))
	for _, s := range list {
		out = append(out, sessionSummaryJSON{
			SessionID: s.SessionID, Title: s.Title, EntryAgent: s.EntryAgent,
			RunCount: s.RunCount, CreatedAt: s.CreatedAt, LastActiveAt: s.LastActiveAt,
			ForkedFrom: s.ForkedFrom,
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"sessions": out})
}

// deleteSession：DELETE /sessions/{id} —— 删除本 owner 的整段会话（runs+events）。
// 删 0 行=会话不存在/非本人 → 404（不泄露他人会话存在性，同 ListRunsBySession 姿态）。
func (h *handlers) deleteSession(w http.ResponseWriter, r *http.Request) {
	if h.sessions == nil {
		writeProblem(w, http.StatusServiceUnavailable, "no_session_store", "会话存储未配置")
		return
	}
	sessionID := chi.URLParam(r, "sessionID")
	n, err := h.sessions.DeleteSession(r.Context(), ownerOf(r), sessionID)
	if err != nil {
		h.log.Error("delete session failed", "err", err)
		writeProblem(w, http.StatusInternalServerError, "internal", "会话删除失败")
		return
	}
	if n == 0 {
		writeProblem(w, http.StatusNotFound, "not_found", "会话不存在")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"deletedRuns": n})
}

// sessionRunJSON 是 GET /sessions/{id}/runs 的响应行（run 元数据，不内嵌事件——
// 前端对每个 run 复用 GET /runs/{runID}/events 回放，保持「回放==实时」单点维护）。
type sessionRunJSON struct {
	RunID        string    `json:"runId"`
	Query        string    `json:"query"`
	AgentType    string    `json:"agentType"`
	Status       string    `json:"status"`
	FinalSummary string    `json:"finalSummary,omitempty"`
	ErrorMsg     string    `json:"errorMsg,omitempty"`
	CreatedAt    time.Time `json:"createdAt"`
	Inherited    bool      `json:"inherited,omitempty"` // docs/14：继承自祖先会话的只读投影轮
	// 本轮请求附带的附件引用数组（AttachmentRef JSON 原样返还；无附件省略）——
	// 前端回放轮据此还原用户气泡附件 chips 与工作区「上传内容」段。
	Attachments json.RawMessage `json:"attachments,omitempty"`
}

// listSessionRuns 返回会话内 run 元数据（created_at 升序）。owner 过滤在 SQL 里，
// 他人会话与不存在的会话同样返回空 → 404，不泄露存在性。fork 打破了 M7 的
// 「空结果==会话不存在」等价：0-own-run 的分叉会话在父会话删除后 timeline 为空，
// 但 ListSessions（forks 表驱动）仍列出它——存在性判定须与列表一致，否则侧栏可见、
// 打开 404。故空结果再查 fork 登记：命中 → 200 + 空数组（owner 过滤在 SQL 内，无
// 存在性泄露），未命中才 404。
func (h *handlers) listSessionRuns(w http.ResponseWriter, r *http.Request) {
	if h.sessions == nil {
		writeProblem(w, http.StatusServiceUnavailable, "no_session_store", "会话存储未配置")
		return
	}
	sessionID := chi.URLParam(r, "sessionID")
	runs, err := h.sessions.ListRunsBySession(r.Context(), ownerOf(r), sessionID)
	if err != nil {
		writeProblem(w, http.StatusInternalServerError, "internal", err.Error())
		return
	}
	if len(runs) == 0 {
		if _, ferr := h.sessions.GetFork(r.Context(), ownerOf(r), sessionID); ferr == nil {
			writeJSON(w, http.StatusOK, map[string]any{"sessionId": sessionID, "runs": []sessionRunJSON{}})
			return
		} else if !errors.Is(ferr, store.ErrForkNotFound) {
			writeProblem(w, http.StatusInternalServerError, "internal", ferr.Error())
			return
		}
		writeProblem(w, http.StatusNotFound, "not_found", "会话不存在")
		return
	}
	out := make([]sessionRunJSON, 0, len(runs))
	for _, run := range runs {
		j := sessionRunJSON{
			RunID: run.RunID, Query: run.QueryText, AgentType: run.EntryAgent,
			Status: run.Status, CreatedAt: run.CreatedAt, Inherited: run.Inherited,
		}
		if len(run.Attachments) > 0 {
			j.Attachments = json.RawMessage(run.Attachments)
		}
		if run.FinalSummaryText != nil {
			j.FinalSummary = *run.FinalSummaryText
		}
		if run.ErrorMsg != nil {
			j.ErrorMsg = *run.ErrorMsg
		}
		out = append(out, j)
	}
	writeJSON(w, http.StatusOK, map[string]any{"sessionId": sessionID, "runs": out})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

// ownerOf 解析调用方身份（docs/17 §3.2 身份解析单点）：**先读 resolveIdentity 放进
// context 的 token 身份**，没有再回退 X-User-Id 头（默认 anonymous）。全控制面唯一身份解析点，
// 20 处调用零改——AUTH_REQUIRED 默认关时 token 缺失即走老路径（X-User-Id），既有测试零回归。
func ownerOf(r *http.Request) string {
	if id, ok := identityFrom(r); ok && id.userID != "" {
		return id.userID
	}
	if v := r.Header.Get("X-User-Id"); v != "" {
		return v
	}
	return "anonymous"
}

func writeProblem(w http.ResponseWriter, status int, code, msg string) {
	if status == http.StatusTooManyRequests {
		w.Header().Set("Retry-After", "1")
	}
	writeJSON(w, status, map[string]string{"code": code, "message": msg})
}

// usageStats：GET /stats/usage?days=30 —— runs 表 owner 域聚合（合计/按天/按模式）。
func (h *handlers) usageStats(w http.ResponseWriter, r *http.Request) {
	if h.stats == nil {
		writeProblem(w, http.StatusServiceUnavailable, "no_stats", "统计未启用")
		return
	}
	days := 30
	if v := r.URL.Query().Get("days"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			days = n
		}
	}
	report, err := h.stats.UsageReport(r.Context(), ownerOf(r), days)
	if err != nil {
		h.log.Error("usage stats failed", "err", err)
		writeProblem(w, http.StatusInternalServerError, "internal", "统计查询失败")
		return
	}
	writeJSON(w, http.StatusOK, report)
}

// listArtifacts：GET /artifacts?limit=100 —— 跨会话产物画廊（owner 域，纯读）。
func (h *handlers) listArtifacts(w http.ResponseWriter, r *http.Request) {
	if h.artifactList == nil {
		writeProblem(w, http.StatusServiceUnavailable, "no_artifact_list", "产物列表未启用")
		return
	}
	limit := 100
	if v := r.URL.Query().Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			limit = n
		}
	}
	// 游标分页（B.11）：?before=<ts_unix_ms>&beforeKey=<resourceKey>（上一页末项）。
	var beforeTS int64
	if v := r.URL.Query().Get("before"); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil {
			beforeTS = n
		}
	}
	beforeKey := r.URL.Query().Get("beforeKey")
	mimePrefix := r.URL.Query().Get("mime") // 如 image/ —— 服务端过滤，防单页客户端过滤漏更旧
	items, err := h.artifactList.ListByOwner(r.Context(), ownerOf(r), limit, beforeTS, beforeKey, mimePrefix)
	if err != nil {
		h.log.Error("list artifacts failed", "err", err)
		writeProblem(w, http.StatusInternalServerError, "internal", "产物列表查询失败")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"artifacts": items})
}
