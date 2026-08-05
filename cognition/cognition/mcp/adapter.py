"""mcp.types.Tool → LangChain StructuredTool（I/O）。

- 工具名 namespacing（`mcp__{server}__{tool}`），保留 MCP 的 inputSchema 作为 args_schema 暴露给 LLM。
- coroutine 内调 `call_fn(server, tool, args)`（即 McpRegistry.call），把 CallToolResult 文本化；
  `isError` → raise ToolException（ToolNode 会转成 error 状态的 ToolMessage）。
- metadata.provider="mcp"，供装配期构建 provider_map，让事件带上 tool_provider。
"""

from __future__ import annotations

from typing import Any, Callable

from langchain_core.tools import BaseTool, StructuredTool, ToolException

from cognition.mcp.naming import namespaced

CallFn = Callable[[str, str, dict], Any]  # (server, tool, args) -> awaitable CallToolResult


def _result_text(result: Any) -> str:
    """把 CallToolResult 的 content 块拼成文本 observation。"""
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(str(text))
    if parts:
        return "\n".join(parts)
    sc = getattr(result, "structuredContent", None)
    return str(sc) if sc is not None else ""


def _make_tool(server: str, tool: Any, call_fn: CallFn, provider: str) -> BaseTool:
    full_name = namespaced(server, tool.name)
    tool_name = tool.name

    async def _coro(**kwargs: Any) -> str:
        result = await call_fn(server, tool_name, kwargs)
        text = _result_text(result)
        if getattr(result, "isError", False):
            raise ToolException(text or f"MCP 工具 {full_name} 返回错误")
        return text

    schema = tool.inputSchema or {"type": "object", "properties": {}}
    return StructuredTool(
        name=full_name,
        description=tool.description or full_name,
        args_schema=schema,
        coroutine=_coro,
        metadata={"provider": provider, "mcp_server": server, "mcp_tool": tool_name},
    )


def to_langchain_tools(
    server: str, mcp_tools: list[Any], call_fn: CallFn, *, provider: str = "mcp"
) -> list[BaseTool]:
    """把一个 server 的 MCP 工具列表转成 LangChain 工具列表。"""
    return [_make_tool(server, t, call_fn, provider) for t in mcp_tools]
