"""Connection helper for write-path callers.

Mirrors ``nokken_forecasting.queries._connection``. The writer
function in ``forecasts.py`` is connection-agnostic; this helper
wraps the write-capable pool from ``nokken_forecasting.db.postgres``
in an ``async with connect_write() as conn`` pattern for CLI and
script callers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from nokken_forecasting.db.postgres import close_write_pool, get_write_pool

if TYPE_CHECKING:
    import asyncpg


@asynccontextmanager
async def connect_write(*, close: bool = False) -> AsyncIterator[asyncpg.Connection]:
    """Acquire a connection from the write-capable pool.

    ``close=True`` tears down the module-level write pool after the
    connection is released — useful for short-lived CLI invocations
    that own the process. The Phase-2 scheduled forecast job will
    keep the pool open for the service lifetime and never pass
    ``close=True``.
    """
    pool = await get_write_pool()
    try:
        async with pool.acquire() as conn:
            yield conn
    finally:
        if close:
            await close_write_pool()
