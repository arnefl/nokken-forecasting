"""Unit tests for ``get_observations`` against a stub connection."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from nokken_forecasting.queries import get_observations
from tests.unit.queries.conftest import StubConn


def _row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "time": datetime(2025, 4, 1, 12, 0, 0),
        "gauge_id": 12,
        "value_type": "flow",
        "value": 42.5,
    }
    base.update(overrides)
    return base


class TestGetObservations:
    async def test_basic_window(self) -> None:
        conn = StubConn([_row()])
        df = await get_observations(
            conn,
            gauge_id=12,
            start=pd.Timestamp("2025-04-01", tz="UTC"),
            end=pd.Timestamp("2025-04-02", tz="UTC"),
        )
        assert "WHERE gauge_id = $1 AND time >= $2 AND time < $3" in conn.last_sql
        assert "ORDER BY time" in conn.last_sql
        assert conn.last_args[0] == 12
        # Naive UTC datetimes bound to the wire.
        assert conn.last_args[1].tzinfo is None
        assert conn.last_args[2].tzinfo is None
        assert list(df.columns) == ["time", "gauge_id", "value_type", "value"]
        assert df.dtypes["time"].tz is not None
        assert str(df.dtypes["time"].tz) == "UTC"

    async def test_value_type_filter(self) -> None:
        conn = StubConn([_row(value_type="level", value=2.1)])
        await get_observations(
            conn,
            gauge_id=12,
            start=pd.Timestamp("2025-04-01", tz="UTC"),
            end=pd.Timestamp("2025-04-02", tz="UTC"),
            value_type="level",
        )
        assert "value_type = $4" in conn.last_sql
        assert conn.last_args[3] == "level"

    async def test_naive_timestamp_rejected(self) -> None:
        conn = StubConn([])
        with pytest.raises(ValueError, match="tz-aware"):
            await get_observations(
                conn,
                gauge_id=12,
                start=pd.Timestamp("2025-04-01"),
                end=pd.Timestamp("2025-04-02", tz="UTC"),
            )

    async def test_empty_result(self) -> None:
        conn = StubConn([])
        df = await get_observations(
            conn,
            gauge_id=12,
            start=pd.Timestamp("2025-04-01", tz="UTC"),
            end=pd.Timestamp("2025-04-02", tz="UTC"),
        )
        assert df.empty
        assert list(df.columns) == ["time", "gauge_id", "value_type", "value"]

    async def test_non_utc_input_converted(self) -> None:
        conn = StubConn([])
        await get_observations(
            conn,
            gauge_id=12,
            start=pd.Timestamp("2025-04-01 02:00", tz="Europe/Oslo"),
            end=pd.Timestamp("2025-04-01 12:00", tz="UTC"),
        )
        # 02:00 Oslo (CEST in April, +02:00) → 00:00 UTC
        assert conn.last_args[1] == datetime(2025, 4, 1, 0, 0)
