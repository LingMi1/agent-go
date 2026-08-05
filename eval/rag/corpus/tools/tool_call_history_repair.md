---
document_id: tools_tool_call_history_repair
title: Tool-call History 自动修复
module: tools
version: "1.0"
source_revision: c04ce65
status: implemented
source_files:
  - cognition/cognition/graphs/history.py
  - cognition/cognition/graphs/nodes.py
  - cognition/cognition/graphs/plan_execute.py
  - cognition/tests/test_history_repair.py
---

# Tool-call History 自动修复

## 业务目标

工具执行中断可能在 checkpoint 中留下带 `tool_calls` 的 AIMessage，却没有对应 ToolMessage；反向也可能出现孤儿 ToolMessage。严格 provider 会因此拒绝后续整段历史，使会话持续失败。

## 执行流程

`repair_dangling_tool_calls` 顺序扫描消息。遇到 AIMessage 工具调用组时，收集紧随其后的 ToolMessage ID，对缺失 ID 补一条 `status=error` 的合成消息；没有前置调用的 ToolMessage 被丢弃。健康序列原对象返回。ReAct think 和 Plan planner 都在入模前调用该函数。

## 关键数据结构

合成 ToolMessage 使用原 `tool_call_id`，内容说明调用被中断且无结果。修复结果只用于本次模型调用；原始 state、checkpoint 和事件账本不回写。历史裁剪另将 AI tool-call 与紧邻 ToolMessage 作为不可拆原子组。

## 失败场景

工具运行期异常通常已由 ToolNode 转成合法 error ToolMessage；修复主要处理异常终止、旧 checkpoint 或 planner 结构化调用缺 ack 的遗留状态。无 ID 的异常工具调用无法构造可靠配对，只能保持 provider 兼容性依赖上游消息形状。

## 限制与消歧

该功能修复的是模型输入协议，不修复 checkpoint 文件或数据库行，也不重放失败工具。它能让被污染会话继续对话，但不能恢复中断工具的副作用或结果。Plan planner 现在也主动写 planning ack，repair 是兼容旧历史的第二道保护。
