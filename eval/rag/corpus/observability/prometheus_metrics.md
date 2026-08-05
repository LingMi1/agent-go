---
document_id: observability_prometheus_metrics
title: Prometheus 指标与运行水位
module: observability
version: "1.0"
source_revision: c04ce65
status: implemented
source_files:
  - control-plane/internal/metrics/metrics.go
  - control-plane/internal/dispatch/dispatch.go
  - control-plane/internal/stream/hub.go
  - control-plane/internal/api/api.go
---

# Prometheus 指标与运行水位

## 业务目标

控制面暴露低基数指标，观察 HTTP 错误率、run 并发和终态、端到端时长、模型用量、事件泵故障、SSE 输出、调度跳拍和 PostgreSQL 连接池。

## 执行流程

独立 Prometheus Registry 在进程初始化时注册指标，`/metrics` 直接暴露该 Registry。HTTP middleware 在 handler 返回后读取 chi route pattern、method 和 status。Dispatcher 在准入、释放和 run 收口时埋点；Hub 在事件持久化和错误分支埋点；SSE sink 在真实写帧后计数。

## 关键数据结构

核心指标包括 `myagent_runs_in_flight`、`runs_rejected_total`、按 status/agent type 的 run 计数、run duration histogram、input/output tokens、model calls、events persisted、SSE frames、pump errors、scheduler fired/skipped，以及五个 pgxpool gauge。

## 失败场景

未知 HTTP method 统一标为 `other`，无匹配路由统一标为 `unmatched`，避免任意输入制造高基数。middleware 使用 chi WrapResponseWriter 保留 Flusher，否则会破坏 SSE。重复注册 pg pool gauge 仅忽略 AlreadyRegistered。

## 限制与消歧

SSE 帧计数包含实时与回放内容帧，不含 heartbeat 和 headless run。run duration 是整个控制面链路，不是单个 LangGraph 节点耗时。代码没有 RAG Recall、准确率或每个 Send 分支的专用 Prometheus 指标，不能从 `/metrics` 直接得到这些质量数据。
