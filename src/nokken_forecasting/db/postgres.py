"""asyncpg pool lifecycle.

One pool per process, created lazily on first use and torn down on
``close_pool()``. Manual CLI runs open-then-close per invocation; the
eventual scheduled forecast job (PR 2) holds it open for the service
lifetime.

Every connection handed out of this pool starts its session with
``default_transaction_read_only = on`` (see ``_init_readonly``). Reads
through the pool — Phase-2 inspect CLI, Phase-3b query-layer — inherit
the read-only default, so any INSERT / UPDATE / DELETE / DDL on those
paths raises a Postgres read-only-transaction error (SQLSTATE 25006)
regardless of role privileges.

The forecast-sink writer in ``nokken_forecasting.writers.forecasts``
opts out per transaction. asyncpg's ``Transaction`` only knows how
to emit ``READ ONLY`` (never ``READ WRITE``), so the writer opens a
plain ``conn.transaction()`` and immediately issues ``SET TRANSACTION
READ WRITE`` as its first statement. That overrides the session
default for the current transaction only; the next ``BEGIN`` on the
same connection inherits ``default_transaction_read_only = on`` again,
so adjacent reads stay defended.

Single role per repo: this pool's DSN role gates writes via the
operator's INSERT grant on ``forecasts``. The defense-in-depth
session-level read-only switch protects the query / inspect surfaces
from accidental writes; the role privileges protect everything else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg

from nokken_forecasting.config import get_settings

if TYPE_CHECKING:
    from asyncpg.pool import Pool

_POOL: Pool | None = None


async def _init_readonly(conn: asyncpg.Connection) -> None:
    # Session-level default; asyncpg invokes this once per new
    # pooled connection. Subsequent transactions on the connection
    # inherit it unless a caller explicitly opens
    # `conn.transaction(readonly=False)` (which the forecast writer
    # does — see writers/forecasts.py).
    await conn.execute("SET default_transaction_read_only = on")


async def get_pool() -> Pool:
    global _POOL
    if _POOL is None:
        settings = get_settings()
        _POOL = await asyncpg.create_pool(
            settings.postgres_dsn,
            min_size=1,
            max_size=5,
            init=_init_readonly,
        )
    return _POOL


async def close_pool() -> None:
    global _POOL
    if _POOL is not None:
        await _POOL.close()
        _POOL = None
