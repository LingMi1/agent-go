package api

import (
	"fmt"
	"net/http"

	"my-agent/control-plane/internal/event"
	"my-agent/control-plane/internal/metrics"
)

// sseSink 把 Envelope 渲染成 SSE 帧写到 HTTP 响应。是 stream.Sink 的实现。
// 由单个 handler goroutine 独占写 ResponseWriter，故无需加锁。
type sseSink struct {
	w http.ResponseWriter
	f http.Flusher
}

// writeSSEHeaders 设置 SSE 必要的响应头（须在写 body 之前调用）。
func writeSSEHeaders(w http.ResponseWriter) {
	h := w.Header()
	h.Set("Content-Type", "text/event-stream")
	h.Set("Cache-Control", "no-cache")
	h.Set("Connection", "keep-alive")
	h.Set("X-Accel-Buffering", "no") // 禁用反代缓冲，保证实时
}

func newSSESink(w http.ResponseWriter) (*sseSink, error) {
	f, ok := w.(http.Flusher)
	if !ok {
		return nil, fmt.Errorf("api: response writer does not support flushing")
	}
	return &sseSink{w: w, f: f}, nil
}

func (s *sseSink) WriteFrame(e event.Envelope) error {
	data, err := event.ToSSEFrame(e)
	if err != nil {
		return err
	}
	// 内容帧：event: message + id: <seq>（供 Last-Event-ID 续传）+ data: <json>
	if _, err := fmt.Fprintf(s.w, "event: message\nid: %d\ndata: %s\n\n", e.Seq, data); err != nil {
		return err
	}
	s.f.Flush()
	// 帧计数在真实 SSE sink 上报（实时 Pump 与回放端点共用本 sink）：headless 定时
	// run 的 nullSink 不再注水，指标语义=「真实写给 SSE 客户端的内容帧」（docs/11 §3.2）。
	metrics.SSEFramesWritten().Inc()
	return nil
}

func (s *sseSink) WriteHeartbeat() error {
	if _, err := fmt.Fprint(s.w, "event: heartbeat\ndata: {\"messageType\":\"heartbeat\"}\n\n"); err != nil {
		return err
	}
	s.f.Flush()
	return nil
}
