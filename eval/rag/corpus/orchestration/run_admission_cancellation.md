---
document_id: orchestration_run_admission_cancellation
title: Run 请求准入、背压与取消传播
module: orchestration
version: "1.0"
source_revision: c04ce65
status: implemented
source_files:
  - control-plane/internal/dispatch/dispatch.go
  - control-plane/internal/api/api.go
  - control-plane/internal/stream/hub.go
  - control-plane/internal/config/config.go
---

# Run 请求准入、背压与取消传播

## 业务目标

控制面在进入昂贵的模型链路前实施非阻塞准入，避免过载请求占用数据库、gRPC 和 SSE 资源。客户端退出或运行超时应沿 context 传播并形成可审计终态。

## 执行流程

HTTP handler 在写任何 SSE 头之前调用 `Dispatcher.Admit`。`semaphore.Weighted.TryAcquire(1)` 成功才创建 run 和 gRPC 流，失败立即返回 429。成功请求被 `context.WithTimeout` 包裹；客户端断开会取消请求 context，gRPC 流随之取消。完成时使用 `context.WithoutCancel` 写回 run 终态，避免断开导致收口失败。

## 关键数据结构

`Dispatcher` 持有请求级信号量、run 仓库、认知客户端、stream Hub 和最大步数。`StartCommand` 记录模式、附件、输出格式、审批恢复与分叉 metadata。run 状态可为 RUNNING、SUCCESS、FAILED、STOPPED 或 TIMEOUT。

## 失败场景

认知流打开失败会将 run 标为 FAILED。超时产生 TIMEOUT 和 `RUN_TIMEOUT`，普通取消产生 STOPPED 和 `CLIENT_GONE`，SSE 写失败产生 STOPPED。事件持久化、seq 或协议错误产生 FAILED。

## 限制与消歧

请求级准入默认上限 16，与 Python `Send` 的任务级并发上限 2 相互独立。系统没有排队等待逻辑，满载请求直接 429。仓库指标可观察并发水位和拒绝次数，但没有提交吞吐基准数据。
