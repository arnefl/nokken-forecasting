"""Single-gauge, multi-issue-time hindcast harness.

The harness is baseline-agnostic: any function with the signature

    (observations: pd.DataFrame, *, gauge_id: int, issue_time: pd.Timestamp,
     value_type: str, horizon_hours: int) -> list[ForecastRow]

drops in. Today's customers are
:func:`~nokken_forecasting.baselines.persistence.persistence_forecast` and
:func:`~nokken_forecasting.baselines.recession.recession_forecast`; PR 4
adds the linear-regression baseline and PR 5 adds GBT, and both will
ride this harness without modification.

Per ``docs/phase3-scoping.md`` Decisions (final), every row written by
one harness invocation shares one wall-clock ``model_run_at`` stamp,
distinguishing "this hindcast run" from live forecasts (whose
``model_run_at ≈ issue_time``) and from other hindcast runs against the
same baseline. The single shared stamp is a contract PR 6's
comparison report depends on.

Per-issue-time error isolation: if reading observations or running the
baseline raises for one ``issue_time``, the harness logs the failure
and continues. A bad issue-time does not abort the run. Writer
failures are treated symmetrically — the writer's own transaction
rolls back, the connection's session-level read-only-default snaps
back as soon as the transaction exits, and the next iteration starts
clean. Idempotency on the same ``(gauge_id, model_version,
issue_time, valid_time, value_type)`` tuple is whatever the writer
provides (``ON CONFLICT DO NOTHING``) — re-running a hindcast over
the same issue-times is a no-op and counts zero rows ``inserted``.

Logging: structured JSON lines via :mod:`nokken_forecasting.logging`.
The CLI dispatcher emits ``hindcast.start`` / ``hindcast.done``; this
module emits one ``hindcast.issue_time`` line per issue-time.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from nokken_forecasting.baselines.persistence import ForecastRow

_LOG = logging.getLogger("nokken_forecasting.hindcast.harness")


# A baseline is the pure row-builder function. The harness ignores any
# extra kwargs the function may accept; the keyword arguments listed
# here are the contract every baseline this harness drives must honour.
BaselineFn = Callable[..., "list[ForecastRow]"]

# Bound observations reader: caller closes over conn / gauge_id /
# value_type / lookback. Returns the observation frame the baseline
# expects (matching ``get_observations`` shape).
ObservationsReader = Callable[[pd.Timestamp], "Awaitable[pd.DataFrame]"]

# Bound writer: caller closes over conn. Returns the number of rows
# actually inserted (i.e. not conflict-suppressed).
WriterFn = Callable[
    [Sequence["ForecastRow"], datetime], "Awaitable[int]"
]


@dataclass(frozen=True)
class IssueTimeOutcome:
    """Outcome of running the baseline once at one issue-time.

    ``rows_attempted`` is the count the baseline emitted before the
    writer ran — useful to detect a baseline that returned fewer rows
    than ``horizon_hours`` for any reason. ``rows_inserted`` is the
    writer's reported count (post-``ON CONFLICT DO NOTHING``); on
    rerun this is zero even when the baseline produced a full batch.
    """

    issue_time: pd.Timestamp
    status: str  # "success" | "error"
    rows_attempted: int
    rows_inserted: int
    error: str | None = None


@dataclass(frozen=True)
class HindcastSummary:
    """Aggregate of one harness invocation across all issue-times.

    All outcomes share ``model_run_at``; PR 6's comparison report
    relies on that to filter ``forecasts`` rows to one hindcast run.
    """

    gauge_id: int
    model_run_at: datetime
    outcomes: tuple[IssueTimeOutcome, ...]

    @property
    def succeeded(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "success")

    @property
    def failed(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "error")

    @property
    def rows_attempted(self) -> int:
        return sum(o.rows_attempted for o in self.outcomes)

    @property
    def rows_inserted(self) -> int:
        return sum(o.rows_inserted for o in self.outcomes)


async def run_hindcast(
    baseline_fn: BaselineFn,
    *,
    gauge_id: int,
    issue_times: Sequence[pd.Timestamp],
    observations_reader: ObservationsReader,
    writer: WriterFn,
    model_run_at: datetime,
    horizon_hours: int = 168,
    value_type: str = "flow",
) -> HindcastSummary:
    """Run ``baseline_fn`` at each ``issue_time`` and write the rows.

    For each ``issue_time``:

    1. ``observations_reader(issue_time)`` fetches the observation
       frame the baseline needs (caller closes over the connection
       and decides the lookback).
    2. ``baseline_fn(observations, gauge_id=..., issue_time=...,
       value_type=..., horizon_hours=...)`` produces ``ForecastRow``
       objects.
    3. ``writer(rows, model_run_at)`` lands them.

    The single ``model_run_at`` is stamped on **every** row from this
    invocation — that is the contract PR 6's comparison report uses
    to tell hindcast runs apart. Any per-issue-time stamping would
    break the report and is not supported.

    ``model_run_at`` must be tz-aware; the writer rejects naive
    inputs but the harness checks early so a misconfigured caller
    fails before the first observation read.

    Returns a :class:`HindcastSummary`; per-issue-time errors land in
    ``outcomes`` rather than aborting the run.
    """
    if model_run_at.tzinfo is None:
        raise ValueError(
            "model_run_at must be tz-aware (UTC); got naive datetime"
        )
    if horizon_hours <= 0:
        raise ValueError(f"horizon_hours must be positive; got {horizon_hours}")

    outcomes: list[IssueTimeOutcome] = []
    for issue_time in issue_times:
        if issue_time.tzinfo is None:
            outcomes.append(
                IssueTimeOutcome(
                    issue_time=issue_time,
                    status="error",
                    rows_attempted=0,
                    rows_inserted=0,
                    error="ValueError: issue_time must be tz-aware (UTC)",
                )
            )
            _LOG.error(
                "hindcast.issue_time",
                extra={
                    "event": "hindcast.issue_time",
                    "gauge_id": gauge_id,
                    "issue_time": str(issue_time),
                    "status": "error",
                    "rows_attempted": 0,
                    "rows_inserted": 0,
                    "error": "ValueError: issue_time must be tz-aware (UTC)",
                },
            )
            continue
        outcomes.append(
            await _run_one_issue_time(
                baseline_fn,
                gauge_id=gauge_id,
                issue_time=issue_time,
                observations_reader=observations_reader,
                writer=writer,
                model_run_at=model_run_at,
                horizon_hours=horizon_hours,
                value_type=value_type,
            )
        )
    return HindcastSummary(
        gauge_id=gauge_id,
        model_run_at=model_run_at,
        outcomes=tuple(outcomes),
    )


async def _run_one_issue_time(
    baseline_fn: BaselineFn,
    *,
    gauge_id: int,
    issue_time: pd.Timestamp,
    observations_reader: ObservationsReader,
    writer: WriterFn,
    model_run_at: datetime,
    horizon_hours: int,
    value_type: str,
) -> IssueTimeOutcome:
    try:
        observations = await observations_reader(issue_time)
        rows = baseline_fn(
            observations,
            gauge_id=gauge_id,
            issue_time=issue_time,
            value_type=value_type,
            horizon_hours=horizon_hours,
        )
        inserted = await writer(rows, model_run_at)
    except Exception as exc:  # noqa: BLE001 - log + continue contract
        error = f"{type(exc).__name__}: {exc}"
        _LOG.error(
            "hindcast.issue_time",
            extra={
                "event": "hindcast.issue_time",
                "gauge_id": gauge_id,
                "issue_time": issue_time.isoformat(),
                "status": "error",
                "rows_attempted": 0,
                "rows_inserted": 0,
                "error": error,
            },
        )
        return IssueTimeOutcome(
            issue_time=issue_time,
            status="error",
            rows_attempted=0,
            rows_inserted=0,
            error=error,
        )
    _LOG.info(
        "hindcast.issue_time",
        extra={
            "event": "hindcast.issue_time",
            "gauge_id": gauge_id,
            "issue_time": issue_time.isoformat(),
            "status": "success",
            "rows_attempted": len(rows),
            "rows_inserted": inserted,
            "value_type": value_type,
            "horizon_hours": horizon_hours,
        },
    )
    return IssueTimeOutcome(
        issue_time=issue_time,
        status="success",
        rows_attempted=len(rows),
        rows_inserted=inserted,
    )
