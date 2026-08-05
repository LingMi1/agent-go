---
document_id: tools_unified_registry
title: 本地、MCP、Skill 与 RAG 统一工具注册表
module: tools
version: "1.0"
source_revision: c04ce65
status: implemented
source_files:
  - cognition/cognition/tools/registry.py
  - cognition/cognition/mcp/naming.py
  - cognition/cognition/events/mapper.py
  - cognition/tests/test_tool_provider_mapping.py
---

# 本地、MCP、Skill 与 RAG 统一工具注册表

## 业务目标

不同来源的能力统一成 LangChain `BaseTool`，使模型绑定、LangGraph ToolNode、事件映射和产物协议无需为每个 provider 单独实现。

## 执行流程

服务启动时先加入 calculator 和 write_report，再按配置预热 MCP、编译 knowledge_search、装配图像生成、web fetch、code interpreter 和 Skill 工具。最终列表按名称去重，生成 `provider_map` 注入 EventMapper，并收集 MCP 等需要停机关闭的资源。

## 关键数据结构

工具 metadata 的 `provider` 值为 local、mcp 或 skill。MCP 工具使用 `mcp__{server}__{tool}` 全局命名，避免与本地和其他 server 冲突。工具事件通过 provider map 写入 `tool_provider`，产物统一为 ArtifactRef。

## 失败场景

单个 MCP server 预热失败只告警并记录 errors，不阻断其他工具。RAG、图像或 Skill 的装配错误可能在启动阶段上浮。重复名称采用后者覆盖对象、首次出现位置不变的稳定去重语义。

## 限制与消歧

“统一工具生态”指统一运行时接口和事件协议，不代表所有工具都能通过配置无代码加入；本地工具仍需实现并注册。MCP 由 server 配置发现，Skill 由目录中的 `SKILL.md` 发现。各工具是否默认启用由独立配置决定。
