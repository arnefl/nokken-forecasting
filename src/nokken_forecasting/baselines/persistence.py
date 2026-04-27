"""Persistence baseline.

Holds the last observed value flat over the forecast horizon —
``Q(t) = Q(issue_time)`` for ``t ∈ {issue_time + 1h, …, issue_time + Nh}``.
Deterministic single trace; no probabilistic component, so every emitted
row carries ``quantile = None``.

Per ``docs/phase3-scoping.md`` Decisions (final): horizon is 7 days
hourly (168 rows per call); ``model_version = 'persistence_v1'``;
Faukstad (gauge id 12) is the only gauge this sprint exercises, but
the function is gauge-id-agnostic so PR 3's hindcast harness can reuse
it across the test window.

Pure: takes a DataFrame of observations and a few scalars, returns a
list of ``ForecastRow`` payload objects. ``model_run_at`` is stamped
by the writer (``writers.forecasts.insert_forecasts``), not here, so
the same row-building logic serves both the live path and the
hindcast harness without branching.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

MODEL_VERSION = "persistence_v1"
HORIZON_HOURS = 168


@dataclass(frozen=True)
class ForecastRow:
    """One row of forecast output destined for ``forecasts``.

    ``issue_time`` and ``valid_time`` are tz-aware UTC ``Timestamp``
    values (the writer naive-izes them at the wire boundary, mirroring
    the readers in ``nokken_forecasting.queries``). ``quantile`` is
    ``None`` for the deterministic point forecast and a value in
    ``[0, 1]`` for a probabilistic row. ``model_run_at`` is **not** on
    this dataclass — the writer stamps it on every row from a single
    argument so live runs and hindcasts share the same payload shape.
    """

    issue_time: pd.Timestamp
    valid_time: pd.Timestamp
    gauge_id: int
    value_type: str
    quantile: float | None
    value: float
    model_version: str


def persistence_forecast(
    observations: pd.DataFrame,
    *,
    gauge_id: int,
    issue_time: pd.Timestamp,
    value_type: str = "flow",
    horizon_hours: int = HORIZON_HOURS,
) -> list[ForecastRow]:
    """Forecast = last observed value held flat for ``horizon_hours``.

    ``observations`` is the DataFrame returned by ``get_observations`` —
    columns ``time, gauge_id, value_type, value`` with ``time`` tz-aware
    UTC. The function selects rows matching ``value_type`` whose ``time``
    is at or before ``issue_time`` and seeds every forecast row with
    the most recent such row's ``value``.

    Raises ``ValueError`` if ``issue_time`` is naive, ``horizon_hours``
    is non-positive, or the filtered series is empty (no observation
    available at or before ``issue_time``).
    """
    if issue_time.tzinfo is None:
        raise ValueError("issue_time must be tz-aware (UTC); got naive Timestamp")
    if horizon_hours <= 0:
        raise ValueError(f"horizon_hours must be positive; got {horizon_hours}")
    if observations.empty:
        raise ValueError(
            f"no observations available for gauge {gauge_id} at or before {issue_time}"
        )

    series = observations[observations["value_type"] == value_type]
    series = series[series["time"] <= issue_time]
    if series.empty:
        raise ValueError(
            f"no '{value_type}' observations for gauge {gauge_id} "
            f"at or before {issue_time}"
        )

    seed = float(series.sort_values("time").iloc[-1]["value"])
    return [
        ForecastRow(
            issue_time=issue_time,
            valid_time=issue_time + pd.Timedelta(hours=h),
            gauge_id=gauge_id,
            value_type=value_type,
            quantile=None,
            value=seed,
            model_version=MODEL_VERSION,
        )
        for h in range(1, horizon_hours + 1)
    ]
