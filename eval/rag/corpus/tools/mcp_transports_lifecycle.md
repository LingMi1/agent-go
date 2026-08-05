---
document_id: tools_mcp_transports_lifecycle
title: MCP 三传输、连接生命周期与串行调用
module: tools
version: "1.0"
source_revision: c04ce65
status: implemented
source_files:
  - cognition/cognition/mcp/config.py
  - cognition/cognition/mcp/connection.py
  - cognition/cognition/mcp/registry.py
  - cognition/tests/test_mcp_registry_fake.py
---

# MCP 三传输、连接生命周期与串行调用

## 业务目标

系统接入 stdio、SSE 和 Streamable HTTP MCP server，同时规避 MCP SDK 会话跨 task 使用和并发复用导致的 cancel-scope 错误。

## 执行流程

配置解析把别名归一为 `stdio|sse|streamable_http`，并把超时统一为秒。SSE/HTTP 使用常驻 worker task 持有已初始化会话，请求经队列由单消费者串行执行；stdio 每次调用在 per-server lock 内新建子进程会话，用完关闭。registry 在装配期发现并缓存工具。

## 关键数据结构

`McpServerConfig` 包含传输、command/args/env 或 URL/endpoint/headers、超时与开关。PersistentConnection 持有 queue、worker、ready future 和工具缓存；TransientConnection 持有 session factory 与串行锁。适配器保留 MCP inputSchema 并转成 StructuredTool。

## 失败场景

缺少 command 或 URL、非法 transport、非正超时会在解析时报错。单 server 预热失败进入 registry errors，其他 server 继续。调用超时或 MCP `isError` 会变成工具错误；持久会话断开时队列中的 pending future 会收到连接错误。

## 限制与消歧

这里的“串行”是同一 MCP server 内的调用约束，不代表整个 Agent 只能串行。不同 server 或本地工具仍可独立执行。stdio 没有连接池，每次 call 新建会话；SSE/HTTP 才复用常驻连接。真实外部 server 的稳定性需单独联调，仓库测试使用内存 MCP server。
