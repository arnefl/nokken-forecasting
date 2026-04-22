"""asyncpg pool lifecycle.

Mirrors `nokken-data/src/nokken_data/db/postgres.py`. One pool per
process, created lazily on first use and torn down on `close_pool()`.
The eventual forecast job (Phase 6) will open the pool at service start
and close it on shutdown; manual CLI runs open-then-close per
invocation. Callers reach the pool through `get_pool()`.

This repo's role is read-only against `observations` / `sections` /
`gauges` / `statistics` until the forecast-sink contract lands in
nokken-web (see `ROADMAP.md` Phase 2). The DSN is whatever
`POSTGRES_DSN` carries; for local dev that is the `nokken_ro` role
copied across from `nokken-web/.env` (see `CLAUDE.md`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg

from nokken_forecasting.config import get_settings

if TYPE_CHECKING:
    from asyncpg.pool import Pool

_POOL: Pool | None = None


async def get_pool() -> Pool:
    global _POOL
    if _POOL is None:
        settings = get_settings()
        _POOL = await asyncpg.create_pool(
            settings.postgres_dsn,
            min_size=1,
            max_size=5,
        )
    return _POOL


async def close_pool() -> None:
    global _POOL
    if _POOL is not None:
        await _POOL.close()
        _POOL = None
