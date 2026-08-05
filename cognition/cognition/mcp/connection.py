"""MCP server 连接（I/O）：两类实现，均把"会话的 enter/use/exit"约束在同一个 task 内。

- **PersistentConnection**（sse / streamable_http）：一个常驻 worker task 持有 `ClientSession`，
  调用经 `asyncio.Queue` 投递、单消费者串行执行。单 worker = 天然 per-server 串行；且
  enter/use/exit 都在 worker task 内，规避 mcp SDK（anyio）的 "cancel scope in different task" 崩溃。
- **TransientConnection**（stdio）：每次 call 在 per-server 锁下新建子进程会话、用完即关；
  enter/exit 同在一次 call 协程内，同样不跨 task。

`SessionFactory` 约定：返回一个 async 上下文管理器，yield **已 initialize** 的 `ClientSession`
（真实传输的工厂负责 initialize；in-memory 测试助手本身即已初始化）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import timedelta
from typing import Any, AsyncContextManager, Callable

logger = logging.getLogger(__name__)

# worker 关闭哨兵。
_CLOSE = object()

SessionFactory = Callable[[], AsyncContextManager[Any]]  # yield 已初始化的 ClientSession


class McpConnectionError(RuntimeError):
    """MCP 连接/调用失败。"""


class PersistentConnection:
    """常驻会话 + 单消费者 worker：天然 per-server 串行，规避跨 task cancel-scope。"""

    def __init__(self, name: str, factory: SessionFactory, *, timeout_s: float = 30.0) -> None:
        self.name = name
        self._factory = factory
        self._timeout = timeout_s
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self._ready: asyncio.Future | None = None
        self._tools: list[Any] = []

    async def ensure_started(self) -> None:
        if self._task is not None:
            await self._ready  # 复用；若已失败会重新抛出
            return
        loop = asyncio.get_running_loop()
        self._ready = loop.create_future()
        self._queue = asyncio.Queue()
        self._task = loop.create_task(self._worker(), name=f"mcp-worker-{self.name}")
        await self._ready

    async def _worker(self) -> None:
        assert self._ready is not None and self._queue is not None
        try:
            async with self._factory() as session:
                resp = await asyncio.wait_for(session.list_tools(), self._timeout)
                self._tools = list(resp.tools)
                if not self._ready.done():
                    self._ready.set_result(None)
                while True:
                    cmd = await self._queue.get()
                    if cmd is _CLOSE:
                        break
                    tool, args, fut = cmd
                    if fut.cancelled():
                        continue
                    try:
                        res = await asyncio.wait_for(session.call_tool(tool, args), self._timeout)
                        if not fut.done():
                            fut.set_result(res)
                    except Exception as exc:  # noqa: BLE001 — 单次调用失败不拖垮 worker
                        if not fut.done():
                            fut.set_exception(exc)
        except Exception as exc:  # noqa: BLE001 — 启动/会话级失败
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(McpConnectionError(f"MCP {self.name} 会话失败: {exc}"))
            self._drain_pending(exc)

    def _drain_pending(self, exc: Exception) -> None:
        if self._queue is None:
            return
        while not self._queue.empty():
            cmd = self._queue.get_nowait()
            if cmd is _CLOSE:
                continue
            _, _, fut = cmd
            if not fut.done():
                fut.set_exception(McpConnectionError(f"MCP {self.name} 会话已断开: {exc}"))

    async def list_tools(self) -> list[Any]:
        return list(self._tools)

    async def call(self, tool: str, args: dict | None) -> Any:
        if self._task is None:
            await self.ensure_started()
        assert self._queue is not None
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        await self._queue.put((tool, args or {}, fut))
        return await fut

    async def aclose(self) -> None:
        if self._task is None:
            return
        if self._queue is not None:
            with contextlib.suppress(Exception):
                self._queue.put_nowait(_CLOSE)
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=5.0)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            self._task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self._task
        finally:
            self._task = None


class TransientConnection:
    """stdio：每次 call 在 per-server 锁下新建子进程会话、用完即关。"""

    def __init__(self, name: str, factory: SessionFactory, lock: asyncio.Lock, *, timeout_s: float = 30.0) -> None:
        self.name = name
        self._factory = factory
        self._lock = lock
        self._timeout = timeout_s
        self._tools: list[Any] = []

    async def ensure_started(self) -> None:
        async with self._lock:
            async with self._factory() as session:
                resp = await asyncio.wait_for(session.list_tools(), self._timeout)
                self._tools = list(resp.tools)

    async def list_tools(self) -> list[Any]:
        return list(self._tools)

    async def call(self, tool: str, args: dict | None) -> Any:
        async with self._lock:  # per-server 串行
            async with self._factory() as session:
                return await asyncio.wait_for(session.call_tool(tool, args or {}), self._timeout)

    async def aclose(self) -> None:
        return None


# ————————————————————————————————————————————————————————————————
# 真实传输的会话工厂（惰性 import；单测/纯逻辑不触发）
# ————————————————————————————————————————————————————————————————
def _stdio_factory(cfg: Any) -> SessionFactory:
    @contextlib.asynccontextmanager
    async def cm():  # type: ignore[no-untyped-def]
        from mcp import ClientSession, StdioServerParameters, stdio_client

        params = StdioServerParameters(
            command=cfg.command, args=list(cfg.args), env=dict(cfg.env) or None
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=cfg.request_timeout_s)
            ) as session:
                await session.initialize()
                yield session

    return cm


def _http_factory(cfg: Any) -> SessionFactory:
    @contextlib.asynccontextmanager
    async def cm():  # type: ignore[no-untyped-def]
        from mcp import ClientSession

        url = (cfg.base_uri or "").rstrip("/") + (cfg.endpoint or "")
        headers = dict(cfg.headers) or None
        timeout = timedelta(seconds=cfg.request_timeout_s)
        if cfg.transport == "sse":
            from mcp.client.sse import sse_client

            async with sse_client(url, headers=headers) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timeout) as session:
                    await session.initialize()
                    yield session
        else:  # streamable_http
            from mcp.client.streamable_http import streamablehttp_client

            async with streamablehttp_client(url, headers=headers) as (read, write, _get_sid):
                async with ClientSession(read, write, read_timeout_seconds=timeout) as session:
                    await session.initialize()
                    yield session

    return cm


def make_connection(cfg: Any, locks: Any):
    """按传输类型建连接：stdio→Transient，sse/streamable_http→Persistent。"""
    if cfg.transport == "stdio":
        return TransientConnection(
            cfg.name, _stdio_factory(cfg), locks.get(cfg.name), timeout_s=cfg.request_timeout_s
        )
    return PersistentConnection(cfg.name, _http_factory(cfg), timeout_s=cfg.request_timeout_s)
