"""Write paths into nokken-web's Postgres schema.

Phase 3 PR 1 introduces the **forecast-sink** writer
(``insert_forecasts``). The writer is connection-agnostic and runs on
the single pool from ``nokken_forecasting.db.postgres``; callers
acquire a connection through the existing ``queries.connect`` helper.

Defense in depth: the pool sets
``default_transaction_read_only = on`` at session init, so query-side
callers can't accidentally write. The writer opts out per transaction
by opening ``conn.transaction(readonly=False)`` — the session default
snaps back on the next transaction. Production write privileges are
granted through the role on ``POSTGRES_DSN`` (one role per repo).
"""

from __future__ import annotations

from nokken_forecasting.writers.forecasts import insert_forecasts

__all__ = ["insert_forecasts"]
