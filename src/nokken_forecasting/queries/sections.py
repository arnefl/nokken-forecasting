"""Reader for the ``sections`` reference table.

Returns the table as-is, with the legacy mixed-case columns
preserved verbatim (per nokken-web migration 001 — the columns are
quoted in the source DDL). Schema:

==========================  ================  ========
Column                      DataFrame dtype   NULL?
==========================  ================  ========
``section_id``              ``int64``         no
``section_name``            ``object``        no
``river_id``                ``int64``         no
``local_section_id``        ``int64``         no
``gauge_id``                ``Int64``         yes (sections may exist without a gauge)
``gauge_sub``               ``int64``         no
``gauge_default``           ``object``        no  (``'flow'`` / ``'level'``)
``flowAbsoluteMin``         ``float64``       yes (NaN when NULL)
``flowMin``                 ``float64``       yes
``flowMax``                 ``float64``       yes
``flowAbsoluteMax``         ``float64``       yes
``levelAbsoluteMin``        ``object``        no
``levelMin``                ``object``        no
``levelMax``                ``object``        no
``levelAbsoluteMax``        ``object``        no
==========================  ================  ========

The ``levelXxx`` columns are ``VARCHAR(10)`` in Postgres for legacy
reasons (units encoded as the string itself). Modelling code ignores
them; they are surfaced here for completeness and parity with the
underlying table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

from nokken_forecasting.queries._frame import rows_to_frame

if TYPE_CHECKING:
    import asyncpg


_COLUMNS: list[str] = [
    "section_id",
    "section_name",
    "river_id",
    "local_section_id",
    "gauge_id",
    "gauge_sub",
    "gauge_default",
    "flowAbsoluteMin",
    "flowMin",
    "flowMax",
    "flowAbsoluteMax",
    "levelAbsoluteMin",
    "levelMin",
    "levelMax",
    "levelAbsoluteMax",
]


async def get_sections(
    conn: asyncpg.Connection,
    gauge_ids: list[int] | None = None,
) -> pd.DataFrame:
    """Return all sections, optionally filtered to those whose
    ``gauge_id`` is in ``gauge_ids``.

    Sections without a gauge (``gauge_id IS NULL``) are excluded when
    a filter is supplied.
    """
    select = (
        'SELECT section_id, section_name, river_id, local_section_id, '
        'gauge_id, gauge_sub, gauge_default, '
        '"flowAbsoluteMin", "flowMin", "flowMax", "flowAbsoluteMax", '
        '"levelAbsoluteMin", "levelMin", "levelMax", "levelAbsoluteMax" '
        'FROM sections'
    )
    args: list[object] = []
    if gauge_ids is not None:
        select += " WHERE gauge_id = ANY($1::INTEGER[])"
        args.append(list(gauge_ids))
    select += " ORDER BY section_id"
    rows = await conn.fetch(select, *args)
    return rows_to_frame(
        rows,
        columns=_COLUMNS,
        int_columns=("section_id", "river_id", "local_section_id", "gauge_sub"),
        nullable_int_columns=("gauge_id",),
        nullable_float_columns=(
            "flowAbsoluteMin",
            "flowMin",
            "flowMax",
            "flowAbsoluteMax",
        ),
    )
