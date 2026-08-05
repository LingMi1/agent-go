// Package dispatch 负责 run 的准入（有界并发 + 背压）与生命周期编排：
// 准入成功后 createRun → 打开认知流 → 由 stream.Hub 泵送 → finishRun。
package dispatch

import (
	"context"
	"errors"
	"log/slog"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"golang.org/x/sync/semaphore"

	"my-agent/control-plane/internal/cognition"
	"my-agent/control-plane/internal/metrics"
	"my-agent/control-plane/internal/store"
	"my-agent/control-plane/internal/stream"
)

// tracer 是 dispatch 的 OTel tracer（docs/18 §3.1 一 run 一根 span）。
// OTel 未启用时它取全局 no-op provider，Start 返回 no-op span——零导出、零开销。
var tracer = otel.Tracer("my-agent/control-plane/dispatch")

// ErrBusy 表示并发已达上限（背压）。上层应回 429“系统繁忙”。
var ErrBusy = errors.New("dispatch: system busy")

// Attachment 是已上传附件的引用（key 归属已在 api 层校验）。
type Attachment struct {
	ResourceKey string
	FileName    string
	MimeType    string
	Size        int64
}

// StartCommand 是一次 run 的启动入参。
type StartCommand struct {
	RunID        string
	SessionID    string
	OwnerID      string
	OutputFormat string // M9：输出格式（透传认知面 metadata）
	ImageGen     bool   // 生图开关（透传认知面 metadata["image_gen"]）
	// M11 HITL：审批恢复三元组（乘 metadata 走既有 Run RPC；空=普通 run）。
	ApprovalResumeID string
	ApprovalDecision string // "approved" | "rejected"
	ApprovalComment  string
	// docs/14 会话分叉：分叉会话的第一条 run 携带（乘 metadata 走既有 Run RPC；空=普通 run）。
	// 认知面据此把父 thread 分叉点 checkpoint 的 messages 播种进新 thread。
	ForkFromSessionID string
	ForkFromRunID     string
	Query             string
	AgentType         string // "react" | "plan_solve"
	Attachments       []Attachment
	// AttachmentsJSON 是请求体 attachments 数组的原始 JSON（api 层原样 marshal），
	// 只为落库（runs.attachments，供会话回放还原附件 chips/上传内容段）；认知面仍走
	// 上面的结构化 Attachments。headless 路径（scheduler/poller）不设置（nil=不落）。
	AttachmentsJSON []byte
}

// Dispatcher 持有并发闸与运行时协作者。
type Dispatcher struct {
	sem      *semaphore.Weighted
	runs     store.RunRepository
	client   cognition.Client
	hub      *stream.Hub
	maxSteps int32
	log      *slog.Logger
}

func New(maxConcurrent int64, runs store.RunRepository, client cognition.Client, hub *stream.Hub, maxSteps int32, log *slog.Logger) *Dispatcher {
	if maxConcurrent <= 0 {
		maxConcurrent = 16
	}
	return &Dispatcher{
		sem:      semaphore.NewWeighted(maxConcurrent),
		runs:     runs,
		client:   client,
		hub:      hub,
		maxSteps: maxSteps,
		log:      log,
	}
}

// Admit 非阻塞地尝试占用一个并发槽。成功返回释放函数；失败返回 (nil, false)。
// 必须在写任何 SSE 响应头之前调用，以便满载时能干净地回 429。
func (d *Dispatcher) Admit() (release func(), ok bool) {
	if !d.sem.TryAcquire(1) {
		metrics.RunsRejected().Inc() // 429「优雅繁忙」可见化
		return nil, false
	}
	metrics.RunsInFlight().Inc() // semaphore 不暴露占用数，在占用/释放处自计数
	return func() { d.sem.Release(1); metrics.RunsInFlight().Dec() }, true
}

