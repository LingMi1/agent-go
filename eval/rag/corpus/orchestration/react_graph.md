---
document_id: orchestration_react_graph
title: ReAct 思考与工具执行循环
module: orchestration
version: "1.0"
source_revision: c04ce65
status: implemented
source_files:
  - cognition/cognition/graphs/react.py
  - cognition/cognition/graphs/nodes.py
  - cognition/cognition/graphs/state.py
  - cognition/tests/test_state_routing.py
---

# ReAct 思考与工具执行循环

## 业务目标

ReAct 模式面向快速、开放式任务，让模型在思考、工具调用和观察之间循环，直到形成最终回答或达到步数限制。项目手工声明图拓扑，以控制事件映射、历史投影和错误语义。

## 执行流程

图拓扑为 `START -> agent -> tools|END`，工具节点完成后回到 agent。think 节点在调用模型前依次修复 tool-call history、按预算裁剪历史、展开附件引用，并临时注入生图或输出格式提示。模型返回带 `tool_calls` 的 `AIMessage` 且当前 step 小于 `max_steps` 时进入 `ToolNode`，否则结束。

## 关键数据结构

`AgentState.messages` 使用 LangGraph `add_messages` reducer；其余字段记录 `request_id`、`session_id`、原始 query、附件/产物引用、流式标记和 step。step 每次 think 后加一。工具异常由 `handle_tool_errors` 转成配对的 error `ToolMessage`。

## 失败场景

工具参数或运行期异常不会直接炸穿整个 run，而是作为失败 observation 返回模型决定后续动作。模型或图节点的未捕获异常由 gRPC servicer 转成错误 result。达到 `max_steps` 时，即便最后一条消息仍有工具调用也会停止。

## 限制与消歧

ReAct 的循环是单 Agent 自主决策，不等于 Plan-Execute 的多个 executor 分支。`ToolNode` 可以处理一条模型消息中的工具调用，但仓库没有独立的工具级全局并发控制器。输出格式提示只在本次模型调用中存在，不写入 checkpoint。
