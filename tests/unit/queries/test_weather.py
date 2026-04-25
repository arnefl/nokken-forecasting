"""Unit tests for the weather readers against a stub connection."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from nokken_forecasting.queries import (
    get_weather_forecast_at_lead,
    get_weather_forecast_latest_as_of,
    get_weather_observations,
)
from tests.unit.queries.conftest import StubConn


def _wobs_row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "time": datetime(2025, 4, 1, 12, 0, 0),
        "gauge_id": 12,
        "variable": "temperature",
        "value": 7.5,
        "source": "met_nordic_analysis_v4",
        "basin_version": 1,
    }
    base.update(overrides)
    return base


def _wfcst_row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "issue_time": datetime(2025, 4, 1, 12, 0, 0),
        "valid_time": datetime(2025, 4, 1, 13, 0, 0),
        "gauge_id": 12,
        "variable": "temperature",
        "value": 7.5,
        "source": "met_locationforecast_2_complete",
        "quantile": None,
        "basin_version": 1,
    }
    base.update(overrides)
    return base


class TestGetWeatherObservations:
    async def test_basic_window(self) -> None:
        conn = StubConn([_wobs_row()])
        df = await get_weather_observations(
            conn,
            gauge_id=12,
            start=pd.Timestamp("2025-04-01", tz="UTC"),
            end=pd.Timestamp("2025-04-02", tz="UTC"),
        )
        assert "FROM weather_observations" in conn.last_sql
        assert list(df.columns) == [
            "time",
            "gauge_id",
            "variable",
            "value",
            "source",
            "basin_version",
        ]
        assert df.dtypes["basin_version"] == "Int64"

    async def test_variables_filter(self) -> None:
        conn = StubConn([_wobs_row()])
        await get_weather_observations(
            conn,
            gauge_id=12,
            start=pd.Timestamp("2025-04-01", tz="UTC"),
            end=pd.Timestamp("2025-04-02", tz="UTC"),
            variables=["temperature", "precipitation"],
        )
        assert "ANY($4::TEXT[])" in conn.last_sql
        assert conn.last_args[3] == ["temperature", "precipitation"]

    async def test_source_filter_combined_with_variables(self) -> None:
        conn = StubConn([_wobs_row()])
        await get_weather_observations(
            conn,
            gauge_id=12,
            start=pd.Timestamp("2025-04-01", tz="UTC"),
            end=pd.Timestamp("2025-04-02", tz="UTC"),
            variables=["shortwave"],
            source="met_nordic_analysis_v4",
        )
        # variables binds at $4, source binds at $5.
        assert "AND source = $5" in conn.last_sql
        assert conn.last_args[3] == ["shortwave"]
        assert conn.last_args[4] == "met_nordic_analysis_v4"

    async def test_null_basin_version_surfaces_as_pd_na(self) -> None:
        conn = StubConn([_wobs_row(basin_version=None)])
        df = await get_weather_observations(
            conn,
            gauge_id=12,
            start=pd.Timestamp("2025-04-01", tz="UTC"),
            end=pd.Timestamp("2025-04-02", tz="UTC"),
        )
        assert pd.isna(df.loc[0, "basin_version"])


class TestGetWeatherForecastLatestAsOf:
    async def test_subquery_picks_max_issue_per_source(self) -> None:
        conn = StubConn([_wfcst_row()])
        await get_weather_forecast_latest_as_of(
            conn,
            gauge_id=12,
            as_of=pd.Timestamp("2025-04-01 18:00", tz="UTC"),
        )
        sql = conn.last_sql
        assert "MAX(issue_time)" in sql
        assert "AND source = wf.source" in sql
        assert "AND issue_time <= $2" in sql

    async def test_horizon_truncates_valid_time(self) -> None:
        conn = StubConn([_wfcst_row()])
        await get_weather_forecast_latest_as_of(
            conn,
            gauge_id=12,
            as_of=pd.Timestamp("2025-04-01 18:00", tz="UTC"),
            horizon_hours=168,
        )
        assert (
            "valid_time <= issue_time + ($3::INTEGER * INTERVAL '1 hour')"
            in conn.last_sql
        )
        assert conn.last_args[2] == 168

    async def test_quantile_null_surfaces_as_nan(self) -> None:
        conn = StubConn([_wfcst_row(quantile=None)])
        df = await get_weather_forecast_latest_as_of(
            conn,
            gauge_id=12,
            as_of=pd.Timestamp("2025-04-01 18:00", tz="UTC"),
        )
        assert pd.isna(df.loc[0, "quantile"])
        assert df.dtypes["quantile"] == "float64"

    async def test_columns_present_in_full_order(self) -> None:
        conn = StubConn([_wfcst_row()])
        df = await get_weather_forecast_latest_as_of(
            conn,
            gauge_id=12,
            as_of=pd.Timestamp("2025-04-01 18:00", tz="UTC"),
        )
        assert list(df.columns) == [
            "issue_time",
            "valid_time",
            "gauge_id",
            "variable",
            "value",
            "source",
            "quantile",
            "basin_version",
        ]


class TestGetWeatherForecastAtLead:
    async def test_cutoff_and_window_args(self) -> None:
        conn = StubConn([_wfcst_row()])
        await get_weather_forecast_at_lead(
            conn,
            gauge_id=12,
            target_time=pd.Timestamp("2025-04-02 12:00", tz="UTC"),
            lead_hours=24,
        )
        # $2 = target - 24h ; $3,$4 = exact target
        assert conn.last_args[1] == datetime(2025, 4, 1, 12, 0, 0)
        assert conn.last_args[2] == datetime(2025, 4, 2, 12, 0, 0)
        assert conn.last_args[3] == datetime(2025, 4, 2, 12, 0, 0)

    async def test_tolerance_widens_valid_time_window(self) -> None:
        conn = StubConn([_wfcst_row()])
        await get_weather_forecast_at_lead(
            conn,
            gauge_id=12,
            target_time=pd.Timestamp("2025-04-02 12:00", tz="UTC"),
            lead_hours=24,
            tolerance_hours=2,
        )
        assert conn.last_args[2] == datetime(2025, 4, 2, 10, 0, 0)
        assert conn.last_args[3] == datetime(2025, 4, 2, 14, 0, 0)

    async def test_negative_lead_rejected(self) -> None:
        conn = StubConn([])
        with pytest.raises(ValueError, match="lead_hours"):
            await get_weather_forecast_at_lead(
                conn,
                gauge_id=12,
                target_time=pd.Timestamp("2025-04-02 12:00", tz="UTC"),
                lead_hours=-1,
            )

    async def test_negative_tolerance_rejected(self) -> None:
        conn = StubConn([])
        with pytest.raises(ValueError, match="tolerance_hours"):
            await get_weather_forecast_at_lead(
                conn,
                gauge_id=12,
                target_time=pd.Timestamp("2025-04-02 12:00", tz="UTC"),
                lead_hours=24,
                tolerance_hours=-1,
            )
