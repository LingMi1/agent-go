"""MCP 工具名命名空间与去重（纯逻辑）。

原项目 ToolCollection 本地优先，导致同名 MCP 工具被静默隐藏。这里用
`mcp__{server}__{tool}` 命名空间根除碰撞：跨 server 同原始名不再冲突，
本地工具与 MCP 工具也不会撞名。
"""

from __future__ import annotations

from typing import Protocol, TypeVar


def namespaced(server: str, tool: str) -> str:
    """把 (server, tool) 拼成全局唯一的工具名。"""
    return f"mcp__{server}__{tool}"


class _Named(Protocol):
    name: str


T = TypeVar("T", bound=_Named)


def dedup(items: list[T]) -> list[T]:
    """按 `.name` 去重：同名后者覆盖前者，位置以首次出现为准，其余顺序稳定。"""
    order: list[str] = []
    latest: dict[str, T] = {}
    for it in items:
        key = it.name
        if key not in latest:
            order.append(key)
        latest[key] = it
    return [latest[k] for k in order]
