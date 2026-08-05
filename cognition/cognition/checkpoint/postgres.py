"""Postgres checkpointer 构造（可恢复/中断/时间旅行）。

延迟/可选：纯逻辑测试不需要 PG。`build_checkpointer(dsn)` 打开一个 AsyncPostgresSaver
（基于连接池），并 .setup() 建表，然后返回 (saver, aclose) ——调用方负责在进程结束时
关闭连接池。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Awaitable, Callable, Tuple

if TYPE_CHECKING:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver


async def build_checkpointer(
    dsn: str,
) -> Tuple["AsyncPostgresSaver", Callable[[], Awaitable[None]]]:
    """打开并初始化一个 AsyncPostgresSaver。

    Returns:
        (saver, aclose)：saver 传入 build_react_graph(checkpointer=saver)；
        aclose 是关闭底层连接池的协程。
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg_pool import AsyncConnectionPool

    # autocommit + 关闭 prepared statement，以兼容 pgbouncer 等连接池场景。
    pool = AsyncConnectionPool(
        conninfo=dsn,
        max_size=10,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0},
    )
    await pool.open()
    saver = AsyncPostgresSaver(pool)
    await saver.setup()

    async def _aclose() -> None:
        await pool.close()

    return saver, _aclose
