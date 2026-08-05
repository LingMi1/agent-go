---
document_id: control_plane_grpc_sse_streaming
title: gRPC 到 SSE 的跨面流式链路
module: control-plane
version: "1.0"
source_revision: c04ce65
status: implemented
source_files:
  - control-plane/internal/cognition/client.go
  - control-plane/internal/stream/hub.go
  - control-plane/internal/api/sse.go
  - proto/agent/v1/agent.proto
---

# gRPC 到 SSE 的跨面流式链路

## 业务目标

模型与工具执行过程应逐步到达浏览器，同时保留结构化契约和取消传播。gRPC 承担内部跨进程流，SSE 承担浏览器友好的单向 HTTP 流。

## 执行流程

Go cognition client 把控制面 RunRequest 转成 proto，并调用 server-streaming `Run`。Hub 用接收 goroutine读取 gRPC 事件，通过有界 channel 交给单个 Pump 循环顺序持久化和写 SSE。SSE 每个内容帧包含 `event: message`、`id: seq` 和 JSON data，并立即 Flush；独立 ticker 写 heartbeat。

## 关键数据结构

proto Event 是跨面的规范形状，Go Envelope 是账本和 SSE 形状。SSE sink 由单 handler goroutine 独占，无需写锁。channel 容量为 16，消费者变慢会自然阻塞 gRPC 接收，形成进程内背压。

## 失败场景

ResponseWriter 不支持 Flusher 时不能建立 SSE。gRPC Recv 错误、EOF 无 finish、事件无效、落库失败或客户端写失败都会映射为 run 错误状态。context 取消会停止接收并取消 Python 图执行。

## 限制与消歧

SSE 只支持服务端到客户端，不是双向 WebSocket。审批通过另发 HTTP POST，再开启一条新 SSE run。心跳只是传输保活，不进入事件账本，也不参与前端业务 reducer 的 seq。
