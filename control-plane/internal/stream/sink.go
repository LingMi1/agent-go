package stream

import "my-agent/control-plane/internal/event"

// Sink 是面向客户端的事件输出端口（SSE 实现见 api 包）。抽象出来便于测试。
type Sink interface {
	WriteFrame(e event.Envelope) error
	WriteHeartbeat() error
	WriteDone() error // 服务端主动关闭：发送终止事件，便于客户端区分正常关机与网络故障。
}

// NullSink 是 Sink 的零行为实现，用于 headless run（调度器/轮询器触发的无客户端 run）。
// 事件仍由 Hub 先落账本，会话列表/回放照常可见。
type NullSink struct{}

func (NullSink) WriteFrame(event.Envelope) error { return nil }
func (NullSink) WriteHeartbeat() error           { return nil }
func (NullSink) WriteDone() error                { return nil }
