---
document_id: architecture_dual_plane_system
title: Go 控制面与 Python 认知面双平面架构
module: architecture
version: "1.0"
source_revision: c04ce65
status: implemented
source_files:
  - control-plane/cmd/controlplane/main.go
  - cognition/cognition/server/grpc_server.py
  - proto/agent/v1/agent.proto
---

# Go 控制面与 Python 认知面双平面架构

## 业务目标

Pro-Agent 将对外服务和智能体推理拆成两个进程。Go 控制面是唯一 HTTP 入口，负责身份、run 生命周期、并发准入、事件账本、SSE、上传与管理接口；Python 认知面在内部运行 LangGraph 图、模型和工具。拆分后，外部协议与认知图可以分别演进。

## 执行流程

控制面收到 `POST /runs` 后生成 run，申请并发槽并写入 PostgreSQL，再通过 `CognitionService.Run` 发起 server-streaming gRPC。认知面按 `agent_type` 选择 ReAct 或 Plan-Execute 图，把 LangGraph 事件映射为统一 `Event` 流。Go 逐帧持久化并经 SSE 推给浏览器。认知面只暴露内部 gRPC 端口，Compose 不发布其宿主机端口。

## 关键数据结构

`RunRequest` 携带 `run_id`、`session_id`、查询、模式、步数、扁平 metadata 和附件引用。`Event` 用 `seq`、`message_id`、`type`、`finish` 与 oneof payload 表达思考、工具、计划、审批和最终结果。Go 的 `StartCommand` 是 HTTP 请求到 gRPC 请求之间的控制面命令。

## 失败场景

PostgreSQL 或认知面连接失败会阻止控制面正常启动。MinIO 桶检查失败只影响上传和产物，不阻断纯文本 ReAct/Plan 主链路。认知面节点异常会被转换为终态错误事件，gRPC 客户端取消会停止图流。

## 限制与消歧

双平面不等于两个对外 API：Python 不是公网入口。README 提到 SSE/WS，但当前路由只有 SSE，没有 WebSocket 服务。Redis 和 NATS 容器也不在这条请求链中。跨面使用的是 gRPC 流，而浏览器接收的是 HTTP SSE。
