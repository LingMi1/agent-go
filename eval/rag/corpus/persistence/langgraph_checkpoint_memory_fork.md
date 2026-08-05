---
document_id: persistence_langgraph_checkpoint_memory_fork
title: LangGraph Checkpoint、会话记忆与分叉
module: persistence
version: "1.0"
source_revision: c04ce65
status: implemented
source_files:
  - cognition/cognition/checkpoint/postgres.py
  - cognition/cognition/server/servicer.py
  - cognition/cognition/graphs/history.py
  - cognition/tests/test_fork_seed.py
---

# LangGraph Checkpoint、会话记忆与分叉

## 业务目标

Checkpoint 保存图内执行状态，支持同一会话续聊、interrupt 恢复和从历史轮创建独立时间线。入模历史再做有界投影，避免 checkpoint 持续增长直接撑爆模型上下文。

## 执行流程

配置 PG DSN 时，服务建立 `AsyncPostgresSaver` 和连接池并执行 setup。根图以 `thread_id=session_id` 编译。每次 run 把 run ID写入 checkpoint metadata。分叉时从父 thread 过滤该 run ID 的最新快照，只把 messages 通道通过 `aupdate_state` 播种到新 thread，然后正常执行新会话首轮。

## 关键数据结构

历史投影保留前导 system、首条 Human 和近期消息，把被挤出的旧组折叠为确定性摘要；AI tool calls 与紧邻 ToolMessage 作为原子组。分叉登记存在 Go 的 `session_forks`，真正认知记忆存在 LangGraph checkpoint。

## 失败场景

找不到父 run checkpoint、快照无 messages 或播种异常时，首轮显式错误收尾，绝不静默变为空记忆会话。目标 thread 已有 checkpoint 时跳过重复播种。未配置 PG 的普通会话不具备跨重启持久记忆。

## 限制与消歧

分叉只复制 messages，不复制 run/events、计划状态、sub_results 或 pending interrupt；父子时间线之后独立演化。Plan-only 分叉点可能没有可继承 messages。历史摘要是只读投影，不回写 checkpoint，也不是事件账本重建记忆。