// Run 执行一次“已准入”的 run：建 run → 打开认知流 → 泵送 → 收口。
// ctx 为 run 上下文（含超时、随客户端断开取消）。finishRun 用脱离取消的上下文，
// 保证即便客户端断开也能写回终态。
func (d *Dispatcher) Run(ctx context.Context, cmd StartCommand, sink stream.Sink) error {
	agentType := cmd.AgentType
	if agentType == "" {
		agentType = "react"
	}
	// OTel 根 span（docs/18 §3.1）：run 单收口——HTTP 与定时 headless run 都过此。
	// 带 span 的 ctx 下传 client.RunAgent，otelgrpc stats handler 据此把 traceparent
	// 写进 outgoing metadata，Python 拦截器接力建 server span。未启用时是 no-op span。
	ctx, span := tracer.Start(ctx, "agent.run", trace.WithAttributes(
		attribute.String("run_id", cmd.RunID),
		attribute.String("session_id", cmd.SessionID),
		attribute.String("agent_type", agentType),
	))
	defer span.End()
	// run-scoped 结构化日志：关联键与 Python 认知面一致（run_id/session_id/agent_type），
	// 便于用同一 run_id 跨进程串起 Go↔Python 全链路。启用 OTel 时再补 trace_id（与
	// Python 侧 format(trace_id,'032x') 同为 32 位 hex），可用同一 trace_id grep 两面日志；
	// 未启用时 SpanContext 全零（IsValid=false），不落 trace_id，保持零行为变化。
	var log *slog.Logger
	if d.log != nil {
		fields := []any{"run_id", cmd.RunID, "session_id", cmd.SessionID, "agent_type", agentType}
		if tid := span.SpanContext().TraceID(); tid.IsValid() {
			fields = append(fields, "trace_id", tid.String())
		}
		log = d.log.With(fields...)
	}
	if err := d.runs.CreateRun(ctx, store.CreateRunParams{
		RunID: cmd.RunID, SessionID: cmd.SessionID, OwnerID: cmd.OwnerID, EntryAgent: agentType, QueryText: cmd.Query,
		Attachments: cmd.AttachmentsJSON,
	}); err != nil {
		return err
	}
	if log != nil {
		log.Info("run start")
	}
	start := time.Now() // run 时长计时起点（CreateRun 后，docs/11 §3.2）

	finCtx := context.WithoutCancel(ctx)

	atts := make([]cognition.Attachment, 0, len(cmd.Attachments))
	for _, a := range cmd.Attachments {
		atts = append(atts, cognition.Attachment(a))
	}
	// 会话历史附件聚合（best-effort）：续轮无需用户重传即可按文件名引用此前上传。
	// fake/不支持的 repo 自然降级为无历史；查询失败只记日志，绝不阻断 run。
	var sessionAtts []byte
	if reader, ok := d.runs.(store.SessionAttachmentsReader); ok {
		if b, aerr := reader.SessionAttachmentsJSON(ctx, cmd.SessionID, cmd.OwnerID, cmd.RunID); aerr == nil {
			sessionAtts = b
		} else if log != nil {
			log.Warn("session attachments aggregate failed", "err", aerr)
		}
	}
	st, err := d.client.RunAgent(ctx, cognition.RunRequest{
		RunID: cmd.RunID, SessionID: cmd.SessionID, Query: cmd.Query, AgentType: agentType, OutputFormat: cmd.OutputFormat, ImageGen: cmd.ImageGen,
		ApprovalResumeID: cmd.ApprovalResumeID, ApprovalDecision: cmd.ApprovalDecision, ApprovalComment: cmd.ApprovalComment,
		ForkFromSessionID: cmd.ForkFromSessionID, ForkFromRunID: cmd.ForkFromRunID,
		MaxSteps: d.maxSteps, OwnerID: cmd.OwnerID, Attachments: atts,
		SessionAttachmentsJSON: sessionAtts,
	})
	if err != nil {
		_ = d.runs.FinishRun(finCtx, store.FinishRunParams{RunID: cmd.RunID, Status: store.StatusFailed, ErrorMsg: err.Error()})
		metrics.Runs().WithLabelValues(store.StatusFailed, agentType).Inc()
		metrics.RunDuration().WithLabelValues(agentType).Observe(time.Since(start).Seconds())
		if log != nil {
			log.Error("open run stream failed", "err", err)
		}
		return err
	}

	res := d.hub.Pump(ctx, cmd.RunID, st, sink)
	fp := store.FinishRunParams{
		RunID: cmd.RunID, Status: res.Status, FinalSummaryText: res.Summary, ErrorMsg: res.ErrorMsg,
	}
	if res.Usage != nil {
		fp.InputTokens, fp.OutputTokens, fp.ModelCalls = res.Usage.InputTokens, res.Usage.OutputTokens, res.Usage.ModelCalls
	}
	if ferr := d.runs.FinishRun(finCtx, fp); ferr != nil && log != nil {
		log.Error("finish run failed", "err", ferr)
	}
	// run 生命周期指标收口：HTTP 与定时 headless run 都经此（docs/11 §3.4）。
	metrics.Runs().WithLabelValues(res.Status, agentType).Inc()
	metrics.RunDuration().WithLabelValues(agentType).Observe(time.Since(start).Seconds())
	metrics.RunTokens().WithLabelValues("input").Add(float64(fp.InputTokens))
	metrics.RunTokens().WithLabelValues("output").Add(float64(fp.OutputTokens))
	metrics.ModelCalls().Add(float64(fp.ModelCalls))
	if log != nil {
		log.Info("run finished", "status", res.Status)
	}
	return nil
}
