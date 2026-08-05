"""MCP 连接/注册表契约测试：用 in-memory MCP server，不触网、不起子进程、不付费。

覆盖："我控制"的几点——预热+发现+缓存、per-server 串行、fail-soft、优雅关闭。
"""

from __future__ import annotations

import asyncio

from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session as memory_session

from cognition.mcp.config import parse_server
from cognition.mcp.connection import PersistentConnection
from cognition.mcp.registry import McpRegistry


def _build_server():
    """一个含 add + slow 的 in-memory MCP server；slow 记录并发以验证串行。"""
    server = FastMCP("fake")
    state = {"concurrent": 0, "max_concurrent": 0, "order": []}

    @server.tool()
    async def slow(i: int) -> str:  # noqa: ANN001
        state["concurrent"] += 1
        state["max_concurrent"] = max(state["max_concurrent"], state["concurrent"])
        state["order"].append(("start", i))
        await asyncio.sleep(0.02)
        state["order"].append(("end", i))
        state["concurrent"] -= 1
        return f"done-{i}"

    @server.tool()
    def add(a: int, b: int) -> int:  # noqa: ANN001
        return a + b

    return server, state


async def test_persistent_connection_preload_list_and_serial():
    server, state = _build_server()
    conn = PersistentConnection("fake", lambda: memory_session(server), timeout_s=10)
    await conn.ensure_started()

    tools = await conn.list_tools()
    assert {t.name for t in tools} == {"slow", "add"}  # 发现+缓存

    # 并发发起 4 个 slow：单消费者 worker → 全程 max_concurrent==1（串行）。
    results = await asyncio.gather(*[conn.call("slow", {"i": i}) for i in range(4)])
    assert len(results) == 4
    assert state["max_concurrent"] == 1
    # start/end 严格成对，不交错。
    order = state["order"]
    assert order[0::2] == [("start", i) for i in range(4)]
    assert order[1::2] == [("end", i) for i in range(4)]

    await conn.aclose()


def _mem_conn_factory(server):
    def factory(cfg, locks):  # noqa: ANN001
        return PersistentConnection(cfg.name, lambda: memory_session(server), timeout_s=10)
    return factory


class _FailingConn:
    def __init__(self, name):  # noqa: ANN001
        self.name = name

    async def ensure_started(self):
        raise RuntimeError("boom")

    async def list_tools(self):
        return []

    async def call(self, *a):  # noqa: ANN002
        raise RuntimeError("boom")

    async def aclose(self):
        return None


async def test_registry_failsoft_and_call_and_aclose():
    good_server, _ = _build_server()

    def factory(cfg, locks):  # noqa: ANN001
        if cfg.name == "bad":
            return _FailingConn("bad")
        return PersistentConnection(cfg.name, lambda: memory_session(good_server), timeout_s=10)

    reg = McpRegistry(connection_factory=factory)
    cfgs = [
        parse_server("good", {"transport": "sse", "url": "http://x"}),
        parse_server("bad", {"transport": "sse", "url": "http://y"}),
    ]
    tools = await reg.preload(cfgs)

    # fail-soft：good 的工具都在、命名空间化；bad 记入 errors、不影响其余。
    names = {t.name for t in tools}
    assert names == {"mcp__good__slow", "mcp__good__add"}
    assert "bad" in reg.errors and "boom" in reg.errors["bad"]

    add_tool = next(t for t in tools if t.name == "mcp__good__add")
    assert add_tool.metadata["provider"] == "mcp"
    out = await add_tool.ainvoke({"a": 1, "b": 2})  # 经 registry.call 串行执行
    assert "3" in out

    await reg.aclose()
