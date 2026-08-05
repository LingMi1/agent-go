package api

// sse_frames_written 计数下沉到真实 SSE sink 的白盒测试（sseSink 未导出，须在包内测）：
// WriteFrame 成功即 Inc——实时 Pump 与回放端点共用本 sink，回放帧也计入；headless
// 定时 run 的 nullSink 与本 sink 无关，天然不计（docs/11 §3.2 语义修正）。

import (
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"

	"my-agent/control-plane/internal/event"
	"my-agent/control-plane/internal/metrics"
)

// framesWrittenValue 从 /metrics exposition 读 sse_frames_written 当前值（包级单例，
// 断言只用增量）。
func framesWrittenValue(t *testing.T) float64 {
	t.Helper()
	rec := httptest.NewRecorder()
	metrics.Handler().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	for _, ln := range strings.Split(rec.Body.String(), "\n") {
		if rest, ok := strings.CutPrefix(ln, "myagent_sse_frames_written_total "); ok {
			v, _ := strconv.ParseFloat(strings.TrimSpace(rest), 64)
			return v
		}
	}
	return 0
}

func TestSSESink_WriteFrameIncrementsFramesWritten(t *testing.T) {
	rec := httptest.NewRecorder() // ResponseRecorder 实现 http.Flusher
	sink, err := newSSESink(rec)
	if err != nil {
		t.Fatalf("newSSESink: %v", err)
	}

	before := framesWrittenValue(t)
	e := event.Envelope{
		Seq: 1, RunID: "r1", MessageID: "r1:think:1", Type: event.TypeToolThought,
		TSUnixMs: 1700000000000, IsFinal: true, Thought: &event.ThoughtPayload{Text: "x"},
	}
	if err := sink.WriteFrame(e); err != nil {
		t.Fatalf("WriteFrame: %v", err)
	}
	if got := framesWrittenValue(t) - before; got != 1 {
		t.Fatalf("期望内容帧写出后计数 +1，得到 %v", got)
	}

	// 心跳不是内容帧，不计。
	if err := sink.WriteHeartbeat(); err != nil {
		t.Fatalf("WriteHeartbeat: %v", err)
	}
	if got := framesWrittenValue(t) - before; got != 1 {
		t.Fatalf("心跳不应计入内容帧计数，得到增量 %v", got)
	}
}
