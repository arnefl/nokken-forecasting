"""Reader for the ``gauges`` reference table.

Returns the table as-is. Schema follows nokken-web migration 001
(see ``db/postgres/migrations/001_reference_tables.sql``):

================  ================  ========
Column            DataFrame dtype   NULL?
================  ================  ========
``gauge_id``      ``int64``         no
``gauge_name``    ``object``        no
``has_flow``      ``int64``         no  (SMALLINT 0/1)
``has_level``     ``int64``         no  (SMALLINT 0/1)
``source``        ``object``        no
``sourcing_key``  ``object``        no
``drainage_basin``  ``float64``     yes (NaN when NULL)
``location``      ``Int64``         yes (geo_spots.spot_id pointer; ``pd.NA`` when NULL)
``gauge_active``  ``int64``         no  (SMALLINT 0/1)
================  ================  ========
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

from nokken_forecasting.queries._frame import rows_to_frame

if TYPE_CHECKING:
    import asyncpg


_COLUMNS: list[str] = [
    "gauge_id",
    "gauge_name",
    "has_flow",
    "has_level",
    "source",
    "sourcing_key",
    "drainage_basin",
    "location",
    "gauge_active",
]


async def get_gauges(
    conn: asyncpg.Connection,
    gauge_ids: list[int] | None = None,
) -> pd.DataFrame:
    """Return all gauges, optionally filtered to ``gauge_ids``.

    See module docstring for the column schema. Empty result still
    carries the documented columns and dtypes.
    """
    sql = (
        "SELECT gauge_id, gauge_name, has_flow, has_level, source, "
        "sourcing_key, drainage_basin, location, gauge_active "
        "FROM gauges"
    )
    args: list[object] = []
    if gauge_ids is not None:
        sql += " WHERE gauge_id = ANY($1::INTEGER[])"
        args.append(list(gauge_ids))
    sql += " ORDER BY gauge_id"
    rows = await conn.fetch(sql, *args)
    return rows_to_frame(
        rows,
        columns=_COLUMNS,
        int_columns=("gauge_id", "has_flow", "has_level", "gauge_active"),
        nullable_int_columns=("location",),
        nullable_float_columns=("drainage_basin",),
    )
