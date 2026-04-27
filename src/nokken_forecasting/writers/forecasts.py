"""Writer for the ``forecasts`` time-series table.

The Postgres table (nokken-web migrations 003 + 011) carries one row
per (issue_time, valid_time, gauge_id, value_type, model_version)
tuple for deterministic forecasts and an additional ``quantile``
column for probabilistic rows. ``model_run_at`` (migration 011) is
the wall-clock execution stamp distinguishing live forecasts
(``≈ issue_time``) from hindcasts (``≫ issue_time``); it is audit-only
and not part of any unique index.

Time-column wire shapes:

* ``issue_time`` and ``valid_time`` are ``TIMESTAMP WITHOUT TIME ZONE``
  (interpreted as naive UTC across the three sibling repos).
* ``model_run_at`` is ``TIMESTAMPTZ``.

Timezone contract — applies to all three columns and to every public
input on this writer (``model_run_at`` here, ``issue_time`` /
``valid_time`` on each ``ForecastRow``): **lenient on tz-aware,
strict on naive.** Any tz-aware input is converted to UTC at the wire
boundary; naive inputs are rejected with ``ValueError`` so a silent
off-by-one tz mismatch cannot creep in. ``to_db_timestamp`` (shared
with the readers) enforces the contract for the TIMESTAMP-WITHOUT-TZ
columns; ``model_run_at`` is checked inline below.

Idempotency: ``ON CONFLICT … DO NOTHING`` against the deterministic
partial-unique index from migration 003. Rerunning the same
(issue_time, valid_time, gauge_id, value_type, model_version) tuple
is a no-op by design — the original ``model_run_at`` is preserved so
the audit trail reflects when each row was *first* written, not when
it was most recently re-attempted. Any future change to ``DO UPDATE
SET model_run_at = NOW()`` would deliberately fail the rerun-no-op
integration test and force a contract review. The probabilistic
idempotency lane (separate partial-unique index keyed on the same
columns plus ``quantile``) is unused by the persistence baseline and
lands when a quantile-emitting baseline does (PR 5, GBT).
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
    # DO NOTHING (not DO UPDATE): rerun is a no-op by design so the
    # audit trail's `model_run_at` reflects when each row was first
    # written. See the module docstring's "Idempotency" paragraph.
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
    historical. ``model_run_at`` must be tz-aware (any UTC offset is
    accepted and converted to UTC); naive datetimes raise.

    The whole batch lands inside a single read-write transaction
    (``conn.transaction(readonly=False)`` overrides the session-level
    ``default_transaction_read_only = on`` default for this transaction
    only — adjacent reads on the same connection stay defended).
    Conflicting rows (same model_version at the same (issue, valid,
    gauge, value_type)) are skipped via ``ON CONFLICT DO NOTHING``.

    All rows must carry ``quantile=None`` — this writer covers the
    deterministic lane only.
    """
    if not rows:
        return 0
    if model_run_at.tzinfo is None:
        raise ValueError(
            "model_run_at must be tz-aware (UTC); got naive datetime"
        )
    model_run_at_utc = model_run_at.astimezone(UTC)
    inserted = 0
    async with conn.transaction(readonly=False):
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
