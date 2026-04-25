"""Unit tests for ``get_gauges`` against a stub connection."""

from __future__ import annotations

import pandas as pd

from nokken_forecasting.queries import get_gauges
from tests.unit.queries.conftest import StubConn


def _row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "gauge_id": 12,
        "gauge_name": "Faukstad",
        "has_flow": 1,
        "has_level": 1,
        "source": "nve",
        "sourcing_key": "2.595.0",
        "drainage_basin": 3700.0,
        "location": 4321,
        "gauge_active": 1,
    }
    base.update(overrides)
    return base


class TestGetGauges:
    async def test_no_filter_emits_unfiltered_select(self) -> None:
        conn = StubConn([_row()])
        df = await get_gauges(conn)
        assert "WHERE" not in conn.last_sql
        assert conn.last_args == ()
        assert list(df.columns) == [
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
        assert df.loc[0, "gauge_id"] == 12
        assert df.dtypes["gauge_id"] == "int64"

    async def test_gauge_id_filter_binds_array(self) -> None:
        conn = StubConn([_row()])
        await get_gauges(conn, gauge_ids=[12, 13])
        assert "ANY($1::INTEGER[])" in conn.last_sql
        assert conn.last_args == ([12, 13],)

    async def test_empty_result_keeps_columns(self) -> None:
        conn = StubConn([])
        df = await get_gauges(conn)
        assert df.empty
        assert list(df.columns) == [
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

    async def test_nullable_columns_use_pd_na(self) -> None:
        conn = StubConn([_row(location=None, drainage_basin=None)])
        df = await get_gauges(conn)
        assert pd.isna(df.loc[0, "location"])
        assert df.dtypes["location"] == "Int64"
        assert pd.isna(df.loc[0, "drainage_basin"])
        assert df.dtypes["drainage_basin"] == "float64"
