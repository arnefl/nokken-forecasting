"""Unit tests for the persistence baseline.

Pure-function module — no DB, no fixtures. Each test builds a
DataFrame matching the ``get_observations`` shape and asserts on the
emitted ``ForecastRow`` list.
"""

from __future__ import annotations

import pandas as pd
import pytest

from nokken_forecasting.baselines.persistence import (
    HORIZON_HOURS,
    MODEL_VERSION,
    ForecastRow,
    persistence_forecast,
)


def _obs_frame(rows: list[tuple[pd.Timestamp, str, float]]) -> pd.DataFrame:
    """Build a DataFrame matching `get_observations`'s output shape."""
    df = pd.DataFrame(rows, columns=["time", "value_type", "value"])
    df["gauge_id"] = 12
    df = df[["time", "gauge_id", "value_type", "value"]]
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df["gauge_id"] = df["gauge_id"].astype("int64")
    df["value"] = df["value"].astype("float64")
    return df


class TestPersistenceForecast:
    def test_happy_path_24h_history(self) -> None:
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        obs = _obs_frame(
            [
                (issue - pd.Timedelta(hours=h), "flow", 50.0 + h)
                for h in range(24)
            ]
        )
        rows = persistence_forecast(obs, gauge_id=12, issue_time=issue)

        assert len(rows) == HORIZON_HOURS == 168
        # Every row carries the seed (the latest = h=0 = 50.0).
        assert all(r.value == 50.0 for r in rows)
        assert all(r.model_version == MODEL_VERSION for r in rows)
        assert all(r.value_type == "flow" for r in rows)
        assert all(r.quantile is None for r in rows)
        assert all(r.gauge_id == 12 for r in rows)
        # First valid_time = issue + 1h, last = issue + 168h.
        assert rows[0].valid_time == issue + pd.Timedelta(hours=1)
        assert rows[-1].valid_time == issue + pd.Timedelta(hours=168)
        # All rows share one issue_time.
        assert all(r.issue_time == issue for r in rows)
        # Returned shape is the public dataclass.
        assert isinstance(rows[0], ForecastRow)

    def test_single_observation_seeds_full_horizon(self) -> None:
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        obs = _obs_frame([(issue - pd.Timedelta(hours=2), "flow", 7.5)])
        rows = persistence_forecast(obs, gauge_id=12, issue_time=issue)

        assert len(rows) == HORIZON_HOURS
        assert all(r.value == 7.5 for r in rows)

    def test_empty_frame_raises(self) -> None:
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        empty = _obs_frame([])
        with pytest.raises(ValueError, match="no observations"):
            persistence_forecast(empty, gauge_id=12, issue_time=issue)

    def test_no_rows_at_or_before_issue_raises(self) -> None:
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        # Sole row is *after* issue_time: should be filtered out and
        # surface as the "no observations at or before" error.
        obs = _obs_frame([(issue + pd.Timedelta(hours=1), "flow", 99.0)])
        with pytest.raises(ValueError, match="at or before"):
            persistence_forecast(obs, gauge_id=12, issue_time=issue)

    def test_value_type_filters_series(self) -> None:
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        obs = _obs_frame(
            [
                (issue - pd.Timedelta(hours=2), "level", 1.5),
                (issue - pd.Timedelta(hours=1), "flow", 42.0),
            ]
        )
        # Asking for level: only the level row qualifies as the seed.
        rows = persistence_forecast(
            obs, gauge_id=12, issue_time=issue, value_type="level"
        )
        assert all(r.value == 1.5 for r in rows)
        assert all(r.value_type == "level" for r in rows)

    def test_naive_issue_time_rejected(self) -> None:
        obs = _obs_frame(
            [
                (
                    pd.Timestamp("2026-04-27T11:00:00", tz="UTC"),
                    "flow",
                    10.0,
                )
            ]
        )
        with pytest.raises(ValueError, match="tz-aware"):
            persistence_forecast(
                obs,
                gauge_id=12,
                issue_time=pd.Timestamp("2026-04-27T12:00:00"),
            )

    def test_non_positive_horizon_rejected(self) -> None:
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        obs = _obs_frame([(issue, "flow", 1.0)])
        with pytest.raises(ValueError, match="horizon_hours must be positive"):
            persistence_forecast(
                obs, gauge_id=12, issue_time=issue, horizon_hours=0
            )

    def test_custom_horizon_hours(self) -> None:
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        obs = _obs_frame([(issue, "flow", 5.0)])
        rows = persistence_forecast(
            obs, gauge_id=12, issue_time=issue, horizon_hours=3
        )
        assert len(rows) == 3
        assert [r.valid_time - issue for r in rows] == [
            pd.Timedelta(hours=1),
            pd.Timedelta(hours=2),
            pd.Timedelta(hours=3),
        ]
