// Package poller：Proactive 连接器的独立轮询 goroutine（docs/16 §3.4，不扩 scheduler）。
//
// 结构照抄 scheduler.Scheduler 骨架——tick 主循环 + ListDue + 先 Admit 后 Claim +
// 独立 slots 信号量（不吃交互准入配额）——但每到期 connector 语义多三步：
// 解密 PAT → 拉外部 API(Poll) → Normalize → 匹配 triggers → 命中渲染 query_template
// → dispatch.Run(nullSink) 起 headless run（与定时触发逐字同构，事件是 push 特例）。
//
// 默认关：main.go 仅在 POLLER_ENABLED 且 SECRET_MASTER_KEY 非空时起本 goroutine——
// 关时零行为变化（既有链路零回归）。
package poller

import (
	"context"
	"log/slog"
	"sync"
	"time"

	"github.com/google/uuid"

	"my-agent/control-plane/internal/connector"
	"my-agent/control-plane/internal/dispatch"
	"my-agent/control-plane/internal/event"
	"my-agent/control-plane/internal/secret"
	"my-agent/control-plane/internal/store"
)

// nullSink：headless run 空写端（事件仍由 hub 先落账本，会话列表/回放照常可见）。
// 与 scheduler.nullSink 逐字同构。
type nullSink struct{}

func (nullSink) WriteFrame(event.Envelope) error { return nil }
func (nullSink) WriteHeartbeat() error           { return nil }

// Poller 周期扫描到期连接器并触发匹配的 run。
type Poller struct {
	connectors store.ConnectorRepository
	triggers   store.TriggerRepository
	dispatcher *dispatch.Dispatcher
	conn       connector.Connector // 事件源实现（GitHub）
	masterKey  []byte              // AES-GCM 主密钥（解密 PAT）；空则不应构造 Poller
	runTimeout time.Duration
	tick       time.Duration
	slots      chan struct{} // 轮询并发独立上限（不吃交互配额）
	log        *slog.Logger

	// 重叠护栏（#4，对齐 scheduler.go 的「上一发仍 RUNNING 则跳过本拍」）：
	// 记录当前正被处理（processConnector 在途）的连接器 id。慢触发 run（时长>poll_interval）
	// 在途时，next_poll_at 会再次到期、ListDue 会再次返回该连接器；若不拦，会用尚未推进的
	// 旧游标重复 Poll 到同一批事件、在同一会话上并发起多个 run（同事件被处理多次）。
	// scheduler 用 DB 侧 last_run_id + GetRun(RUNNING) 判重叠；poller 是单实例后台 goroutine，
	// 且一次连接器轮询会触发 0..N 个 run（无单一 last_run_id 可记），故用进程内在途集合作等价护栏
	// （不新增迁移列，且 processConnector 的上下文带 runTimeout 超时——僵尸 goroutine 至多卡到
	// 超时即退出并解除在途，等价 scheduler 的「超过 runTimeout 即不再算重叠」自愈窗口）。
	mu       sync.Mutex
	inflight map[string]struct{}
}

// New 构造 poller。maxConcurrent 独立于 Dispatcher 全局准入（防轮询吃满交互配额）。
func New(connectors store.ConnectorRepository, triggers store.TriggerRepository, d *dispatch.Dispatcher,
	conn connector.Connector, masterKey []byte, runTimeout, tick time.Duration, maxConcurrent int, log *slog.Logger) *Poller {
	if maxConcurrent <= 0 {
		maxConcurrent = 2
	}
	if tick <= 0 {
		tick = 30 * time.Second
	}
	return &Poller{
		connectors: connectors, triggers: triggers, dispatcher: d, conn: conn, masterKey: masterKey,
		runTimeout: runTimeout, tick: tick,
		slots: make(chan struct{}, maxConcurrent), log: log,
		inflight: make(map[string]struct{}),
	}
}

// beginProcessing 尝试把连接器标记为「在途」；已在途返回 false（本拍跳过，重叠护栏）。
func (p *Poller) beginProcessing(id string) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	if _, ok := p.inflight[id]; ok {
		return false
	}
	p.inflight[id] = struct{}{}
	return true
}

// endProcessing 解除在途标记（processConnector 结束或超时退出时调用）。
func (p *Poller) endProcessing(id string) {
	p.mu.Lock()
	delete(p.inflight, id)
	p.mu.Unlock()
}

// Run 阻塞运行至 ctx 取消（main 优雅停机时 cancel）。
func (p *Poller) Run(ctx context.Context) {
	ticker := time.NewTicker(p.tick)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			p.pollDue(ctx)
		}
	}
}

