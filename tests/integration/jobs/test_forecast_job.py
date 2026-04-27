"""End-to-end test for the scheduled-job tick.

Calls ``run_forecast_job`` against the testcontainer Postgres seeded
with the synthetic Faukstad fixture. The injected connection carries
``default_transaction_read_only = on`` (via
``seeded_conn_readonly_default``) so the writer's
``SET TRANSACTION READ WRITE`` opt-out is exercised the way it is in
production. Equivalent end-to-end test for ``run_forecast_job`` —
covers the job-module wiring rather than the writer alone.
"""

from __future__ import annotations

import asyncpg
import pandas as pd
import pytest

from nokken_forecasting.baselines.persistence import HORIZON_HOURS, MODEL_VERSION
from nokken_forecasting.jobs import run_forecast_job
from tests.integration.queries.conftest import (
    FIXTURE_GAUGE_ID,
    FIXTURE_OBS_START,
)

# The synthetic fixture seeds 24 hourly observations starting at
# FIXTURE_OBS_START. The persistence baseline reads the most recent
# observation at or before issue_time, so any issue_time inside (or
# just after) that window yields a deterministic forecast.
_ISSUE_TIME = pd.Timestamp(
    FIXTURE_OBS_START + pd.Timedelta(hours=23), tz="UTC"
)


class TestRunForecastJob:
    async def test_writes_full_persistence_batch_and_exits_zero(
        self, seeded_conn_readonly_default: asyncpg.Connection
    ) -> None:
        summary = await run_forecast_job(
            issue_time=_ISSUE_TIME,
            gauges=(FIXTURE_GAUGE_ID,),
            conn=seeded_conn_readonly_default,
        )

        assert summary.exit_code == 0
        assert summary.succeeded == 1
        assert summary.failed == 0
        assert summary.rows_written == HORIZON_HOURS

        rows = await seeded_conn_readonly_default.fetch(
            "SELECT issue_time, valid_time, gauge_id, value_type, "
            "model_version, value, model_run_at "
            "FROM forecasts WHERE gauge_id = $1 ORDER BY valid_time",
            FIXTURE_GAUGE_ID,
        )
        assert len(rows) == HORIZON_HOURS
        assert all(r["model_version"] == MODEL_VERSION for r in rows)
        assert all(r["value_type"] == "flow" for r in rows)
        # `model_run_at` is stamped once at job start; every row in
        # this tick shares it.
        distinct_run_at = {r["model_run_at"] for r in rows}
        assert len(distinct_run_at) == 1
        # Persistence holds the *last* observation flat. The fixture
        # seeds value = 10.0 + h for h ∈ [0, 23], so the last value at
        # issue_time = FIXTURE_OBS_START + 23h is 33.0.
        assert all(r["value"] == pytest.approx(33.0) for r in rows)
        # Lead times: H+1 .. H+168.
        leads = [
            (r["valid_time"] - r["issue_time"]).total_seconds() / 3600
            for r in rows
        ]
        assert leads == [float(h) for h in range(1, HORIZON_HOURS + 1)]

    async def test_rerun_inside_same_hour_is_no_op(
        self, seeded_conn_readonly_default: asyncpg.Connection
    ) -> None:
        # Idempotency: a second tick at the same `issue_time` collides
        # with the writer's deterministic uniqueness key. PR 1's
        # writer test already covers the contract at the writer
        # boundary; this asserts the job preserves it.
        first = await run_forecast_job(
            issue_time=_ISSUE_TIME,
            gauges=(FIXTURE_GAUGE_ID,),
            conn=seeded_conn_readonly_default,
        )
        second = await run_forecast_job(
            issue_time=_ISSUE_TIME,
            gauges=(FIXTURE_GAUGE_ID,),
            conn=seeded_conn_readonly_default,
        )
        assert first.exit_code == 0
        assert second.exit_code == 0
        assert first.rows_written == HORIZON_HOURS
        assert second.rows_written == 0

        count = await seeded_conn_readonly_default.fetchval(
            "SELECT COUNT(*) FROM forecasts WHERE gauge_id = $1",
            FIXTURE_GAUGE_ID,
        )
        assert count == HORIZON_HOURS

    async def test_unknown_gauge_logged_as_error_run_exits_nonzero(
        self, seeded_conn_readonly_default: asyncpg.Connection
    ) -> None:
        # Single-gauge tick where the gauge has no observations: the
        # persistence baseline raises ValueError, which the job
        # converts to a per-gauge `error` outcome. With one gauge that
        # collapses to "every gauge failed" → exit_code = 1.
        summary = await run_forecast_job(
            issue_time=_ISSUE_TIME,
            gauges=(99999,),  # not in `gauges`; observations empty
            conn=seeded_conn_readonly_default,
        )
        assert summary.exit_code == 1
        assert summary.succeeded == 0
        assert summary.failed == 1
        assert summary.outcomes[0].error is not None

        count = await seeded_conn_readonly_default.fetchval(
            "SELECT COUNT(*) FROM forecasts"
        )
        assert count == 0
