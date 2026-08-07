package stream

import "my-agent/control-plane/internal/event"

// Sink 是面向客户端的事件输出端口（SSE 实现见 api 包）。抽象出来便于测试。
type Sink interface {
	WriteFrame(e event.Envelope) error
	WriteHeartbeat() error
	WriteDone() error // 服务端主动关闭：发送终止事件，便于客户端区分正常关机与网络故障。
}
