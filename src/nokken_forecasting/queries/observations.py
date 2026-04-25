"""Reader for the ``observations`` time-series table.

The Postgres table (nokken-web migration 002) carries gauge readings
keyed on ``(time, gauge_id, value_type)`` with ``value_type`` in
``{'flow', 'level'}``. The table has **no** ``source`` column and uses
``value_type`` rather than ``variable`` — the broader weather-table
vocabulary does not apply here. Schema:

================  =========================  ========
Column            DataFrame dtype            NULL?
================  =========================  ========
``time``          ``datetime64[ns, UTC]``    no
``gauge_id``      ``int64``                  no
``value_type``    ``object``                 no  (``'flow'`` / ``'level'``)
``value``         ``float64``                no
================  =========================  ========

Time-window semantics: half-open ``[start, end)`` (``time >= start
AND time < end``). Both bounds must be tz-aware UTC ``Timestamp``
values.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

from nokken_forecasting.queries._frame import rows_to_frame, to_db_timestamp

if TYPE_CHECKING:
    import asyncpg


_COLUMNS: list[str] = ["time", "gauge_id", "value_type", "value"]


async def get_observations(
    conn: asyncpg.Connection,
    gauge_id: int,
    start: pd.Timestamp,
    end: pd.Timestamp,
    value_type: str | None = None,
) -> pd.DataFrame:
    """Historical gauge readings for one gauge over ``[start, end)``.

    Returns rows in chronological order (``time`` ascending). No
    resampling, no gap-filling, no NaN handling — rows surface as
    they sit in Postgres.

    ``value_type`` is the column name in the underlying table; the
    broader weather-table vocabulary uses ``variable`` and is
    therefore inapplicable here. Pass ``'flow'`` or ``'level'`` to
    filter; ``None`` returns both.
    """
    sql = (
        "SELECT time, gauge_id, value_type, value "
        "FROM observations "
        "WHERE gauge_id = $1 AND time >= $2 AND time < $3"
    )
    args: list[object] = [gauge_id, to_db_timestamp(start), to_db_timestamp(end)]
    if value_type is not None:
        sql += " AND value_type = $4"
        args.append(value_type)
    sql += " ORDER BY time"
    rows = await conn.fetch(sql, *args)
    return rows_to_frame(
        rows,
        columns=_COLUMNS,
        time_columns=("time",),
        int_columns=("gauge_id",),
        float_columns=("value",),
    )
