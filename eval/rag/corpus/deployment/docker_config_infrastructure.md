---
document_id: deployment_docker_config_infrastructure
title: Docker Compose、配置开关与基础设施用途
module: deployment
version: "1.0"
source_revision: c04ce65
status: partial
source_files:
  - deploy/docker-compose.yml
  - deploy/.env.example
  - control-plane/internal/config/config.go
  - cognition/cognition/config.py
---

# Docker Compose、配置开关与基础设施用途

## 业务目标

项目支持仅启动基础设施的开发模式，以及通过 `app` profile 构建控制面、认知面和前端的完整栈。配置分别由 Go 环境变量和 `COGNITION_` 前缀的 Python settings 读取。

## 执行流程

默认 Compose 启动 PostgreSQL、Qdrant、Redis、MinIO 和 NATS；`--profile app` 再启动内部 cognition 和唯一对外的 control-plane。控制面托管构建后的 SPA，连接 PostgreSQL、MinIO、Qdrant 和 cognition。Tempo 位于额外 `observability` profile。

## 关键数据结构

PostgreSQL 挂载业务/checkpoint 数据，Qdrant 挂载向量数据，MinIO 挂载对象。控制面默认端口 8080，认知 gRPC 在容器网络暴露 50051。关键开关包括 RAG、真实对象上传、鉴权、poller、OTel、MCP、Skill runner 和 code interpreter。

## 失败场景

控制面依赖 PostgreSQL、MinIO 健康和 cognition；认知面依赖 PostgreSQL、MinIO、Qdrant。错误地把宿主机 localhost 地址带入容器会连接失败，因此 Compose 显式覆盖服务名端点。密钥只注入 cognition，不进入控制面镜像层。

## 限制与消歧

Redis 和 NATS 虽有容器、卷和描述，但当前 Go/Python 运行代码没有客户端接线；实际缓存、限流、pub-sub 或事件总线用途为 unknown。认知容器显式使用 local Skill runner，不提供 Docker-in-Docker 沙箱。完整栈不包含 Grafana 或 Prometheus 服务。
