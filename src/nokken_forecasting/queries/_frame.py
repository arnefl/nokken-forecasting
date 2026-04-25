"""Shared dtype / tz handling for the query-layer readers.

The Postgres tables use ``TIMESTAMP WITHOUT TIME ZONE`` for every
time column, with the convention that values are naive UTC. asyncpg
returns those columns as ``datetime`` with ``tzinfo=None``; the
helpers below normalise them to tz-aware UTC ``pandas.Timestamp``
values inside DataFrames, and prepare tz-aware inputs for binding
back to the DB.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

import pandas as pd


def to_db_timestamp(ts: pd.Timestamp | datetime) -> datetime:
    """Convert an inbound timestamp to a naive-UTC ``datetime``.

    Postgres ``TIMESTAMP WITHOUT TIME ZONE`` rejects tz-aware values
    over the asyncpg wire and there is no useful default conversion;
    callers must therefore pass tz-aware UTC. Naive inputs are
    rejected explicitly so a silent off-by-one tz mismatch can't
    creep in.
    """
    if isinstance(ts, pd.Timestamp):
        if ts.tzinfo is None:
            raise ValueError(
                "timestamp must be tz-aware (UTC); got naive Timestamp"
            )
        return ts.tz_convert("UTC").to_pydatetime().replace(tzinfo=None)
    if ts.tzinfo is None:
        raise ValueError("timestamp must be tz-aware (UTC); got naive datetime")
    return pd.Timestamp(ts).tz_convert("UTC").to_pydatetime().replace(tzinfo=None)


def rows_to_frame(
    rows: Iterable[Any],
    *,
    columns: list[str],
    time_columns: tuple[str, ...] = (),
    int_columns: tuple[str, ...] = (),
    nullable_int_columns: tuple[str, ...] = (),
    float_columns: tuple[str, ...] = (),
    nullable_float_columns: tuple[str, ...] = (),
) -> pd.DataFrame:
    """Build a typed DataFrame from asyncpg rows.

    ``rows`` is the iterable of ``asyncpg.Record`` objects (or
    anything dict-like whose keys are the column names). The
    DataFrame is constructed with exactly ``columns`` in that order,
    so an empty result still carries the documented schema.

    ``time_columns`` are localized to UTC (datetime64[ns, UTC]).
    ``int_columns`` are coerced to ``int64`` (must be non-null).
    ``nullable_int_columns`` are coerced to pandas ``Int64`` so a
    DB NULL surfaces as ``pd.NA``.
    ``float_columns`` are ``float64``; nullable variants stay
    ``float64`` with ``NaN`` filling NULLs.
    """
    materialised = [dict(r) for r in rows]
    df = pd.DataFrame(materialised, columns=columns)
    for col in time_columns:
        df[col] = pd.to_datetime(df[col], utc=True)
    for col in int_columns:
        df[col] = df[col].astype("int64")
    for col in nullable_int_columns:
        df[col] = df[col].astype("Int64")
    for col in float_columns:
        df[col] = df[col].astype("float64")
    for col in nullable_float_columns:
        df[col] = df[col].astype("float64")
    return df
