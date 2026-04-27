"""End-to-end test for the hindcast harness.

Drives a 3-issue-time persistence hindcast through ``run_hindcast``
against the testcontainer Postgres seeded with the synthetic Faukstad
fixture. The injected connection carries
``default_transaction_read_only = on`` (via
``seeded_conn_readonly_default``) so the writer's
``SET TRANSACTION READ WRITE`` opt-out is exercised the way it is in
production.

Asserts the load-bearing harness contracts:

1. The total row count equals ``n_issue_times × horizon_hours``.
2. **Every** row from one harness invocation shares one
   ``model_run_at`` — the contract PR 6's comparison report depends
   on to filter rows to a hindcast run.
3. ``model_run_at >> issue_time`` for hindcast rows (live forecasts
   have ``model_run_at ≈ issue_time``; this is the distinguishing
   property).
"""

from __future__ import annotations

from datetime import UTC, datetime

import asyncpg
import pandas as pd

from nokken_forecasting.baselines.persistence import (
    HORIZON_HOURS,
    MODEL_VERSION,
    persistence_forecast,
)
from nokken_forecasting.hindcast import run_hindcast
from nokken_forecasting.queries import get_observations
from nokken_forecasting.writers import insert_forecasts
from tests.integration.queries.conftest import (
    FIXTURE_GAUGE_ID,
    FIXTURE_OBS_START,
)

# Issue-times within the synthetic fixture's 24-hour observation
# window — each is at-or-after ≥1 obs, so persistence has a seed.
_ISSUE_TIMES = (
    pd.Timestamp(FIXTURE_OBS_START + pd.Timedelta(hours=8), tz="UTC"),
    pd.Timestamp(FIXTURE_OBS_START + pd.Timedelta(hours=15), tz="UTC"),
    pd.Timestamp(FIXTURE_OBS_START + pd.Timedelta(hours=23), tz="UTC"),
)


class TestRunHindcast:
    async def test_persistence_hindcast_writes_rows_with_shared_run_at(
        self, seeded_conn_readonly_default: asyncpg.Connection
    ) -> None:
        run_at = datetime.now(UTC)

        async def reader(it: pd.Timestamp) -> pd.DataFrame:
            # 24-hour lookback covers the full synthetic-fixture window
            # for any of the three issue-times.
            return await get_observations(
                seeded_conn_readonly_default,
                gauge_id=FIXTURE_GAUGE_ID,
                start=it - pd.Timedelta(hours=24),
                end=it + pd.Timedelta(seconds=1),
                value_type="flow",
            )

        async def writer(rows, model_run_at):  # type: ignore[no-untyped-def]
            return await insert_forecasts(
                seeded_conn_readonly_default,
                rows,
                model_run_at=model_run_at,
            )

        summary = await run_hindcast(
            persistence_forecast,
            gauge_id=FIXTURE_GAUGE_ID,
            issue_times=_ISSUE_TIMES,
            observations_reader=reader,
            writer=writer,
            model_run_at=run_at,
            horizon_hours=HORIZON_HOURS,
            value_type="flow",
        )

        assert summary.succeeded == 3
        assert summary.failed == 0
        assert summary.rows_attempted == 3 * HORIZON_HOURS
        assert summary.rows_inserted == 3 * HORIZON_HOURS
        assert summary.model_run_at == run_at

        # DB-side: total row count and the three load-bearing contracts.
        rows = await seeded_conn_readonly_default.fetch(
            "SELECT issue_time, valid_time, model_version, model_run_at "
            "FROM forecasts WHERE gauge_id = $1 ORDER BY issue_time, valid_time",
            FIXTURE_GAUGE_ID,
        )
        assert len(rows) == 3 * HORIZON_HOURS
        assert all(r["model_version"] == MODEL_VERSION for r in rows)

        # Contract 1: every row shares one model_run_at.
        distinct_run_at = {r["model_run_at"] for r in rows}
        assert distinct_run_at == {run_at}

        # Contract 2: the rows partition into 3 issue-times, each with
        # exactly HORIZON_HOURS valid_times.
        per_issue: dict[object, int] = {}
        for r in rows:
            per_issue[r["issue_time"]] = per_issue.get(r["issue_time"], 0) + 1
        assert len(per_issue) == 3
        assert all(count == HORIZON_HOURS for count in per_issue.values())

        # Contract 3: model_run_at >> issue_time. The fixture issue
        # times are at FIXTURE_OBS_START + 8/15/23 h (April 2025);
        # `run_at` is the wall-clock now (>= 2026-04). Verify the
        # asymmetry the report relies on to tell hindcast apart from
        # live runs.
        for r in rows:
            issue_naive = r["issue_time"]  # TIMESTAMP WITHOUT TZ
            assert (
                run_at.replace(tzinfo=None)
                if issue_naive.tzinfo is None
                else run_at
            ) > issue_naive  # type: ignore[operator]

    async def test_rerun_is_no_op_on_writer_uniqueness_key(
        self, seeded_conn_readonly_default: asyncpg.Connection
    ) -> None:
        # First run lands rows; second run with a *different*
        # model_run_at re-emits the same baseline rows for the same
        # (issue, valid, gauge, value_type, model_version) tuples and
        # gets ON CONFLICT DO NOTHING. The harness's reported
        # `rows_inserted` is 0 on the rerun even though the baseline
        # produced full batches; the original `model_run_at` survives.
        first_run_at = datetime.now(UTC)

        async def reader(it: pd.Timestamp) -> pd.DataFrame:
            return await get_observations(
                seeded_conn_readonly_default,
                gauge_id=FIXTURE_GAUGE_ID,
                start=it - pd.Timedelta(hours=24),
                end=it + pd.Timedelta(seconds=1),
                value_type="flow",
            )

        async def writer(rows, model_run_at):  # type: ignore[no-untyped-def]
            return await insert_forecasts(
                seeded_conn_readonly_default, rows, model_run_at=model_run_at
            )

        first = await run_hindcast(
            persistence_forecast,
            gauge_id=FIXTURE_GAUGE_ID,
            issue_times=_ISSUE_TIMES,
            observations_reader=reader,
            writer=writer,
            model_run_at=first_run_at,
            horizon_hours=HORIZON_HOURS,
            value_type="flow",
        )
        # Rerun with a noticeably-later model_run_at; if DO NOTHING
        # ever flipped to DO UPDATE, the stored value would shift.
        second_run_at = datetime.now(UTC).replace(microsecond=999_999)
        second = await run_hindcast(
            persistence_forecast,
            gauge_id=FIXTURE_GAUGE_ID,
            issue_times=_ISSUE_TIMES,
            observations_reader=reader,
            writer=writer,
            model_run_at=second_run_at,
            horizon_hours=HORIZON_HOURS,
            value_type="flow",
        )
        assert first.rows_inserted == 3 * HORIZON_HOURS
        assert second.rows_inserted == 0  # all conflict-suppressed
        assert second.succeeded == 3  # baseline still ran cleanly

        # Stored rows still carry the *first* run's model_run_at —
        # the audit trail reflects when each row was first written.
        run_at_values = await seeded_conn_readonly_default.fetch(
            "SELECT DISTINCT model_run_at FROM forecasts WHERE gauge_id = $1",
            FIXTURE_GAUGE_ID,
        )
        assert len(run_at_values) == 1
        assert run_at_values[0]["model_run_at"] == first_run_at
