"""Integration tests for the forecasts writer.

Rides the testcontainer Postgres seeded with nokken-web migrations
at the SCHEMA_COMPAT-pinned SHA. Reuses the ``seeded_conn`` fixture
from ``tests/integration/queries/conftest.py`` — a writable
asyncpg connection with the synthetic Faukstad gauge in place but
the ``forecasts`` table empty.

The writer is connection-agnostic, so injecting ``seeded_conn``
directly is enough for round-trip coverage. The single pool from
``db.postgres.get_pool`` is exercised at runtime by the ``forecast``
CLI subcommand and (PR 2) the scheduled job; its
``default_transaction_read_only`` invariant is already covered by
``tests/integration/test_inspect.py``.
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

    async def test_rerun_is_no_op_and_preserves_original_model_run_at(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        # Contract guard: ON CONFLICT DO NOTHING means a rerun of the
        # same (issue, valid, gauge, value_type, model_version) tuple
        # is a no-op AND the original `model_run_at` survives. Any
        # future change to DO UPDATE SET model_run_at = NOW() will
        # break this test deliberately, forcing a contract review.
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        valid = issue + pd.Timedelta(hours=1)
        run_at = datetime(2026, 4, 27, 12, 0, 30, tzinfo=UTC)
        batch = [_row(issue_time=issue, valid_time=valid, value=42.5)]

        first = await insert_forecasts(seeded_conn, batch, model_run_at=run_at)
        # Bump model_run_at on the rerun: if DO NOTHING ever flipped to
        # DO UPDATE, the stored value would shift to run_at + 1h.
        second = await insert_forecasts(
            seeded_conn, batch, model_run_at=run_at + timedelta(hours=1)
        )
        assert first == 1
        assert second == 0

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
        # Tz contract (see writer module docstring): naive datetime
        # inputs raise so a silent off-by-one tz mismatch can't creep
        # in. Symmetric with `to_db_timestamp`'s naive-rejection on
        # the TIMESTAMP-WITHOUT-TZ columns.
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

    async def test_non_utc_aware_model_run_at_converted_to_utc(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        # Tz contract (see writer module docstring): tz-aware non-UTC
        # inputs are accepted and converted to UTC at the wire
        # boundary. 14:00:30+02:00 should land as 12:00:30Z.
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

    async def test_writer_overrides_session_read_only_default(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        # Regression: the writer must succeed even when the connection's
        # session has `default_transaction_read_only = on` (which the
        # pool's `_init_readonly` callback applies in production).
        # asyncpg's `Transaction` builder only emits `READ ONLY`, never
        # `READ WRITE` — so a writer that opens
        # `conn.transaction(readonly=False)` and trusts it to issue
        # `BEGIN READ WRITE` actually issues plain `BEGIN`, inherits the
        # session default, and INSERT raises SQLSTATE 25006. The
        # `seeded_conn` fixture is a raw `asyncpg.connect` and does NOT
        # carry that session default, so the other tests in this module
        # cannot catch the regression — this one sets the GUC explicitly
        # to mirror the production pool init.
        await seeded_conn.execute("SET default_transaction_read_only = on")
        assert (
            await seeded_conn.fetchval("SHOW default_transaction_read_only")
            == "on"
        )

        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        valid = issue + pd.Timedelta(hours=1)
        run_at = datetime(2026, 4, 27, 12, 0, 30, tzinfo=UTC)
        inserted = await insert_forecasts(
            seeded_conn,
            [_row(issue_time=issue, valid_time=valid, value=42.5)],
            model_run_at=run_at,
        )
        assert inserted == 1

        # Snap-back: the next BEGIN on the same connection inherits the
        # session-level read-only default again. The writer's
        # `SET TRANSACTION READ WRITE` is scoped to its transaction
        # only, so a follow-up write attempt outside the writer's
        # block must fail with SQLSTATE 25006 — defending adjacent
        # reads on the same connection.
        with pytest.raises(asyncpg.exceptions.ReadOnlySQLTransactionError):
            async with seeded_conn.transaction():
                await seeded_conn.execute(
                    "INSERT INTO forecasts "
                    "(issue_time, valid_time, gauge_id, value_type, "
                    "quantile, value, model_version, model_run_at) "
                    "VALUES ($1, $2, $3, $4, NULL, $5, $6, $7)",
                    valid.tz_convert("UTC").to_pydatetime().replace(tzinfo=None)
                    + timedelta(hours=1),
                    valid.tz_convert("UTC").to_pydatetime().replace(tzinfo=None)
                    + timedelta(hours=2),
                    FIXTURE_GAUGE_ID,
                    "flow",
                    99.0,
                    MODEL_VERSION,
                    run_at,
                )
