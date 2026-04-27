"""Unit tests for the scheduled forecast job's gauge-iteration logic.

The DB-touching pieces (``get_observations``, ``insert_forecasts``)
are monkeypatched out — this module tests the wiring around them: how
errors per-gauge propagate (or don't), how the summary aggregates,
how the exit code is derived. The real read → forecast → write path
is exercised by ``tests/integration/jobs/test_forecast_job.py`` and
the writer's own integration suite.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from nokken_forecasting.baselines.persistence import (
    HORIZON_HOURS,
    ForecastRow,
)
from nokken_forecasting.jobs import forecast_job

_SENTINEL_CONN = object()


def _stub_observations() -> pd.DataFrame:
    # Returned by the patched get_observations; the patched
    # persistence_forecast doesn't read it but the job passes it
    # through, so a non-empty DataFrame avoids any accidental empty-
    # series guard in the real persistence baseline if someone wires
    # things wrong.
    return pd.DataFrame(
        {
            "time": [pd.Timestamp("2026-04-27T00:00:00", tz="UTC")],
            "gauge_id": [12],
            "value_type": ["flow"],
            "value": [42.0],
        }
    )


def _stub_rows(gauge_id: int, issue_time: pd.Timestamp) -> list[ForecastRow]:
    return [
        ForecastRow(
            issue_time=issue_time,
            valid_time=issue_time + pd.Timedelta(hours=1),
            gauge_id=gauge_id,
            value_type="flow",
            quantile=None,
            value=42.0,
            model_version="persistence_v1",
        )
    ]


@pytest.fixture
def patch_pipeline(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch the three job collaborators with controllable doubles.

    The returned dict is mutated by individual tests to flip a gauge's
    behaviour to "error" or to assert the call counts.
    """
    state: dict[str, Any] = {
        "errors": {},  # gauge_id → exception to raise
        "obs_calls": [],
        "forecast_calls": [],
        "insert_calls": [],
        "insert_returns": {},  # gauge_id → int rows inserted
    }

    async def fake_get_observations(
        conn: Any,
        *,
        gauge_id: int,
        start: pd.Timestamp,
        end: pd.Timestamp,
        value_type: str,
    ) -> pd.DataFrame:
        state["obs_calls"].append((gauge_id, start, end, value_type))
        if gauge_id in state["errors"]:
            raise state["errors"][gauge_id]
        return _stub_observations()

    def fake_persistence_forecast(
        observations: pd.DataFrame,
        *,
        gauge_id: int,
        issue_time: pd.Timestamp,
        value_type: str = "flow",
        horizon_hours: int = HORIZON_HOURS,
    ) -> list[ForecastRow]:
        state["forecast_calls"].append((gauge_id, issue_time, horizon_hours))
        return _stub_rows(gauge_id, issue_time)

    async def fake_insert_forecasts(
        conn: Any,
        rows: list[ForecastRow],
        *,
        model_run_at: Any,
    ) -> int:
        gauge_id = rows[0].gauge_id if rows else None
        state["insert_calls"].append((gauge_id, len(rows), model_run_at))
        return state["insert_returns"].get(gauge_id, len(rows))

    monkeypatch.setattr(
        forecast_job, "get_observations", fake_get_observations
    )
    monkeypatch.setattr(
        forecast_job, "persistence_forecast", fake_persistence_forecast
    )
    monkeypatch.setattr(
        forecast_job, "insert_forecasts", fake_insert_forecasts
    )
    return state


