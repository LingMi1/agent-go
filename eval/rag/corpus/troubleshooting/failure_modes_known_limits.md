---
document_id: troubleshooting_failure_modes_known_limits
title: 常见故障、降级路径与已知限制
module: troubleshooting
version: "1.0"
source_revision: c04ce65
status: partial
source_files:
  - control-plane/internal/stream/hub.go
  - cognition/cognition/server/servicer.py
  - cognition/cognition/config.py
  - README.md
---

# 常见故障、降级路径与已知限制

## 业务目标

本篇集中记录容易被功能清单掩盖的运行条件和降级语义，供故障检索时区分“功能不存在”“默认关闭”“依赖不可用”和“已有数据可回放”。

## 执行流程

健康端点并发检查 PostgreSQL 与 cognition gRPC。请求过载在 SSE 头写出前返回 429。运行中断后，事件账本可回放已落库帧；有 checkpoint 的 ReAct 审批可另起 run 恢复。附件、OCR、MinIO 上传、MCP server 和可观测 optional seam 多采用局部降级或 fail-soft。

## 关键数据结构

run 错误码包括 CLIENT_GONE、RUN_TIMEOUT、SINK_WRITE_ERROR、EVENT_INVALID、SEQ_GAP、PERSIST_ERROR 和 STREAM_EOF_NO_FINISH。配置默认关闭 RAG、rerank、code interpreter、HITL tool list、真实图像生成、鉴权强制和 OTel。

## 失败场景

SSE 断线会取消活 run，不能用 Last-Event-ID 原地续跑。无 PG 时 checkpoint 不能跨重启，HITL 仅内存恢复。Plan 模式不支持审批和图片多模态输入。真实 embedding/reranker、OCR、图像生成、MCP 和外部连接器依赖服务或密钥。

## 限制与消歧

仓库没有完整 Eval harness、60 条 RAG 标注集、Recall@5 数据或 20 组并行耗时基准；这些指标均为 unknown。Flow、NL2SQL/TableRAG、Gmail/飞书、gVisor/microVM、Vault/KMS 和 WebSocket 未实现。README 中 Redis/NATS/WS 等架构词不能覆盖源码接线事实。
