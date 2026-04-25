"""Integration tests for the query-layer readers.

Each test asks the seeded fixture-conn (writable; bypasses the
read-only inspect pool) for a specific slice of the synthetic data
and asserts on shape, dtypes, ordering, and value content. The
fixture seeds known synthetic values so a regression that quietly
returns the wrong row is caught.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import asyncpg
import pandas as pd
import pytest

from nokken_forecasting.queries import (
    get_gauges,
    get_observations,
    get_sections,
    get_weather_forecast_at_lead,
    get_weather_forecast_latest_as_of,
    get_weather_observations,
)
from tests.integration.queries.conftest import (
    FIXTURE_FCST_ISSUE_TIMES,
    FIXTURE_GAUGE_ID,
    FIXTURE_OBS_START,
    FIXTURE_SECTION_IDS,
    LOCATIONFORECAST_VARIABLES,
    WEATHER_VARIABLES,
)


def _utc(dt: datetime) -> pd.Timestamp:
    return pd.Timestamp(dt).tz_localize("UTC")


class TestGetGaugesIntegration:
    async def test_seeded_gauge_round_trip(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        df = await get_gauges(seeded_conn)
        assert len(df) == 1
        row = df.iloc[0]
        assert row["gauge_id"] == FIXTURE_GAUGE_ID
        assert row["gauge_name"] == "Faukstad"
        assert row["sourcing_key"] == "2.595.0"
        assert pd.isna(row["location"])

    async def test_filter_by_unknown_gauge(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        df = await get_gauges(seeded_conn, gauge_ids=[9999])
        assert df.empty
        # Empty frame still carries the documented columns.
        assert "gauge_id" in df.columns


class TestGetSectionsIntegration:
    async def test_returns_both_seeded_sections(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        df = await get_sections(seeded_conn)
        assert sorted(df["section_id"].tolist()) == list(FIXTURE_SECTION_IDS)
        assert (df["gauge_id"] == FIXTURE_GAUGE_ID).all()

    async def test_filter_by_gauge(self, seeded_conn: asyncpg.Connection) -> None:
        df = await get_sections(seeded_conn, gauge_ids=[FIXTURE_GAUGE_ID])
        assert len(df) == len(FIXTURE_SECTION_IDS)
        df_empty = await get_sections(seeded_conn, gauge_ids=[9999])
        assert df_empty.empty


class TestGetObservationsIntegration:
    async def test_full_24h_window(self, seeded_conn: asyncpg.Connection) -> None:
        df = await get_observations(
            seeded_conn,
            gauge_id=FIXTURE_GAUGE_ID,
            start=_utc(FIXTURE_OBS_START),
            end=_utc(FIXTURE_OBS_START + timedelta(hours=24)),
        )
        assert len(df) == 24
        # Half-open window: end is exclusive.
        assert df["time"].min() == _utc(FIXTURE_OBS_START)
        assert df["time"].max() == _utc(FIXTURE_OBS_START + timedelta(hours=23))
        # Ascending order by time.
        assert df["time"].is_monotonic_increasing

    async def test_tz_round_trip_keeps_utc(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        df = await get_observations(
            seeded_conn,
            gauge_id=FIXTURE_GAUGE_ID,
            start=_utc(FIXTURE_OBS_START),
            end=_utc(FIXTURE_OBS_START + timedelta(hours=1)),
        )
        assert str(df.dtypes["time"].tz) == "UTC"
        assert df["time"].iloc[0].tzinfo is not None

    async def test_value_type_filter(self, seeded_conn: asyncpg.Connection) -> None:
        df = await get_observations(
            seeded_conn,
            gauge_id=FIXTURE_GAUGE_ID,
            start=_utc(FIXTURE_OBS_START),
            end=_utc(FIXTURE_OBS_START + timedelta(hours=24)),
            value_type="level",
        )
        # Fixture only seeded flow rows.
        assert df.empty


class TestGetWeatherObservationsIntegration:
    async def test_all_five_variables_present(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        df = await get_weather_observations(
            seeded_conn,
            gauge_id=FIXTURE_GAUGE_ID,
            start=_utc(FIXTURE_OBS_START),
            end=_utc(FIXTURE_OBS_START + timedelta(hours=24)),
            source="met_nordic_analysis_v4",
        )
        assert set(df["variable"].unique()) == set(WEATHER_VARIABLES)
        # 24 hours × 5 vars = 120 rows when restricted to v4 source.
        assert len(df) == 120

    async def test_source_filter_isolates_stitch_boundary(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        df = await get_weather_observations(
            seeded_conn,
            gauge_id=FIXTURE_GAUGE_ID,
            start=_utc(FIXTURE_OBS_START),
            end=_utc(FIXTURE_OBS_START + timedelta(hours=24)),
            source="met_nordic_analysis_operational",
        )
        # Fixture only seeds one operational-source row (the NULL
        # basin_version sentinel).
        assert len(df) == 1
        assert pd.isna(df["basin_version"].iloc[0])

    async def test_variables_subset_filter(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        df = await get_weather_observations(
            seeded_conn,
            gauge_id=FIXTURE_GAUGE_ID,
            start=_utc(FIXTURE_OBS_START),
            end=_utc(FIXTURE_OBS_START + timedelta(hours=24)),
            variables=["temperature", "shortwave"],
        )
        assert set(df["variable"].unique()) == {"temperature", "shortwave"}


class TestGetWeatherForecastLatestAsOfIntegration:
    async def test_picks_latest_issue_per_source(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        latest_issue = FIXTURE_FCST_ISSUE_TIMES[-1]
        df = await get_weather_forecast_latest_as_of(
            seeded_conn,
            gauge_id=FIXTURE_GAUGE_ID,
            as_of=_utc(latest_issue + timedelta(hours=1)),
        )
        # Only the latest cycle returns rows.
        assert (df["issue_time"] == _utc(latest_issue)).all()
        # Both sources represented.
        assert set(df["source"].unique()) == {
            "met_locationforecast_2_complete",
            "met_nordic_forecast_1km",
        }

    async def test_horizon_truncation(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        latest_issue = FIXTURE_FCST_ISSUE_TIMES[-1]
        df = await get_weather_forecast_latest_as_of(
            seeded_conn,
            gauge_id=FIXTURE_GAUGE_ID,
            as_of=_utc(latest_issue + timedelta(hours=1)),
            horizon_hours=2,
        )
        # Horizon=2 keeps valid_time == issue+1 and issue+2 only.
        max_valid = df["valid_time"].max()
        assert max_valid == _utc(latest_issue + timedelta(hours=2))

    async def test_source_filter_isolates_one_source(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        latest_issue = FIXTURE_FCST_ISSUE_TIMES[-1]
        df = await get_weather_forecast_latest_as_of(
            seeded_conn,
            gauge_id=FIXTURE_GAUGE_ID,
            as_of=_utc(latest_issue + timedelta(hours=1)),
            source="met_nordic_forecast_1km",
        )
        assert (df["source"] == "met_nordic_forecast_1km").all()
        assert (df["variable"] == "shortwave").all()

    async def test_quantile_null_and_non_null_coexist(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        latest_issue = FIXTURE_FCST_ISSUE_TIMES[-1]
        df = await get_weather_forecast_latest_as_of(
            seeded_conn,
            gauge_id=FIXTURE_GAUGE_ID,
            as_of=_utc(latest_issue + timedelta(hours=1)),
            variables=["temperature"],
            source="met_locationforecast_2_complete",
        )
        # The deterministic rows + the one probabilistic row. The
        # 0.9 sentinel value is stored in REAL precision so direct
        # equality is unreliable; check with tolerance.
        assert df["quantile"].isna().any()
        non_null = df["quantile"].dropna()
        assert len(non_null) >= 1
        assert pd.Series(non_null).between(0.85, 0.95).all()

    async def test_as_of_before_first_cycle_is_empty(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        df = await get_weather_forecast_latest_as_of(
            seeded_conn,
            gauge_id=FIXTURE_GAUGE_ID,
            as_of=_utc(FIXTURE_FCST_ISSUE_TIMES[0] - timedelta(hours=1)),
        )
        assert df.empty
        # Schema preserved on empty.
        assert "issue_time" in df.columns


class TestGetWeatherForecastAtLeadIntegration:
    async def test_lead_picks_earlier_cycle(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        # target = latest_issue + 2h = 14:00. lead_hours=4 → cutoff =
        # 10:00, which disqualifies the latest cycle (12:00) and
        # leaves only the earlier cycle (00:00). The earlier cycle's
        # 24-hour horizon covers valid_time=14:00.
        latest_issue = FIXTURE_FCST_ISSUE_TIMES[-1]
        target = latest_issue + timedelta(hours=2)
        df = await get_weather_forecast_at_lead(
            seeded_conn,
            gauge_id=FIXTURE_GAUGE_ID,
            target_time=_utc(target),
            lead_hours=4,
            tolerance_hours=0,
            source="met_locationforecast_2_complete",
        )
        earlier_issue = FIXTURE_FCST_ISSUE_TIMES[0]
        assert (df["issue_time"] == _utc(earlier_issue)).all()
        assert (df["valid_time"] == _utc(target)).all()
        # Four locationforecast variables (one row each, deterministic).
        assert len(df) == len(LOCATIONFORECAST_VARIABLES)

    async def test_tolerance_widens_match(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        latest_issue = FIXTURE_FCST_ISSUE_TIMES[-1]
        target = latest_issue + timedelta(hours=2)
        # lead_hours=0, tolerance=1h: latest cycle is the issue and
        # any valid_time in [target-1, target+1] qualifies → +1, +2, +3.
        df = await get_weather_forecast_at_lead(
            seeded_conn,
            gauge_id=FIXTURE_GAUGE_ID,
            target_time=_utc(target),
            lead_hours=0,
            tolerance_hours=1,
            source="met_locationforecast_2_complete",
            variables=["temperature"],
        )
        valid_hours = sorted(
            (vt - _utc(latest_issue)).total_seconds() / 3600
            for vt in df["valid_time"].unique()
        )
        assert valid_hours == [1, 2, 3]


class TestEmptyResultPreservesSchema:
    async def test_observations_empty_window(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        df = await get_observations(
            seeded_conn,
            gauge_id=FIXTURE_GAUGE_ID,
            start=_utc(datetime(2030, 1, 1)),
            end=_utc(datetime(2030, 1, 2)),
        )
        assert df.empty
        assert list(df.columns) == ["time", "gauge_id", "value_type", "value"]

    async def test_weather_observations_empty_window(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        df = await get_weather_observations(
            seeded_conn,
            gauge_id=FIXTURE_GAUGE_ID,
            start=_utc(datetime(2030, 1, 1)),
            end=_utc(datetime(2030, 1, 2)),
        )
        assert df.empty
        assert "basin_version" in df.columns


class TestUnknownGaugeReturnsEmpty:
    async def test_observations_unknown_gauge(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        df = await get_observations(
            seeded_conn,
            gauge_id=9999,
            start=_utc(FIXTURE_OBS_START),
            end=_utc(FIXTURE_OBS_START + timedelta(hours=24)),
        )
        assert df.empty


def _unused() -> None:
    # Placate ruff "unused import" if pytest aliases shift; the
    # ``pytest`` import is for the conftest skip path.
    _ = pytest
