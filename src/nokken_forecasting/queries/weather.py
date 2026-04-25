"""Readers for the gauge-keyed weather tables.

Both tables are written by nokken-data and re-keyed to ``gauge_id``
in nokken-web migration 008 (see ``SCHEMA_COMPAT.md``). The
``basin_version`` column stamps which ``basins.version`` polygon
produced each row; it is nullable for rows whose source is not
basin-polygon-derived.

``weather_observations`` schema
-------------------------------

================  =========================  ========
Column            DataFrame dtype            NULL?
================  =========================  ========
``time``          ``datetime64[ns, UTC]``    no
``gauge_id``      ``int64``                  no
``variable``      ``object``                 no
``value``         ``float64``                no
``source``        ``object``                 no
``basin_version`` ``Int64``                  yes
================  =========================  ========

``weather_forecasts`` schema
----------------------------

================  =========================  ========
Column            DataFrame dtype            NULL?
================  =========================  ========
``issue_time``    ``datetime64[ns, UTC]``    no
``valid_time``    ``datetime64[ns, UTC]``    no
``gauge_id``      ``int64``                  no
``variable``      ``object``                 no
``value``         ``float64``                no
``source``        ``object``                 no
``quantile``      ``float64``                yes (NaN = deterministic)
``basin_version`` ``Int64``                  yes
================  =========================  ========

Half-open ``[start, end)`` is used wherever a time window is
specified. Inputs must be tz-aware UTC ``Timestamp`` values.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

from nokken_forecasting.queries._frame import rows_to_frame, to_db_timestamp

if TYPE_CHECKING:
    import asyncpg


_OBS_COLUMNS: list[str] = [
    "time",
    "gauge_id",
    "variable",
    "value",
    "source",
    "basin_version",
]

_FCST_COLUMNS: list[str] = [
    "issue_time",
    "valid_time",
    "gauge_id",
    "variable",
    "value",
    "source",
    "quantile",
    "basin_version",
]


async def get_weather_observations(
    conn: asyncpg.Connection,
    gauge_id: int,
    start: pd.Timestamp,
    end: pd.Timestamp,
    variables: list[str] | None = None,
    source: str | None = None,
) -> pd.DataFrame:
    """Historical hourly forcing for one gauge over ``[start, end)``.

    Returns rows in chronological order, then by variable, then by
    source. No resampling, no imputation. ``variables`` accepts a
    subset of the five-variable vocabulary (``'temperature'``,
    ``'precipitation'``, ``'shortwave'``, ``'relative_humidity'``,
    ``'wind_speed'``); ``source`` filters to a single upstream
    product (e.g. ``'met_nordic_analysis_v4'`` vs
    ``'met_nordic_analysis_operational'`` over the historical-stitch
    boundary).
    """
    sql = (
        "SELECT time, gauge_id, variable, value, source, basin_version "
        "FROM weather_observations "
        "WHERE gauge_id = $1 AND time >= $2 AND time < $3"
    )
    args: list[object] = [gauge_id, to_db_timestamp(start), to_db_timestamp(end)]
    if variables is not None:
        args.append(list(variables))
        sql += f" AND variable = ANY(${len(args)}::TEXT[])"
    if source is not None:
        args.append(source)
        sql += f" AND source = ${len(args)}"
    sql += " ORDER BY time, variable, source"
    rows = await conn.fetch(sql, *args)
    return rows_to_frame(
        rows,
        columns=_OBS_COLUMNS,
        time_columns=("time",),
        int_columns=("gauge_id",),
        float_columns=("value",),
        nullable_int_columns=("basin_version",),
    )


async def get_weather_forecast_latest_as_of(
    conn: asyncpg.Connection,
    gauge_id: int,
    as_of: pd.Timestamp,
    horizon_hours: int | None = None,
    variables: list[str] | None = None,
    source: str | None = None,
) -> pd.DataFrame:
    """Most recent forecast issued at or before ``as_of``.

    Per source: returns rows from the single ``MAX(issue_time)`` ≤
    ``as_of`` for that source. If ``source`` is ``None``, every
    distinct source contributes its own latest issue_time
    independently — multiple sources with different latest issue
    cycles will appear together in the result, distinguishable by
    ``issue_time`` and ``source``. Within each source, exactly one
    issue_time is returned (the full horizon of that cycle).

    ``horizon_hours`` truncates rows to ``valid_time <= issue_time +
    horizon_hours`` per source so ``horizon_hours=168`` cleanly cuts
    a 7-day window. ``variables`` and ``source`` filter as in
    ``get_weather_observations``.

    Returns rows ordered by ``source``, then ``valid_time``, then
    ``variable``, then ``quantile``.
    """
    sql_parts = [
        "SELECT issue_time, valid_time, gauge_id, variable, value, "
        "source, quantile, basin_version",
        "FROM weather_forecasts wf",
        "WHERE gauge_id = $1",
        "  AND issue_time = (",
        "      SELECT MAX(issue_time)",
        "        FROM weather_forecasts",
        "       WHERE gauge_id = $1",
        "         AND source = wf.source",
        "         AND issue_time <= $2",
        "  )",
    ]
    args: list[object] = [gauge_id, to_db_timestamp(as_of)]
    if horizon_hours is not None:
        args.append(int(horizon_hours))
        sql_parts.append(
            f"  AND valid_time <= issue_time + (${len(args)}::INTEGER * INTERVAL '1 hour')"
        )
    if variables is not None:
        args.append(list(variables))
        sql_parts.append(f"  AND variable = ANY(${len(args)}::TEXT[])")
    if source is not None:
        args.append(source)
        sql_parts.append(f"  AND source = ${len(args)}")
    sql_parts.append("ORDER BY source, valid_time, variable, quantile NULLS FIRST")
    sql = "\n".join(sql_parts)
    rows = await conn.fetch(sql, *args)
    return rows_to_frame(
        rows,
        columns=_FCST_COLUMNS,
        time_columns=("issue_time", "valid_time"),
        int_columns=("gauge_id",),
        float_columns=("value",),
        nullable_float_columns=("quantile",),
        nullable_int_columns=("basin_version",),
    )


async def get_weather_forecast_at_lead(
    conn: asyncpg.Connection,
    gauge_id: int,
    target_time: pd.Timestamp,
    lead_hours: int,
    tolerance_hours: int = 0,
    variables: list[str] | None = None,
    source: str | None = None,
) -> pd.DataFrame:
    """Forecast that *would have been available* ``lead_hours``
    before ``target_time``.

    Picks ``MAX(issue_time) <= target_time - lead_hours`` per source,
    then returns rows from that issue_time whose ``valid_time`` lies
    in ``[target_time - tolerance_hours, target_time +
    tolerance_hours]``. ``tolerance_hours=0`` requires an exact match
    on ``valid_time``.

    Used for hindcast skill evaluation: "what did we forecast at
    lead L for time T?" → ``lead_hours=L``, ``target_time=T``.

    Returns the same column set as
    ``get_weather_forecast_latest_as_of``.
    """
    if lead_hours < 0:
        raise ValueError("lead_hours must be non-negative")
    if tolerance_hours < 0:
        raise ValueError("tolerance_hours must be non-negative")
    target_db = to_db_timestamp(target_time)
    cutoff = target_time - pd.Timedelta(hours=lead_hours)
    lo = target_time - pd.Timedelta(hours=tolerance_hours)
    hi = target_time + pd.Timedelta(hours=tolerance_hours)
    sql_parts = [
        "SELECT issue_time, valid_time, gauge_id, variable, value, "
        "source, quantile, basin_version",
        "FROM weather_forecasts wf",
        "WHERE gauge_id = $1",
        "  AND issue_time = (",
        "      SELECT MAX(issue_time)",
        "        FROM weather_forecasts",
        "       WHERE gauge_id = $1",
        "         AND source = wf.source",
        "         AND issue_time <= $2",
        "  )",
        "  AND valid_time >= $3",
        "  AND valid_time <= $4",
    ]
    args: list[object] = [
        gauge_id,
        to_db_timestamp(cutoff),
        to_db_timestamp(lo),
        to_db_timestamp(hi),
    ]
    if variables is not None:
        args.append(list(variables))
        sql_parts.append(f"  AND variable = ANY(${len(args)}::TEXT[])")
    if source is not None:
        args.append(source)
        sql_parts.append(f"  AND source = ${len(args)}")
    sql_parts.append("ORDER BY source, valid_time, variable, quantile NULLS FIRST")
    sql = "\n".join(sql_parts)
    # ``target_db`` participates in pd.Timedelta arithmetic above; no
    # need to bind it directly.
    del target_db
    rows = await conn.fetch(sql, *args)
    return rows_to_frame(
        rows,
        columns=_FCST_COLUMNS,
        time_columns=("issue_time", "valid_time"),
        int_columns=("gauge_id",),
        float_columns=("value",),
        nullable_float_columns=("quantile",),
        nullable_int_columns=("basin_version",),
    )
