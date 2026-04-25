"""Connection helper for query-layer callers.

Readers themselves are connection-agnostic — they take an
``asyncpg.Connection`` and never construct their own. This helper
exists for callers (CLI, scripts) that want a one-shot ``async with``
pattern over the read-only pool from
``nokken_forecasting.db.postgres``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from nokken_forecasting.db.postgres import close_pool, get_pool

if TYPE_CHECKING:
    import asyncpg


@asynccontextmanager
async def connect(*, close: bool = False) -> AsyncIterator[asyncpg.Connection]:
    """Acquire a connection from the read-only pool.

    Yields an ``asyncpg.Connection`` from ``get_pool()`` (the read-only
    pool used by the ``inspect`` CLI). The connection is released back
    to the pool on exit.

    ``close=True`` tears down the module-level pool after release —
    useful for short-lived CLI invocations that own the process. The
    Phase 6 forecast job will keep the pool open for the service
    lifetime and never pass ``close=True``.
    """
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            yield conn
    finally:
        if close:
            await close_pool()
