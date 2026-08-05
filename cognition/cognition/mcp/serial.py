"""per-server 串行锁取用（纯逻辑）。

MCP 同步会话不可并发复用。stdio 临时连接用这里的 per-server `asyncio.Lock`
串行化（sse/http 持久连接改用单消费者 worker task 天然串行，见 connection.py）。
"""

from __future__ import annotations

import asyncio


class SerialLocks:
    """按 server 名分配互斥锁：同名返回同一把锁。"""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, server: str) -> asyncio.Lock:
        lock = self._locks.get(server)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[server] = lock
        return lock
