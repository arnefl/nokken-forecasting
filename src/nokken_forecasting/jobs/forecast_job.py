"""Daily forecast job — the unattended path the systemd timer invokes.

Iterates over the ``FORECAST_GAUGES`` constant, runs the persistence
baseline for each, and writes the rows into ``forecasts``. The same
read → baseline → write building blocks the manual
``forecast persistence`` CLI uses, so an operator debugging a tick can
reproduce it byte-for-byte from the shell.

Idempotency: relies on the writer's deterministic ON CONFLICT DO NOTHING
contract (see ``writers.forecasts``). Two ticks landing inside the same
hour produce the same ``issue_time`` (top-of-hour floor) and therefore
the same uniqueness key, so the second tick's rows are no-ops with the
original ``model_run_at`` preserved.

Logging: structured JSON lines via :mod:`nokken_forecasting.logging`.
The operator greps:

* ``event=forecast_job.start`` — one line per tick, listing the gauges
  about to run.
* ``event=forecast_job.gauge`` — one line per gauge with
  ``status=success|error`` and ``rows_written``.
* ``event=forecast_job.done`` — the summary line carrying total /
  succeeded / failed / rows_written.

Error policy: a failure on one gauge does not abort the run — log the
error and continue. The job exits non-zero only when **every** gauge
failed (today's single-gauge case collapses cleanly: any failure exits
non-zero, which is correct).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pandas as pd

from nokken_forecasting.baselines.persistence import (
    HORIZON_HOURS,
    persistence_forecast,
)
from nokken_forecasting.queries import connect, get_observations
from nokken_forecasting.writers import insert_forecasts

if TYPE_CHECKING:
    import asyncpg

_LOG = logging.getLogger("nokken_forecasting.jobs.forecast_job")

# Gauges this sprint's scheduled tick covers. Faukstad (gauge id 12) is
# the only forecast target per ``docs/phase3-scoping.md`` Decisions
# (final). Multi-gauge fan-out is post-Phase-3 — when it lands, this
# graduates to a config table or an env-driven list, not a generalised
# "any baseline against any gauge" framework. See PR 2 prompt §2.
FORECAST_GAUGES: tuple[int, ...] = (12,)

# Look-back window over which observations are read to seed each
# persistence baseline. Mirrors ``cli._PERSISTENCE_LOOKBACK_HOURS`` so
# both the unattended path and the manual operator path see the same
# input window for the same gauge / issue_time.
_PERSISTENCE_LOOKBACK_HOURS = 24 * 7


@dataclass(frozen=True)
class JobOutcome:
    """Outcome of running the persistence baseline for one gauge."""

    gauge_id: int
    status: str  # "success" | "error"
    rows_written: int
    error: str | None = None


@dataclass(frozen=True)
class JobSummary:
    """Aggregate outcome of one forecast-job tick.

    ``exit_code`` is 0 when at least one gauge succeeded (or the gauge
    list was empty), non-zero when every attempted gauge failed.
    """

    issue_time: pd.Timestamp
    outcomes: tuple[JobOutcome, ...]
    exit_code: int

    @property
    def succeeded(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "success")

    @property
    def failed(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "error")

    @property
    def rows_written(self) -> int:
        return sum(o.rows_written for o in self.outcomes)


def _resolve_issue_time(issue_time: pd.Timestamp | None) -> pd.Timestamp:
    if issue_time is None:
        # Floor to the top of the hour so two ticks fired inside the same
        # hour collide on the writer's deterministic uniqueness key and
        # the second tick is a no-op. Daily timers will fall on
        # 00:00 UTC anyway; this guards Persistent=true catch-up runs and
        # ad-hoc operator invocations equally.
        return pd.Timestamp.now(tz="UTC").floor("h")
    if issue_time.tzinfo is None:
        raise ValueError("issue_time must be tz-aware (UTC); got naive Timestamp")
    return issue_time


async def _forecast_one_gauge(
    conn: asyncpg.Connection,
    *,
    gauge_id: int,
    issue_time: pd.Timestamp,
    value_type: str,
    horizon_hours: int,
    model_run_at: datetime,
) -> JobOutcome:
    try:
        lookback_start = issue_time - pd.Timedelta(hours=_PERSISTENCE_LOOKBACK_HOURS)
        # Push the upper bound a second past `issue_time` so an
        # observation stamped exactly at `issue_time` lands inside the
        # half-open read window. Mirrors the manual CLI path.
        lookback_end = issue_time + pd.Timedelta(seconds=1)
        obs = await get_observations(
            conn,
            gauge_id=gauge_id,
            start=lookback_start,
            end=lookback_end,
            value_type=value_type,
        )
        rows = persistence_forecast(
            obs,
            gauge_id=gauge_id,
            issue_time=issue_time,
            value_type=value_type,
            horizon_hours=horizon_hours,
        )
        inserted = await insert_forecasts(conn, rows, model_run_at=model_run_at)
    except Exception as exc:  # noqa: BLE001 - surface every failure shape
        _LOG.error(
            "forecast_job.gauge",
            extra={
                "event": "forecast_job.gauge",
                "gauge_id": gauge_id,
                "status": "error",
                "rows_written": 0,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        return JobOutcome(
            gauge_id=gauge_id,
            status="error",
            rows_written=0,
            error=f"{type(exc).__name__}: {exc}",
        )
    _LOG.info(
        "forecast_job.gauge",
        extra={
            "event": "forecast_job.gauge",
            "gauge_id": gauge_id,
            "status": "success",
            "rows_written": inserted,
            "issue_time": issue_time.isoformat(),
            "value_type": value_type,
            "horizon_hours": horizon_hours,
        },
    )
    return JobOutcome(gauge_id=gauge_id, status="success", rows_written=inserted)


async def run_forecast_job(
    *,
    issue_time: pd.Timestamp | None = None,
    value_type: str = "flow",
    horizon_hours: int = HORIZON_HOURS,
    gauges: Sequence[int] = FORECAST_GAUGES,
    conn: asyncpg.Connection | None = None,
) -> JobSummary:
    """Run the persistence baseline for every gauge in ``gauges``.

    ``conn`` is optional: when ``None`` (the production path) the job
    acquires a connection from the read-only-default pool in
    ``db.postgres`` via ``queries.connect``. The writer opts out of the
    session-level read-only default per transaction. Tests inject a
    ``seeded_conn`` directly to short-circuit the pool.

    Returns a :class:`JobSummary`. The CLI dispatcher relays
    ``summary.exit_code`` to ``SystemExit`` so the systemd unit reflects
    success / failure cleanly in journald.
    """
    resolved_issue_time = _resolve_issue_time(issue_time)
    model_run_at = datetime.now(UTC)
    gauges_tuple = tuple(gauges)

    _LOG.info(
        "forecast_job.start",
        extra={
            "event": "forecast_job.start",
            "issue_time": resolved_issue_time.isoformat(),
            "value_type": value_type,
            "horizon_hours": horizon_hours,
            "gauges": list(gauges_tuple),
        },
    )

    outcomes: list[JobOutcome] = []
    if conn is None:
        async with connect() as acquired:
            for gauge_id in gauges_tuple:
                outcomes.append(
                    await _forecast_one_gauge(
                        acquired,
                        gauge_id=gauge_id,
                        issue_time=resolved_issue_time,
                        value_type=value_type,
                        horizon_hours=horizon_hours,
                        model_run_at=model_run_at,
                    )
                )
    else:
        for gauge_id in gauges_tuple:
            outcomes.append(
                await _forecast_one_gauge(
                    conn,
                    gauge_id=gauge_id,
                    issue_time=resolved_issue_time,
                    value_type=value_type,
                    horizon_hours=horizon_hours,
                    model_run_at=model_run_at,
                )
            )

    # Empty gauges list exits 0 (a no-op tick is not a failure). With a
    # non-empty list we fail only when *every* gauge errored.
    failed = sum(1 for o in outcomes if o.status == "error")
    exit_code = 1 if outcomes and failed == len(outcomes) else 0
    summary = JobSummary(
        issue_time=resolved_issue_time,
        outcomes=tuple(outcomes),
        exit_code=exit_code,
    )
    _LOG.info(
        "forecast_job.done",
        extra={
            "event": "forecast_job.done",
            "issue_time": resolved_issue_time.isoformat(),
            "total": len(outcomes),
            "succeeded": summary.succeeded,
            "failed": summary.failed,
            "rows_written": summary.rows_written,
            "exit_code": exit_code,
        },
    )
    return summary
