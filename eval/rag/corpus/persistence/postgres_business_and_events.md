---
document_id: persistence_postgres_business_and_events
title: PostgreSQL 业务状态与事件账本
module: persistence
version: "1.0"
source_revision: c04ce65
status: implemented
source_files:
  - control-plane/internal/store/migrations/0001_runs.sql
  - control-plane/internal/store/migrations/0002_events.sql
  - control-plane/internal/store/run.go
  - control-plane/internal/store/event.go
---

# PostgreSQL 业务状态与事件账本

## 业务目标

Go 控制面以 PostgreSQL 保存对外可审计的 run 生命周期、事件事实和管理数据。它是会话列表、历史回放、用量统计、认证、定时任务与连接器的持久化基础。

## 执行流程

每次 run 在调用认知面前插入 `runs`，状态初始 RUNNING。事件从 gRPC 到达后逐条插入 `events`。终态 result 或流错误触发 `FinishRun` 写入状态、摘要、错误和 token 用量。会话不是单独主表，而是 runs 与 session_forks 的查询投影。

## 关键数据结构

`runs` 以 run ID 为主键，包含 session、owner、entry agent、query、状态、摘要、错误、schema version、时间和用量。`events` 以 `(run_id, seq)` 为主键，payload 为 JSONB。后续迁移增加 schedules、session_forks、users、auth_sessions、connectors 和 triggers。

## 失败场景

run 创建失败时不会调用认知面。事件唯一约束冲突返回 duplicate seq；事件写失败使当前 run FAILED。控制面启动时迁移失败会直接退出。客户端断开后，终态写入使用脱离取消的 context 尽力完成。

## 限制与消歧

事件账本保存对外事实，不作为 LangGraph 节点状态恢复源；checkpoint 由另一套 LangGraph Postgres 表管理。删除会话会按仓库实现清理业务记录，但知识库向量和 MinIO 对象有各自生命周期。PostgreSQL 不是向量库。
