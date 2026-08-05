---
document_id: observability_otel_langfuse_grafana
title: OpenTelemetry、Langfuse、Tempo 与 Grafana 边界
module: observability
version: "1.0"
source_revision: c04ce65
status: partial
source_files:
  - control-plane/internal/observability/otel.go
  - cognition/cognition/observability/otel_seam.py
  - cognition/cognition/observability/langfuse_seam.py
  - deploy/observability/README.md
---

# OpenTelemetry、Langfuse、Tempo 与 Grafana 边界

## 业务目标

一次 run 可用同一 trace ID关联 Go 控制面、gRPC 和 Python 认知面日志；运维侧可将 Prometheus 指标和 Tempo trace 接入 Grafana。所有追踪能力默认关闭，失败时不影响 Agent 主链路。

## 执行流程

启用 OTel 时，Go 创建 OTLP/gRPC exporter、全局 provider 和 W3C propagator，Dispatcher 为每个 run 创建 `agent.run` span，gRPC stats handler传播上下文。Python optional seam 可创建 provider 和 aio server interceptor，读取当前 trace ID写入日志。Langfuse 通过可选 callback 注入 LangGraph config。

## 关键数据结构

服务名分别为 control-plane 和 cognition，OTLP 默认端点 4317。Tempo 以 Compose `observability` profile 启动。仓库提供 Grafana datasource/dashboard provisioning 和 Prometheus scrape 片段，看板包含 8 个控制面面板。

## 失败场景

Go exporter 装配失败只告警并继续。Python OTel 或 Langfuse 未安装、配置错误时 import guard 返回 no-op。Tempo 不可达时 exporter 不能提供有效 trace，但不改变 run 业务结果。

## 限制与消歧

Python 基础镜像没有安装 `otel` optional extra，单设 `OTEL_ENABLED=1` 仍可能只有 Go span。Grafana 和 Prometheus 不在本项目 Compose 中启动，需接入外部实例；Tempo 才是可选 Compose 服务。Langfuse 仅有 seam，真实 trace 验证状态为 unknown。OTel 只做 trace，metrics 仍走原生 Prometheus。
