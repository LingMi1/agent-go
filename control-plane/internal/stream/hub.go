// Package stream 承载单次 run 的事件管道：从认知面收事件 → 校验 seq → 先落库 → 再推 SSE，
// 并发出心跳、处理取消。它是“事实先持久化、再展示”和“重放=实时”的执行点。
package stream

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"time"

	"my-agent/control-plane/internal/cognition"
	"my-agent/control-plane/internal/event"
	"my-agent/control-plane/internal/metrics"
	"my-agent/control-plane/internal/store"
)

// Result 是一次 Pump 的终态，供上层写回 run 生命周期。
type Result struct {
	Status    string // store.Status*
	Summary   string
	ErrorCode string
	ErrorMsg  string
	Usage     *event.UsagePayload // M11：终态 RESULT 附带的全 run 用量（可 nil）
}

// Hub 把一条认知事件流泵到存储与客户端。
type Hub struct {
	events    store.EventRepository
	heartbeat time.Duration
	log       *slog.Logger
}

func NewHub(events store.EventRepository, heartbeat time.Duration, log *slog.Logger) *Hub {
	if heartbeat <= 0 {
		heartbeat = 10 * time.Second
	}
	return &Hub{events: events, heartbeat: heartbeat, log: log}
}

// Pump 消费 stream 直到 run 结束/取消/出错，返回终态。
// 不变量：每个事件先 Append 落库、再 WriteFrame 推送（先持久化后展示）。
func (h *Hub) Pump(ctx context.Context, runID string, s cognition.Stream, sink Sink) Result {
	evCh := make(chan event.Envelope, 16)
	recvErrCh := make(chan error, 1)

	// G1：接收 goroutine。ctx 取消时底层 gRPC 流会让 Recv 出错，从而退出。
	go func() {
		for {
			e, err := s.Recv()
			if err != nil {
				recvErrCh <- err
				close(evCh)
				return
			}
			select {
			case evCh <- e:
			case <-ctx.Done():
				return
			}
		}
	}()

	ticker := time.NewTicker(h.heartbeat)
	defer ticker.Stop()

	var lastSeq uint64
	for {
		select {
		case <-ctx.Done():
			// 客户端断开 → STOPPED；超时 → TIMEOUT。Python 侧随 gRPC 取消而停在最后 checkpoint。
			if ctx.Err() == context.DeadlineExceeded {
				metrics.PumpErrors().WithLabelValues("RUN_TIMEOUT").Inc()
				return Result{Status: store.StatusTimeout, ErrorCode: "RUN_TIMEOUT"}
			}
			metrics.PumpErrors().WithLabelValues("CLIENT_GONE").Inc()
			return Result{Status: store.StatusStopped, ErrorCode: "CLIENT_GONE"}

		case <-ticker.C:
			_ = sink.WriteHeartbeat() // 心跳不落库、不计入 seq

		case e, ok := <-evCh:
			if !ok {
				return h.onStreamClosed(recvErrCh)
			}
			if err := e.Validate(); err != nil {
				return failed("EVENT_INVALID", err)
			}
			if e.Seq != lastSeq+1 { // Python 分配 seq，这里做单调/无空洞校验
				return failed("SEQ_GAP", fmt.Errorf("expected seq %d, got %d", lastSeq+1, e.Seq))
			}
			lastSeq = e.Seq

			if err := h.events.Append(ctx, e); err != nil { // 先落库
				return failed("PERSIST_ERROR", err)
			}
			metrics.EventsPersisted().Inc()
			// 帧计数不在此处：sse_frames_written 由真实 SSE sink 自身上报（api sseSink），
			// 定时 headless run 的 nullSink 不注水、回放路径也能计入（docs/11 §3.2）。
			if err := sink.WriteFrame(e); err != nil { // 再推送；写客户端失败按断开处理
				metrics.PumpErrors().WithLabelValues("SINK_WRITE_ERROR").Inc()
				return Result{Status: store.StatusStopped, ErrorCode: "SINK_WRITE_ERROR", ErrorMsg: err.Error()}
			}
			if e.Finish {
				res := Result{Status: store.StatusSuccess, Summary: resultText(e)}
				if e.Result != nil {
					res.Usage = e.Result.Usage
				}
				return res
			}
		}
	}
}

func (h *Hub) onStreamClosed(recvErrCh chan error) Result {
	select {
	case err := <-recvErrCh:
		if err == io.EOF {
			// 流正常结束却没有 finish，属异常（缺终态事件）。
			metrics.PumpErrors().WithLabelValues("STREAM_EOF_NO_FINISH").Inc()
			return Result{Status: store.StatusFailed, ErrorCode: "STREAM_EOF_NO_FINISH", ErrorMsg: "cognition stream ended before finish"}
		}
		metrics.PumpErrors().WithLabelValues("STREAM_RECV_ERROR").Inc()
		return Result{Status: store.StatusFailed, ErrorCode: "STREAM_RECV_ERROR", ErrorMsg: err.Error()}
	default:
		metrics.PumpErrors().WithLabelValues("STREAM_CLOSED").Inc()
		return Result{Status: store.StatusFailed, ErrorCode: "STREAM_CLOSED"}
	}
}

func failed(code string, err error) Result {
	metrics.PumpErrors().WithLabelValues(code).Inc() // EVENT_INVALID / SEQ_GAP / PERSIST_ERROR
	return Result{Status: store.StatusFailed, ErrorCode: code, ErrorMsg: err.Error()}
}

func resultText(e event.Envelope) string {
	if e.Result != nil {
		return e.Result.Text
	}
	return ""
}