class TestRunForecastJob:
    async def test_happy_path_all_gauges_succeed(
        self, patch_pipeline: dict[str, Any]
    ) -> None:
        issue = pd.Timestamp("2026-04-27T00:00:00", tz="UTC")
        summary = await forecast_job.run_forecast_job(
            issue_time=issue,
            gauges=(12, 13),
            conn=_SENTINEL_CONN,
        )

        assert summary.exit_code == 0
        assert summary.succeeded == 2
        assert summary.failed == 0
        assert summary.rows_written == 2  # one row stub × two gauges
        assert [o.gauge_id for o in summary.outcomes] == [12, 13]
        assert all(o.status == "success" for o in summary.outcomes)
        assert [c[0] for c in patch_pipeline["obs_calls"]] == [12, 13]
        assert [c[0] for c in patch_pipeline["insert_calls"]] == [12, 13]
        # All gauges in one tick share one model_run_at: the writer
        # stamps each batch from a single argument and the job builds
        # that argument once, before the gauge loop.
        run_ats = {c[2] for c in patch_pipeline["insert_calls"]}
        assert len(run_ats) == 1

    async def test_one_gauge_fails_others_succeed_exit_zero(
        self, patch_pipeline: dict[str, Any]
    ) -> None:
        # Exit non-zero only when *every* gauge errors. A single-gauge
        # outage in a multi-gauge tick is logged-and-continued: the
        # systemd unit shouldn't escalate to "this whole tick failed"
        # in that case.
        patch_pipeline["errors"][13] = RuntimeError("upstream feed down")
        issue = pd.Timestamp("2026-04-27T00:00:00", tz="UTC")
        summary = await forecast_job.run_forecast_job(
            issue_time=issue,
            gauges=(12, 13, 14),
            conn=_SENTINEL_CONN,
        )

        assert summary.exit_code == 0
        assert summary.succeeded == 2
        assert summary.failed == 1
        assert summary.rows_written == 2
        statuses = {o.gauge_id: o.status for o in summary.outcomes}
        assert statuses == {12: "success", 13: "error", 14: "success"}
        failed = next(o for o in summary.outcomes if o.gauge_id == 13)
        assert failed.error is not None
        assert "upstream feed down" in failed.error
        assert failed.rows_written == 0
        # The failed gauge still shows up in obs_calls (the failure
        # happens *inside* get_observations) but never reaches insert.
        assert [c[0] for c in patch_pipeline["obs_calls"]] == [12, 13, 14]
        assert [c[0] for c in patch_pipeline["insert_calls"]] == [12, 14]

    async def test_every_gauge_fails_exit_nonzero(
        self, patch_pipeline: dict[str, Any]
    ) -> None:
        patch_pipeline["errors"][12] = RuntimeError("boom 12")
        patch_pipeline["errors"][13] = RuntimeError("boom 13")
        issue = pd.Timestamp("2026-04-27T00:00:00", tz="UTC")
        summary = await forecast_job.run_forecast_job(
            issue_time=issue,
            gauges=(12, 13),
            conn=_SENTINEL_CONN,
        )

        assert summary.exit_code == 1
        assert summary.succeeded == 0
        assert summary.failed == 2
        assert summary.rows_written == 0

    async def test_single_gauge_failure_exits_nonzero(
        self, patch_pipeline: dict[str, Any]
    ) -> None:
        # Today's production reality: FORECAST_GAUGES = (12,) — one
        # gauge. "all-gauges-failed" collapses to "the only gauge
        # failed", which is the right escalation: the operator wants
        # journald to flag a single-gauge outage as the whole tick
        # failing.
        patch_pipeline["errors"][12] = RuntimeError("boom")
        summary = await forecast_job.run_forecast_job(
            issue_time=pd.Timestamp("2026-04-27T00:00:00", tz="UTC"),
            gauges=(12,),
            conn=_SENTINEL_CONN,
        )
        assert summary.exit_code == 1
        assert summary.failed == 1

    async def test_empty_gauge_list_is_noop_exits_zero(
        self, patch_pipeline: dict[str, Any]
    ) -> None:
        # An empty FORECAST_GAUGES is degenerate but legal — exits zero
        # so a temporary "pause everything" via gauge-list redaction
        # doesn't poison the unit's success state.
        summary = await forecast_job.run_forecast_job(
            issue_time=pd.Timestamp("2026-04-27T00:00:00", tz="UTC"),
            gauges=(),
            conn=_SENTINEL_CONN,
        )
        assert summary.exit_code == 0
        assert summary.outcomes == ()
        assert summary.rows_written == 0
        assert patch_pipeline["obs_calls"] == []
        assert patch_pipeline["insert_calls"] == []

    async def test_naive_issue_time_rejected(
        self, patch_pipeline: dict[str, Any]
    ) -> None:
        with pytest.raises(ValueError, match="tz-aware"):
            await forecast_job.run_forecast_job(
                issue_time=pd.Timestamp("2026-04-27T00:00:00"),
                gauges=(12,),
                conn=_SENTINEL_CONN,
            )

    async def test_default_issue_time_floors_to_top_of_hour(
        self,
        patch_pipeline: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The default issue_time is `now()` floored to top-of-hour so
        # two ticks fired inside the same hour (e.g. a Persistent=true
        # catch-up landing right after a manual run) collide on the
        # writer's deterministic uniqueness key. Pinning `now()` makes
        # the floor observable.
        fake_now = pd.Timestamp("2026-04-27T03:47:13.500", tz="UTC")

        class _FrozenTimestamp(pd.Timestamp):
            pass

        def fake_pd_now(*, tz: str) -> pd.Timestamp:
            assert tz == "UTC"
            return fake_now

        monkeypatch.setattr(
            forecast_job.pd.Timestamp, "now", staticmethod(fake_pd_now)
        )
        summary = await forecast_job.run_forecast_job(
            gauges=(12,), conn=_SENTINEL_CONN
        )
        assert summary.issue_time == pd.Timestamp(
            "2026-04-27T03:00:00", tz="UTC"
        )
