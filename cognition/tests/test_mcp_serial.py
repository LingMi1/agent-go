"""per-server 串行锁取用（纯逻辑）。"""

from __future__ import annotations

import asyncio

from cognition.mcp.serial import SerialLocks


def test_same_server_returns_same_lock():
    locks = SerialLocks()
    a = locks.get("github")
    b = locks.get("github")
    assert a is b
    assert isinstance(a, asyncio.Lock)


def test_different_servers_return_different_locks():
    locks = SerialLocks()
    assert locks.get("github") is not locks.get("gitlab")
