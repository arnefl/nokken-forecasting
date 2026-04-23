"""asyncpg pool lifecycle with DB-level read-only enforcement.

Mirrors `nokken-data/src/nokken_data/db/postgres.py`. One pool per
process, created lazily on first use and torn down on `close_pool()`.
The eventual forecast job (Phase 6) will open the pool at service start
and close it on shutdown; manual CLI runs open-then-close per
invocation. Callers reach the pool through `get_pool()`.

Every connection handed out of this pool starts its session with
``default_transaction_read_only = on`` (see ``_init_readonly``). This
makes any INSERT / UPDATE / DELETE / DDL raise a Postgres
read-only-transaction error (SQLSTATE 25006) regardless of the role's
table privileges — defense in depth over the ``nokken_ro`` role used
locally. Phase 2's inspection CLI and the eventual Phase 3 query layer
both ride this pool.

The forecast-sink write path (Phase 6) will need a writer pool; when
it lands, it builds its own `create_pool` call without the read-only
init (and a role scoped to the sink table), leaving this pool as the
read-only consumer path.
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
    # inherit it unless a caller explicitly flips the setting.
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
