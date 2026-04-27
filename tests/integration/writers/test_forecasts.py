"""Integration tests for the forecasts writer.

Rides the testcontainer Postgres seeded with nokken-web migrations
at the SCHEMA_COMPAT-pinned SHA. Reuses the ``seeded_conn`` fixture
from ``tests/integration/queries/conftest.py`` — a writable
asyncpg connection with the synthetic Faukstad gauge in place but
the ``forecasts`` table empty.

The writer is connection-agnostic, so injecting ``seeded_conn``
directly bypasses the pool layer here. The pool wiring
(``connect_write`` against ``POSTGRES_WRITE_DSN``) is exercised by
the integration runtime when the operator points the env var at
`nessie` post-deploy; the pool itself has no logic worth a
dedicated unit test beyond the fail-fast on a missing DSN.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import asyncpg
import pandas as pd
import pytest

from nokken_forecasting.baselines.persistence import (
    MODEL_VERSION,
    ForecastRow,
)
from nokken_forecasting.writers.forecasts import insert_forecasts
from tests.integration.queries.conftest import FIXTURE_GAUGE_ID


def _row(
    *,
    issue_time: pd.Timestamp,
    valid_time: pd.Timestamp,
    value: float,
    quantile: float | None = None,
) -> ForecastRow:
    return ForecastRow(
        issue_time=issue_time,
        valid_time=valid_time,
        gauge_id=FIXTURE_GAUGE_ID,
        value_type="flow",
        quantile=quantile,
        value=value,
        model_version=MODEL_VERSION,
    )


class TestInsertForecasts:
    async def test_round_trip_single_row(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        valid = issue + pd.Timedelta(hours=1)
        run_at = datetime(2026, 4, 27, 12, 0, 30, tzinfo=UTC)

        inserted = await insert_forecasts(
            seeded_conn,
            [_row(issue_time=issue, valid_time=valid, value=42.5)],
            model_run_at=run_at,
        )
        assert inserted == 1

        rows = await seeded_conn.fetch(
            "SELECT issue_time, valid_time, gauge_id, value_type, quantile, "
            "value, model_version, model_run_at "
            "FROM forecasts WHERE gauge_id = $1",
            FIXTURE_GAUGE_ID,
        )
        assert len(rows) == 1
        row = rows[0]
        # TIMESTAMP WITHOUT TIME ZONE round-trips as naive UTC.
        assert row["issue_time"] == issue.tz_convert("UTC").to_pydatetime().replace(
            tzinfo=None
        )
        assert row["valid_time"] == valid.tz_convert("UTC").to_pydatetime().replace(
            tzinfo=None
        )
        assert row["gauge_id"] == FIXTURE_GAUGE_ID
        assert row["value_type"] == "flow"
        assert row["quantile"] is None
        assert row["value"] == pytest.approx(42.5)
        assert row["model_version"] == MODEL_VERSION
        # TIMESTAMPTZ round-trips tz-aware; asyncpg returns it in UTC.
        assert row["model_run_at"] == run_at

    async def test_round_trip_full_persistence_batch(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        issue = pd.Timestamp("2026-04-27T00:00:00", tz="UTC")
        run_at = datetime(2026, 4, 27, 0, 1, 0, tzinfo=UTC)
        rows = [
            _row(
                issue_time=issue,
                valid_time=issue + pd.Timedelta(hours=h),
                value=10.0,
            )
            for h in range(1, 169)
        ]
        inserted = await insert_forecasts(
            seeded_conn, rows, model_run_at=run_at
        )
        assert inserted == 168

        count = await seeded_conn.fetchval(
            "SELECT COUNT(*) FROM forecasts WHERE gauge_id = $1 "
            "AND model_version = $2",
            FIXTURE_GAUGE_ID,
            MODEL_VERSION,
        )
        assert count == 168
        # All rows in the batch carry the same model_run_at.
        distinct_run_at = await seeded_conn.fetch(
            "SELECT DISTINCT model_run_at FROM forecasts "
            "WHERE gauge_id = $1 AND model_version = $2",
            FIXTURE_GAUGE_ID,
            MODEL_VERSION,
        )
        assert len(distinct_run_at) == 1
        assert distinct_run_at[0]["model_run_at"] == run_at

    async def test_idempotent_rerun_skips_conflicts(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        valid = issue + pd.Timedelta(hours=1)
        run_at = datetime(2026, 4, 27, 12, 0, 30, tzinfo=UTC)
        batch = [_row(issue_time=issue, valid_time=valid, value=42.5)]

        first = await insert_forecasts(seeded_conn, batch, model_run_at=run_at)
        # Second invocation — same (issue, valid, gauge, value_type,
        # model_version) tuple — should ON CONFLICT DO NOTHING. We
        # bump model_run_at to confirm the original row is preserved.
        second = await insert_forecasts(
            seeded_conn, batch, model_run_at=run_at + timedelta(hours=1)
        )
        assert first == 1
        assert second == 0

        # Original row still carries the original model_run_at.
        stored_run_at = await seeded_conn.fetchval(
            "SELECT model_run_at FROM forecasts WHERE gauge_id = $1",
            FIXTURE_GAUGE_ID,
        )
        assert stored_run_at == run_at

    async def test_empty_batch_returns_zero(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        inserted = await insert_forecasts(
            seeded_conn, [], model_run_at=datetime(2026, 4, 27, tzinfo=UTC)
        )
        assert inserted == 0
        count = await seeded_conn.fetchval("SELECT COUNT(*) FROM forecasts")
        assert count == 0

    async def test_naive_model_run_at_rejected(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        rows = [_row(issue_time=issue, valid_time=issue + pd.Timedelta(hours=1), value=1.0)]
        with pytest.raises(ValueError, match="tz-aware"):
            await insert_forecasts(
                seeded_conn, rows, model_run_at=datetime(2026, 4, 27, 12)
            )

    async def test_quantile_row_rejected(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        rows = [
            _row(
                issue_time=issue,
                valid_time=issue + pd.Timedelta(hours=1),
                value=1.0,
                quantile=0.9,
            )
        ]
        with pytest.raises(ValueError, match="deterministic"):
            await insert_forecasts(
                seeded_conn, rows, model_run_at=datetime(2026, 4, 27, 12, tzinfo=UTC)
            )

    async def test_non_utc_timezone_normalised(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        # model_run_at can come in tz-aware non-UTC; it should be
        # converted to UTC before binding so the stored value matches.
        plus_two = timezone(timedelta(hours=2))
        run_at_local = datetime(2026, 4, 27, 14, 0, 30, tzinfo=plus_two)
        run_at_utc = run_at_local.astimezone(UTC)
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        rows = [_row(issue_time=issue, valid_time=issue + pd.Timedelta(hours=1), value=1.0)]
        await insert_forecasts(seeded_conn, rows, model_run_at=run_at_local)
        stored = await seeded_conn.fetchval(
            "SELECT model_run_at FROM forecasts WHERE gauge_id = $1",
            FIXTURE_GAUGE_ID,
        )
        assert stored == run_at_utc
