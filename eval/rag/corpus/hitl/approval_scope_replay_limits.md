---
document_id: hitl_approval_scope_replay_limits
title: HITL 适用范围与节点重放限制
module: hitl
version: "1.0"
source_revision: c04ce65
status: partial
source_files:
  - cognition/cognition/approval.py
  - cognition/cognition/server/grpc_server.py
  - cognition/cognition/server/servicer.py
  - cognition/tests/test_approval.py
---

# HITL 适用范围与节点重放限制

## 业务目标

本模块记录审批机制不能覆盖的模式和恢复语义，防止把局部安全门描述成全平台事务或通用人工工作流。

## 执行流程

只有 ReAct 主图绑定审批包装后的工具。Plan-Execute 和 deep_research 的 executor 使用未包装工具，因为分支会捕获 GraphInterrupt，且 executor 子图没有 checkpointer。未配置 PG 但启用审批时，服务退到 `InMemorySaver`，仅支持同进程恢复。

## 关键数据结构

当前 `first_interrupt_payload` 只取首个审批中断，resume 字符串也只处理一个决议。挂起状态在 checkpoint tasks/writes 中；恢复时从 state 恢复附件白名单，但 `output_format` 不在 checkpoint 内。

## 失败场景

LangGraph resume 会重跑 interrupt 所在节点中断前的代码，因此实现要求所有真实副作用放在 interrupt 之后。同一 ToolNode 批次中的其他工具仍可能随节点重放再次执行。用户在 pending 期间直接发送新消息会从 START 开新轮，旧 pending 任务失效。

## 限制与消歧

Plan 模式高危工具没有 HITL 保护；多并发审批、工具副作用幂等键和分布式审批队列均未实现。恢复轮无法还原原 output format，会回落自由格式。PG 支持跨进程恢复，但没有证据证明任意部署拓扑下的多实例协调，状态为 unknown。