func (p *Poller) pollDue(ctx context.Context) {
	due, err := p.connectors.ListDue(ctx, 16)
	if err != nil {
		p.warn("poller list due failed", "err", err)
		return
	}
	for _, c := range due {
		// 重叠护栏（#4）：上一发轮询仍在途（慢触发 run 未跑完，next_poll_at 已再度到期）→
		// 本拍跳过、不认领、不用旧游标重复 Poll——否则同事件会被重复取回并并发起多个 run。
		// 放在 Claim 之前：不推进 next_poll_at，行仍到期，run 跑完解除在途后下拍自然重试。
		if !p.beginProcessing(c.ConnectorID) {
			continue
		}
		// 轮询自有小信号量：满即本拍不再触发（行未认领，下拍推进的 next_poll_at 仍到期，下拍重试）。
		select {
		case p.slots <- struct{}{}:
		default:
			p.endProcessing(c.ConnectorID)
			return
		}
		// 先 Admit 后 Claim（同 scheduler：反序会在满载时推进 next_poll_at 却没跑，静默丢一拍）。
		// 一次连接器轮询占一个 dispatch 槽——本连接器命中的（通常 0..1 个）run 复用它顺序跑。
		release, ok := p.dispatcher.Admit()
		if !ok {
			<-p.slots
			p.endProcessing(c.ConnectorID)
			return // 系统满载：留行，下个 tick 重试
		}
		claimed, err := p.connectors.Claim(ctx, c.ConnectorID)
		if err != nil || !claimed {
			release()
			<-p.slots
			p.endProcessing(c.ConnectorID)
			if err != nil {
				p.warn("poller claim failed", "err", err)
			}
			continue // 已被禁用/边界竞争：放弃本条
		}
		go func(c store.Connector) {
			defer p.endProcessing(c.ConnectorID)
			defer release()
			defer func() { <-p.slots }()
			p.processConnector(c)
		}(c)
	}
}

// processConnector：解密 PAT → Poll → Normalize → 匹配 triggers → 命中起 run → 推进游标。
// headless：不依附任何 HTTP 请求，自建超时上下文。
func (p *Poller) processConnector(c store.Connector) {
	patBytes, err := secret.Open(p.masterKey, c.TokenCiphertext)
	if err != nil {
		p.warn("poller decrypt PAT failed", "connectorId", c.ConnectorID, "err", err)
		return
	}
	pat := string(patBytes)

	pollCtx, cancel := context.WithTimeout(context.Background(), p.runTimeout)
	defer cancel()
	raws, newCursor, err := p.conn.Poll(pollCtx, connector.Conn{Kind: c.Kind, PAT: pat, Cursor: c.Cursor})
	pat = "" // 明文 PAT 用完即弃（绝不落库/日志）
	if err != nil {
		p.warn("poller poll failed", "connectorId", c.ConnectorID, "err", err)
		return // 拉取失败不推进游标（下拍以同游标重试）
	}

	trigs, err := p.triggers.ListByConnector(context.Background(), c.ConnectorID)
	if err != nil {
		p.warn("poller list triggers failed", "connectorId", c.ConnectorID, "err", err)
		return
	}

	lastPollID := c.LastPollID
	dispatchFailed := false
	for _, raw := range raws {
		ev := p.conn.Normalize(raw)
		for _, t := range trigs {
			if !t.Enabled {
				continue
			}
			if !connector.MatchEvent(t.EventType, t.Filter, ev) {
				continue
			}
			if err := p.fire(c, t, ev); err != nil {
				// 派发失败（认知面短暂不可用 / CreateRun 写库失败等）：本拍不推进游标，
				// 让整批事件在下拍以同游标重取重试（#6，at-least-once——docs/16 明确不做去重）。
				dispatchFailed = true
			}
		}
		if raw.ID != "" {
			lastPollID = raw.ID
		}
	}

	// 任一命中事件派发失败 → 不推进游标：已匹配事件不被静默越过丢弃，下拍重试（#6）。
	if dispatchFailed {
		p.warn("poller skip cursor advance: dispatch failed this tick", "connectorId", c.ConnectorID)
		return
	}
	// 推进游标（即便无匹配也要推进，避免重复拉同一批；拉取失败/派发失败已提前 return 不推进）。
	if err := p.connectors.UpdateCursor(context.Background(), c.ConnectorID, newCursor, lastPollID); err != nil {
		p.warn("poller update cursor failed", "connectorId", c.ConnectorID, "err", err)
	}
}

// fire：一条命中 → 渲染 query_template → dispatch.Run(nullSink) 起 headless run。
// 与定时触发逐字同构（同一 dispatch.Run 单收口，事件是 push 特例）。
// 返回 dispatch.Run 的 error（起 run 失败）——供上层据此决定是否推进游标（#6）。
func (p *Poller) fire(c store.Connector, t store.Trigger, ev connector.InternalEvent) error {
	query := connector.RenderTemplate(t.QueryTemplate, ev.Fields)
	// needs_approval：dispatch/proto 不可改（无审批必需标志位），故经 query 前缀引导——
	// 触发的 run 对高危动作走 M11 HITL 审批闸（docs/16 §3.1 第三层）。
	if t.NeedsApproval {
		query = "（本任务涉及的高危动作请先请求人工审批再执行）\n\n" + query
	}
	runID := uuid.NewString()
	// 服务端生成固定会话 id（每规则一条会话，thread 记忆延续 + 列表单条目，同 sched-* 范式）。
	sessionID := "trig-" + shortID(t.TriggerID)
	runCtx, cancel := context.WithTimeout(context.Background(), p.runTimeout)
	defer cancel()
	if p.log != nil {
		p.log.Info("triggered run fired", "connectorId", c.ConnectorID, "triggerId", t.TriggerID,
			"runId", runID, "eventType", ev.Type)
	}
	if err := p.dispatcher.Run(runCtx, dispatch.StartCommand{
		RunID: runID, SessionID: sessionID, OwnerID: c.OwnerID,
		Query: query, AgentType: t.AgentType,
	}, nullSink{}); err != nil {
		p.warn("triggered run failed", "triggerId", t.TriggerID, "err", err)
		return err
	}
	return nil
}

func shortID(id string) string {
	if len(id) > 8 {
		return id[:8]
	}
	return id
}

func (p *Poller) warn(msg string, args ...any) {
	if p.log != nil {
		p.log.Warn(msg, args...)
	}
}
