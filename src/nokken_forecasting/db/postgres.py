"""asyncpg pool lifecycle.

Two pools live here:

* ``get_pool`` / ``close_pool`` — the read-only pool. Every connection
  it hands out starts its session with
  ``default_transaction_read_only = on`` (see ``_init_readonly``), so
  any INSERT / UPDATE / DELETE / DDL raises a Postgres
  read-only-transaction error (SQLSTATE 25006) regardless of the
  role's table privileges. The Phase-2 inspect CLI and the Phase-3b
  query-layer readers ride this pool.
* ``get_write_pool`` / ``close_write_pool`` — the write-capable pool
  for the forecast-sink path (Phase 3 PR 1+). No read-only init; the
  DSN is expected to carry a role scoped to the sink table
  (``nokken_forecast_writer`` per ``docs/phase3-scoping.md`` §5G).
  Sourced from ``POSTGRES_WRITE_DSN``; opening the pool fails fast if
  the env var is unset.

Each pool is created lazily on first use, owned by the active event
loop, and torn down by its matching ``close_*`` helper. Manual CLI
runs open-then-close per invocation; the eventual scheduled
forecast job (PR 2) holds them open for the service lifetime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg

from nokken_forecasting.config import get_settings

if TYPE_CHECKING:
    from asyncpg.pool import Pool

_POOL: Pool | None = None
_WRITE_POOL: Pool | None = None


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


async def get_write_pool() -> Pool:
    """Lazily create the write-capable pool from ``POSTGRES_WRITE_DSN``.

    Raises ``RuntimeError`` if the env var is unset — the read-only
    DSN is **never** reused for writes, since that pool would refuse
    any write at session level even with a write-privileged role.
    """
    global _WRITE_POOL
    if _WRITE_POOL is None:
        settings = get_settings()
        if not settings.postgres_write_dsn:
            raise RuntimeError(
                "POSTGRES_WRITE_DSN is not set; the forecast-sink path "
                "requires a write-capable DSN scoped to the writer role. "
                "Local dev / unit tests should not exercise this path."
            )
        _WRITE_POOL = await asyncpg.create_pool(
            settings.postgres_write_dsn,
            min_size=1,
            max_size=5,
        )
    return _WRITE_POOL


async def close_write_pool() -> None:
    global _WRITE_POOL
    if _WRITE_POOL is not None:
        await _WRITE_POOL.close()
        _WRITE_POOL = None
