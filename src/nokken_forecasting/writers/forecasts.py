"""Writer for the ``forecasts`` time-series table.

The Postgres table (nokken-web migrations 003 + 011) carries one row
per (issue_time, valid_time, gauge_id, value_type, model_version)
tuple for deterministic forecasts and an additional ``quantile``
column for probabilistic rows. ``model_run_at`` (migration 011) is
the wall-clock execution stamp distinguishing live forecasts
(``≈ issue_time``) from hindcasts (``≫ issue_time``); it is audit-only
and not part of any unique index.

Time-column wire shape: ``issue_time`` and ``valid_time`` are
``TIMESTAMP WITHOUT TIME ZONE`` (interpreted as naive UTC across the
three sibling repos), so tz-aware UTC inputs are converted to naive
UTC before binding. ``model_run_at`` is ``TIMESTAMPTZ`` and is bound
tz-aware.

Idempotency mirrors the deterministic-row writer in
``nokken-data``'s weather-forecast pipelines: ``ON CONFLICT
(issue_time, valid_time, gauge_id, value_type, model_version) WHERE
quantile IS NULL DO NOTHING`` against the partial-unique index from
migration 003. Re-running the same model run is therefore a no-op.
The probabilistic-row idempotency lane (separate partial-unique
index on the same columns plus ``quantile``) is unused by the
persistence baseline (which is deterministic) and lands when a
quantile-emitting baseline does (PR 5, GBT).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from nokken_forecasting.queries._frame import to_db_timestamp

if TYPE_CHECKING:
    import asyncpg

    from nokken_forecasting.baselines.persistence import ForecastRow


_DETERMINISTIC_INSERT_SQL = (
    "INSERT INTO forecasts "
    "(issue_time, valid_time, gauge_id, value_type, quantile, value, "
    "model_version, model_run_at) "
    "VALUES ($1, $2, $3, $4, NULL, $5, $6, $7) "
    "ON CONFLICT (issue_time, valid_time, gauge_id, value_type, model_version) "
    "WHERE quantile IS NULL DO NOTHING"
)


async def insert_forecasts(
    conn: asyncpg.Connection,
    rows: Sequence[ForecastRow],
    *,
    model_run_at: datetime,
) -> int:
    """Insert a batch of deterministic forecast rows; return the count landed.

    Every row in ``rows`` is stamped with the same ``model_run_at`` —
    callers control the column from one argument. Live runs pass
    ``datetime.now(UTC)``; PR 3's hindcast harness will pass the
    wall-clock time of the harness invocation, distinguishing the
    rows from live forecasts even though their ``issue_time`` is
    historical. ``model_run_at`` must be tz-aware UTC.

    The whole batch lands inside a single transaction so a partial
    insert is impossible. Conflicting rows (same model_version at the
    same (issue, valid, gauge, value_type)) are skipped via
    ``ON CONFLICT DO NOTHING``; this matters for idempotent re-runs
    of a scheduled cycle and for the hindcast harness when re-issuing
    a window.

    All rows must carry ``quantile=None`` — this writer covers the
    deterministic lane only. Probabilistic rows land via a separate
    code path (introduced when PR 5's GBT baseline ships native
    quantiles).
    """
    if not rows:
        return 0
    if model_run_at.tzinfo is None:
        raise ValueError(
            "model_run_at must be tz-aware (UTC); got naive datetime"
        )
    model_run_at_utc = model_run_at.astimezone(UTC)
    inserted = 0
    async with conn.transaction():
        for row in rows:
            if row.quantile is not None:
                raise ValueError(
                    "insert_forecasts handles deterministic rows only "
                    "(quantile=None); got "
                    f"quantile={row.quantile!r}"
                )
            status = await conn.execute(
                _DETERMINISTIC_INSERT_SQL,
                to_db_timestamp(row.issue_time),
                to_db_timestamp(row.valid_time),
                row.gauge_id,
                row.value_type,
                float(row.value),
                row.model_version,
                model_run_at_utc,
            )
            if status.startswith("INSERT 0 1"):
                inserted += 1
    return inserted
