---
document_id: architecture_event_contract_ledger_replay
title: 统一事件契约、事实账本与同构回放
module: architecture
version: "1.0"
source_revision: c04ce65
status: implemented
source_files:
  - proto/agent/v1/agent.proto
  - control-plane/internal/stream/hub.go
  - control-plane/internal/store/event.go
  - control-plane/internal/api/api.go
---

# 统一事件契约、事实账本与同构回放

## 业务目标

系统用同一事件模型承载实时展示、审计和历史恢复界面，避免前端为实时流和历史记录维护两套解释逻辑。事件账本记录已经发生的外部事实，而不是重新执行智能体。

## 执行流程

Python 为每个 run 从 1 开始分配连续 `seq`。Go `Hub.Pump` 校验事件结构和 `seq == lastSeq+1`，先调用事件仓库 `Append`，成功后才写 SSE。`GET /runs/{runID}/events` 按序读取账本，并复用实时路径的 SSE 编码器逐帧重发。心跳由 Go 定时生成，不经过 gRPC、不占 seq、也不落库。

## 关键数据结构

PostgreSQL `events` 以 `(run_id, seq)` 为主键，payload 使用 JSONB。`message_id` 是前端原位更新键，工具调用时等于 `tool_call_id`。只有 result 事件可令 `finish=true`。事件类型包括 tool thought/call/result、plan thought/snapshot/task、approval request 和 result。

## 失败场景

非法事件、seq 空洞、重复主键或持久化错误都会使 Pump 以 FAILED 收口；SSE 写失败被视为客户端断开。gRPC 正常 EOF 却没有 finish 事件会产生 `STREAM_EOF_NO_FINISH`。回放发现旧数据序列异常时只记录告警，仍尽力返回已有账本。

## 限制与消歧

事件回放不是 checkpoint resume：回放只重发已落库事件，不继续模型或工具执行。SSE 帧虽然带 `id: seq`，服务端没有读取 `Last-Event-ID` 来续接仍活跃的 run；断线会取消当前请求，之后只能回放已持久化部分。
