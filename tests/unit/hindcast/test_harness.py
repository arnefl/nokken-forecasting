"""Unit tests for the hindcast harness.

Pure-function tests with mocked observations_reader and writer — no DB.
Covers the per-issue-time loop, baseline-agnostic dispatch, idempotent
rerun (writer's reported zero on second pass), and error isolation.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
import pytest

from nokken_forecasting.baselines.persistence import (
    ForecastRow,
    persistence_forecast,
)
from nokken_forecasting.baselines.recession import recession_forecast
from nokken_forecasting.hindcast import (
    HindcastSummary,
    IssueTimeOutcome,
    run_hindcast,
)


def _obs_frame(rows: list[tuple[pd.Timestamp, str, float]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["time", "value_type", "value"])
    df["gauge_id"] = 12
    df = df[["time", "gauge_id", "value_type", "value"]]
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df["gauge_id"] = df["gauge_id"].astype("int64")
    df["value"] = df["value"].astype("float64")
    return df


def _flat_history(*, issue_time: pd.Timestamp, value: float, n: int = 24) -> pd.DataFrame:
    return _obs_frame(
        [(issue_time - pd.Timedelta(hours=h), "flow", value) for h in range(n)]
    )


class _CapturingWriter:
    """Records each call and returns the configured row count.

    Matches ``WriterFn`` signature: ``(rows, model_run_at) -> int``.
    Defaults to "every row inserted"; tests that need a conflict-
    suppressed rerun set ``returns`` to a fixed integer.
    """

    def __init__(self, returns: int | None = None) -> None:
        self._returns = returns
        self.calls: list[tuple[tuple[ForecastRow, ...], datetime]] = []

    async def __call__(
        self, rows: Sequence[ForecastRow], model_run_at: datetime
    ) -> int:
        self.calls.append((tuple(rows), model_run_at))
        if self._returns is None:
            return len(rows)
        return self._returns


class TestRunHindcast:
    async def test_happy_path_three_issue_times_persistence(self) -> None:
        # Three issue-times, persistence baseline, 24h horizon. The
        # observations_reader returns a flat history per issue-time so
        # persistence emits 24 rows of value=10.0 each.
        issue_times = [
            pd.Timestamp("2024-01-01T00:00:00", tz="UTC"),
            pd.Timestamp("2024-01-08T00:00:00", tz="UTC"),
            pd.Timestamp("2024-01-15T00:00:00", tz="UTC"),
        ]
        reader_calls: list[pd.Timestamp] = []

        async def reader(it: pd.Timestamp) -> pd.DataFrame:
            reader_calls.append(it)
            return _flat_history(issue_time=it, value=10.0)

        writer = _CapturingWriter()
        run_at = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)

        summary = await run_hindcast(
            persistence_forecast,
            gauge_id=12,
            issue_times=issue_times,
            observations_reader=reader,
            writer=writer,
            model_run_at=run_at,
            horizon_hours=24,
        )

        assert isinstance(summary, HindcastSummary)
        assert summary.gauge_id == 12
        assert summary.model_run_at == run_at
        assert summary.succeeded == 3
        assert summary.failed == 0
        assert summary.rows_attempted == 24 * 3
        assert summary.rows_inserted == 24 * 3
        # Reader called once per issue-time, in order.
        assert reader_calls == issue_times
        # Writer called once per issue-time; every call carried the
        # SAME model_run_at — the harness's load-bearing contract.
        assert len(writer.calls) == 3
        assert {call[1] for call in writer.calls} == {run_at}
        # Each batch is a full 24-row persistence horizon.
        for rows, _ in writer.calls:
            assert len(rows) == 24
            assert all(r.model_version == "persistence_v1" for r in rows)

    async def test_dispatches_recession_baseline_unchanged(self) -> None:
        # Same harness, same plumbing, recession baseline instead of
        # persistence. Demonstrates baseline-agnostic dispatch — no
        # type sniffing, no per-baseline branches in the harness.
        issue = pd.Timestamp("2024-06-01T00:00:00", tz="UTC")
        # Synthetic clean recession of 48 h: q0=100, k=0.01.
        import math
        history = _obs_frame(
            [
                (
                    issue - pd.Timedelta(hours=47 - h),
                    "flow",
                    100.0 * math.exp(-0.01 * h),
                )
                for h in range(48)
            ]
        )

        async def reader(it: pd.Timestamp) -> pd.DataFrame:
            return history

        writer = _CapturingWriter()
        run_at = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)

        summary = await run_hindcast(
            recession_forecast,
            gauge_id=12,
            issue_times=[issue],
            observations_reader=reader,
            writer=writer,
            model_run_at=run_at,
            horizon_hours=24,
        )
        assert summary.succeeded == 1
        assert summary.rows_inserted == 24
        rows, _ = writer.calls[0]
        assert all(r.model_version == "recession_v1" for r in rows)
        # Strictly decreasing forecast (the analytic decay curve).
        values = [r.value for r in rows]
        assert all(values[i + 1] < values[i] for i in range(len(values) - 1))

    async def test_idempotent_rerun_writer_reports_zero(self) -> None:
        # The writer's ``ON CONFLICT DO NOTHING`` is what makes a
        # rerun a no-op. The harness reflects that count faithfully.
        issue = pd.Timestamp("2024-01-01T00:00:00", tz="UTC")

        async def reader(it: pd.Timestamp) -> pd.DataFrame:
            return _flat_history(issue_time=it, value=10.0)

        zero_writer = _CapturingWriter(returns=0)  # all rows conflict-suppressed
        run_at = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)

        summary = await run_hindcast(
            persistence_forecast,
            gauge_id=12,
            issue_times=[issue],
            observations_reader=reader,
            writer=zero_writer,
            model_run_at=run_at,
            horizon_hours=24,
        )
        assert summary.succeeded == 1  # baseline ran fine
        assert summary.rows_attempted == 24
        assert summary.rows_inserted == 0  # writer suppressed all

    async def test_per_issue_time_error_isolated_observation_read(self) -> None:
        # First issue-time: reader raises (e.g. transient DB hiccup).
        # Second + third: succeed. The harness logs the failure and
        # continues — final summary has 1 error and 2 successes.
        issue_times = [
            pd.Timestamp("2024-01-01T00:00:00", tz="UTC"),
            pd.Timestamp("2024-01-08T00:00:00", tz="UTC"),
            pd.Timestamp("2024-01-15T00:00:00", tz="UTC"),
        ]
        bad = issue_times[0]

        async def reader(it: pd.Timestamp) -> pd.DataFrame:
            if it == bad:
                raise RuntimeError("transient DB error")
            return _flat_history(issue_time=it, value=10.0)

        writer = _CapturingWriter()
        run_at = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)

        summary = await run_hindcast(
            persistence_forecast,
            gauge_id=12,
            issue_times=issue_times,
            observations_reader=reader,
            writer=writer,
            model_run_at=run_at,
            horizon_hours=24,
        )
        assert summary.succeeded == 2
        assert summary.failed == 1
        bad_outcome = next(o for o in summary.outcomes if o.issue_time == bad)
        assert bad_outcome.status == "error"
        assert bad_outcome.rows_inserted == 0
        assert bad_outcome.error and "RuntimeError" in bad_outcome.error
        # Writer was called for the surviving 2 issue-times, NOT the bad one.
        assert len(writer.calls) == 2

    async def test_per_issue_time_error_isolated_baseline_raise(self) -> None:
        # The recession baseline raises on a too-noisy series. With
        # per-issue-time error isolation that single bad series
        # doesn't abort the overall run.
        issue_times = [
            pd.Timestamp("2024-01-01T00:00:00", tz="UTC"),
            pd.Timestamp("2024-01-08T00:00:00", tz="UTC"),
        ]
        clean = issue_times[1]
        import math

        async def reader(it: pd.Timestamp) -> pd.DataFrame:
            if it == clean:
                # 48h of clean recession.
                return _obs_frame(
                    [
                        (
                            it - pd.Timedelta(hours=47 - h),
                            "flow",
                            100.0 * math.exp(-0.01 * h),
                        )
                        for h in range(48)
                    ]
                )
            # Flat / oscillating series → recession baseline raises.
            return _obs_frame(
                [(it - pd.Timedelta(hours=h), "flow", 10.0) for h in range(48)]
            )

        writer = _CapturingWriter()
        run_at = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
        summary = await run_hindcast(
            recession_forecast,
            gauge_id=12,
            issue_times=issue_times,
            observations_reader=reader,
            writer=writer,
            model_run_at=run_at,
            horizon_hours=24,
        )
        assert summary.succeeded == 1
        assert summary.failed == 1
        clean_outcome = next(o for o in summary.outcomes if o.issue_time == clean)
        assert clean_outcome.status == "success"
        assert clean_outcome.rows_inserted == 24

    async def test_naive_model_run_at_rejected(self) -> None:
        async def reader(it: pd.Timestamp) -> pd.DataFrame:
            return _flat_history(issue_time=it, value=10.0)

        writer = _CapturingWriter()
        with pytest.raises(ValueError, match="tz-aware"):
            await run_hindcast(
                persistence_forecast,
                gauge_id=12,
                issue_times=[pd.Timestamp("2024-01-01", tz="UTC")],
                observations_reader=reader,
                writer=writer,
                model_run_at=datetime(2026, 4, 27, 12, 0, 0),  # naive
            )

    async def test_naive_issue_time_logged_as_error_continues(self) -> None:
        # The harness rejects naive issue_times *per-issue-time* (not
        # by raising) so a single bad entry in a long list doesn't
        # abort the run — same shape as a baseline-side failure.
        good = pd.Timestamp("2024-01-08T00:00:00", tz="UTC")
        naive = pd.Timestamp("2024-01-01T00:00:00")

        async def reader(it: pd.Timestamp) -> pd.DataFrame:
            return _flat_history(issue_time=it, value=10.0)

        writer = _CapturingWriter()
        run_at = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
        summary = await run_hindcast(
            persistence_forecast,
            gauge_id=12,
            issue_times=[naive, good],
            observations_reader=reader,
            writer=writer,
            model_run_at=run_at,
            horizon_hours=24,
        )
        assert summary.succeeded == 1
        assert summary.failed == 1
        assert summary.outcomes[0].status == "error"
        assert summary.outcomes[0].error is not None
        assert "tz-aware" in summary.outcomes[0].error
        assert summary.outcomes[1].status == "success"

    async def test_non_positive_horizon_rejected(self) -> None:
        async def reader(it: pd.Timestamp) -> pd.DataFrame:
            return _flat_history(issue_time=it, value=10.0)

        writer = _CapturingWriter()
        run_at = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
        with pytest.raises(ValueError, match="horizon_hours must be positive"):
            await run_hindcast(
                persistence_forecast,
                gauge_id=12,
                issue_times=[pd.Timestamp("2024-01-01", tz="UTC")],
                observations_reader=reader,
                writer=writer,
                model_run_at=run_at,
                horizon_hours=0,
            )

    async def test_empty_issue_times_returns_empty_summary(self) -> None:
        async def reader(it: pd.Timestamp) -> pd.DataFrame:  # pragma: no cover
            return _flat_history(issue_time=it, value=10.0)

        writer = _CapturingWriter()
        run_at = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
        summary = await run_hindcast(
            persistence_forecast,
            gauge_id=12,
            issue_times=[],
            observations_reader=reader,
            writer=writer,
            model_run_at=run_at,
        )
        assert summary.succeeded == 0
        assert summary.failed == 0
        assert summary.rows_inserted == 0
        assert summary.rows_attempted == 0
        assert writer.calls == []

    async def test_outcome_dataclass_fields_match_summary(self) -> None:
        # IssueTimeOutcome shape pin so a refactor doesn't quietly
        # rename fields PR 6's comparison report wires onto.
        issue = pd.Timestamp("2024-01-01T00:00:00", tz="UTC")

        async def reader(it: pd.Timestamp) -> pd.DataFrame:
            return _flat_history(issue_time=it, value=10.0)

        writer = _CapturingWriter()
        summary = await run_hindcast(
            persistence_forecast,
            gauge_id=12,
            issue_times=[issue],
            observations_reader=reader,
            writer=writer,
            model_run_at=datetime(2026, 4, 27, tzinfo=UTC),
            horizon_hours=24,
        )
        outcome = summary.outcomes[0]
        assert isinstance(outcome, IssueTimeOutcome)
        assert outcome.issue_time == issue
        assert outcome.status == "success"
        assert outcome.rows_attempted == 24
        assert outcome.rows_inserted == 24
        assert outcome.error is None


def test_hindcast_summary_aggregates_outcomes() -> None:
    # Pure dataclass behaviour test — no harness invocation.
    base = pd.Timestamp("2024-01-01T00:00:00", tz="UTC")
    outcomes = (
        IssueTimeOutcome(
            issue_time=base,
            status="success",
            rows_attempted=10,
            rows_inserted=8,
        ),
        IssueTimeOutcome(
            issue_time=base + timedelta(days=7),
            status="error",
            rows_attempted=0,
            rows_inserted=0,
            error="boom",
        ),
        IssueTimeOutcome(
            issue_time=base + timedelta(days=14),
            status="success",
            rows_attempted=10,
            rows_inserted=10,
        ),
    )
    summary = HindcastSummary(
        gauge_id=12,
        model_run_at=datetime(2026, 4, 27, tzinfo=UTC),
        outcomes=outcomes,
    )
    assert summary.succeeded == 2
    assert summary.failed == 1
    assert summary.rows_attempted == 20
    assert summary.rows_inserted == 18


# ``Any`` is imported but unused at module level — referenced via the
# typing alias in fixture signatures only when callers extend.
_ = Any
