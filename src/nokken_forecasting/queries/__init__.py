"""Read-only query layer over nokken-web's Postgres schema.

Phase 3b deliverable: thin async readers backing the Phase 3
baselines and the Phase 6 forecast job. Wraps the five tables
modelling code consumes — ``gauges``, ``sections``, ``observations``,
``weather_observations``, ``weather_forecasts`` — as connection-
agnostic functions returning ``pandas.DataFrame``.

Two-layer plan
--------------

This module is the **lower layer**: SQL-natural shape, one row per
DB row, column names matching the source table. Time columns are
``pandas.Timestamp`` localized to UTC. ``value`` is ``float64``;
``basin_version`` and other nullable integers are pandas ``Int64``.

The **upper layer** — Shyft-shaped per-variable per-point /
per-basin time series for ``geo_point_source`` — is deliberately
deferred to Phase 4. Building that transform before exercising
Shyft's installation and runtime constraints would risk fixing
subtle CRS / tz / dtype / NaN choices wrong; nothing in the lower
layer here forecloses that work.

Connection handling
-------------------

Readers take an injected ``asyncpg.Connection`` and never construct
their own. The ``connect`` helper provides ``async with connect() as
conn`` over the read-only pool from ``nokken_forecasting.db.postgres``;
tests can inject a writable connection (or a mock) directly. The
read-only role used by ``inspect`` works for every read here.

Time semantics
--------------

The Postgres tables use ``TIMESTAMP WITHOUT TIME ZONE`` — see
``nokken-web``'s migrations 002, 003, and 008. The convention across
all three sibling repos is to interpret stored values as naive UTC.
Readers accept tz-aware ``pandas.Timestamp`` inputs (UTC required)
and emit tz-aware UTC ``Timestamp`` columns. Half-open ``[start, end)``
ranges; the upper bound is exclusive.
"""

from __future__ import annotations

from nokken_forecasting.queries._connection import connect
from nokken_forecasting.queries.gauges import get_gauges
from nokken_forecasting.queries.observations import get_observations
from nokken_forecasting.queries.sections import get_sections
from nokken_forecasting.queries.weather import (
    get_weather_forecast_at_lead,
    get_weather_forecast_latest_as_of,
    get_weather_observations,
)

__all__ = [
    "connect",
    "get_gauges",
    "get_observations",
    "get_sections",
    "get_weather_forecast_at_lead",
    "get_weather_forecast_latest_as_of",
    "get_weather_observations",
]
