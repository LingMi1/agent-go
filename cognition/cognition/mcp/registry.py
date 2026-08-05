"""McpRegistry（编排）：装配期预热 + 工具发现缓存 + ensure_loaded 精确补热 + 优雅关闭。

- `preload(cfgs)`：逐 server 连接+发现，**fail-soft**（单 server 失败记入 errors、不影响其余），
  返回去重后的 LangChain 工具列表（工具名 namespaced，metadata.provider="mcp"）。
- `call(server, tool, args)`：经连接串行执行；缺失时 `ensure_loaded` 按 server 精确补热。
- `aclose()`：优雅关闭全部连接（worker task / 子进程）。
`connection_factory` 可注入，便于用 in-memory server 做契约测试（默认 make_connection）。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from langchain_core.tools import BaseTool

from cognition.mcp.adapter import to_langchain_tools
from cognition.mcp.connection import McpConnectionError, make_connection
from cognition.mcp.naming import dedup
from cognition.mcp.serial import SerialLocks

logger = logging.getLogger(__name__)

ConnectionFactory = Callable[[Any, SerialLocks], Any]  # (cfg, locks) -> connection


class McpRegistry:
    """MCP 运行时注册表（按 run 之上的进程级共享，装配期预热一次）。"""

    def __init__(self, *, provider: str = "mcp", connection_factory: ConnectionFactory | None = None) -> None:
        self._provider = provider
        self._make = connection_factory or make_connection
        self._locks = SerialLocks()
        self._conns: dict[str, Any] = {}
        self._cfgs: dict[str, Any] = {}
        self._errors: dict[str, str] = {}

    @property
    def errors(self) -> dict[str, str]:
        return dict(self._errors)

    async def preload(self, cfgs: list[Any]) -> list[BaseTool]:
        tools: list[BaseTool] = []
        for cfg in cfgs:
            self._cfgs[cfg.name] = cfg
            try:
                conn = self._make(cfg, self._locks)
                await conn.ensure_started()
                self._conns[cfg.name] = conn
                mcp_tools = await conn.list_tools()
                tools.extend(to_langchain_tools(cfg.name, mcp_tools, self.call, provider=self._provider))
            except Exception as exc:  # noqa: BLE001 — fail-soft：透出具体错误但不中断其余
                self._errors[cfg.name] = str(exc)
                logger.warning("MCP 预热失败 %s: %s", cfg.name, exc)
        return dedup(tools)

    async def ensure_loaded(self, servers: list[str]) -> None:
        for name in servers:
            if name in self._conns:
                continue
            cfg = self._cfgs.get(name)
            if cfg is None:
                continue
            conn = self._make(cfg, self._locks)
            await conn.ensure_started()
            self._conns[name] = conn

    async def call(self, server: str, tool: str, args: dict) -> Any:
        await self.ensure_loaded([server])
        conn = self._conns.get(server)
        if conn is None:
            raise McpConnectionError(f"MCP server 未加载: {server}（预热错误: {self._errors.get(server)}）")
        return await conn.call(tool, args)

    async def aclose(self) -> None:
        for conn in list(self._conns.values()):
            try:
                await conn.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.warning("MCP 关闭连接失败: %s", exc)
        self._conns.clear()
