"""Write paths into nokken-web's Postgres schema.

Phase 3 PR 1 introduces the **forecast-sink** writer. Mirrors the
``queries`` package's shape: connection-agnostic functions and a
``connect_write`` helper for callers (CLI, scheduled job, hindcast
harness) that want a one-shot ``async with`` over the write-capable
pool.

The write-capable pool lives in ``nokken_forecasting.db.postgres``
under ``get_write_pool`` / ``close_write_pool``; it does **not** set
``default_transaction_read_only = on`` and is sourced from a separate
DSN (``POSTGRES_WRITE_DSN``) carrying the writer-scoped role on
production deploy units. The read-only pool used by the inspect CLI
and the query-layer readers stays untouched.
"""

from __future__ import annotations

from nokken_forecasting.writers._connection import connect_write
from nokken_forecasting.writers.forecasts import insert_forecasts

__all__ = [
    "connect_write",
    "insert_forecasts",
]
