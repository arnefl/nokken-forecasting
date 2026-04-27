"""Thin CLI parser / dispatcher tests.

Exercises argument parsing and dispatch routing for the new
``forecast persistence`` subcommand without touching Postgres. The
full read → model → write path is covered by the unit and
integration tests for the components themselves; this layer just
asserts the wiring is intact.
"""

from __future__ import annotations

import argparse

import pandas as pd
import pytest

from nokken_forecasting import cli


class TestParser:
    def test_forecast_persistence_with_explicit_issue_time(self) -> None:
        parser = cli._build_parser()
        args = parser.parse_args(
            [
                "forecast",
                "persistence",
                "--gauge-id",
                "12",
                "--issue-time",
                "2026-04-27T12:00:00Z",
            ]
        )
        assert args.group == "forecast"
        assert args.command == "persistence"
        assert args.gauge_id == 12
        assert args.issue_time == "2026-04-27T12:00:00Z"
        # Defaults match the scoping doc's spec.
        assert args.value_type == "flow"
        assert args.horizon_hours == 168

    def test_forecast_persistence_issue_time_defaults_to_now(self) -> None:
        # `--issue-time` is optional for manual operator runs;
        # `_dispatch_forecast` substitutes `pd.Timestamp.now(tz='UTC')`
        # when the parsed value is None. PR 2's scheduled job will
        # always pass `--issue-time` explicitly.
        parser = cli._build_parser()
        args = parser.parse_args(
            ["forecast", "persistence", "--gauge-id", "12"]
        )
        assert args.gauge_id == 12
        assert args.issue_time is None

    def test_forecast_persistence_overrides(self) -> None:
        parser = cli._build_parser()
        args = parser.parse_args(
            [
                "forecast",
                "persistence",
                "--gauge-id",
                "12",
                "--issue-time",
                "2026-04-27T12:00:00Z",
                "--value-type",
                "level",
                "--horizon-hours",
                "24",
            ]
        )
        assert args.value_type == "level"
        assert args.horizon_hours == 24

    def test_forecast_run_no_args_uses_defaults(self) -> None:
        # `forecast run` is the unattended entry point — no required
        # args. `--issue-time` defaults to None and the job module
        # substitutes wall-clock now floored to top-of-hour.
        parser = cli._build_parser()
        args = parser.parse_args(["forecast", "run"])
        assert args.group == "forecast"
        assert args.command == "run"
        assert args.issue_time is None
        assert args.value_type == "flow"
        assert args.horizon_hours == 168

    def test_forecast_run_with_explicit_issue_time(self) -> None:
        parser = cli._build_parser()
        args = parser.parse_args(
            [
                "forecast",
                "run",
                "--issue-time",
                "2026-04-27T00:00:00Z",
                "--horizon-hours",
                "24",
            ]
        )
        assert args.issue_time == "2026-04-27T00:00:00Z"
        assert args.horizon_hours == 24

    def test_inspect_and_query_groups_still_parse(self) -> None:
        # Existing groups must not regress — the new `forecast`
        # subparser sits alongside, not on top of them.
        parser = cli._build_parser()
        inspect_args = parser.parse_args(["inspect", "tables"])
        assert inspect_args.group == "inspect"
        query_args = parser.parse_args(
            [
                "query",
                "observations",
                "--gauge-id",
                "12",
                "--start",
                "2025-04-01",
                "--end",
                "2025-04-02",
            ]
        )
        assert query_args.group == "query"
        assert query_args.command == "observations"


class TestHindcastParser:
    def test_hindcast_run_persistence_required_args(self) -> None:
        parser = cli._build_parser()
        args = parser.parse_args(
            [
                "hindcast",
                "run",
                "--baseline",
                "persistence",
                "--gauge-id",
                "12",
                "--start",
                "2024-01-01",
                "--end",
                "2024-01-31",
            ]
        )
        assert args.group == "hindcast"
        assert args.command == "run"
        assert args.baseline == "persistence"
        assert args.gauge_id == 12
        assert args.start == "2024-01-01"
        assert args.end == "2024-01-31"
        # Defaults: weekly cadence, flow value-type, 168h horizon.
        assert args.cadence == "weekly"
        assert args.value_type == "flow"
        assert args.horizon_hours == 168

    def test_hindcast_run_recession_with_overrides(self) -> None:
        parser = cli._build_parser()
        args = parser.parse_args(
            [
                "hindcast",
                "run",
                "--baseline",
                "recession",
                "--gauge-id",
                "12",
                "--start",
                "2020-01-01T00:00:00Z",
                "--end",
                "2024-12-31T00:00:00Z",
                "--cadence",
                "daily",
                "--value-type",
                "level",
                "--horizon-hours",
                "24",
            ]
        )
        assert args.baseline == "recession"
        assert args.cadence == "daily"
        assert args.value_type == "level"
        assert args.horizon_hours == 24

    def test_hindcast_run_rejects_unknown_baseline(self) -> None:
        parser = cli._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "hindcast",
                    "run",
                    "--baseline",
                    "lightgbm",  # PR 5; not yet wired into the CLI
                    "--gauge-id",
                    "12",
                    "--start",
                    "2024-01-01",
                    "--end",
                    "2024-01-31",
                ]
            )


class TestBuildIssueTimes:
    def test_weekly_cadence_inclusive_bounds(self) -> None:
        start = pd.Timestamp("2024-01-01T00:00:00", tz="UTC")
        end = pd.Timestamp("2024-01-29T00:00:00", tz="UTC")
        issue_times = cli._build_issue_times(
            start=start, end=end, cadence="weekly"
        )
        # 5 inclusive Mondays Jan 1, 8, 15, 22, 29.
        assert len(issue_times) == 5
        assert issue_times[0] == start
        assert issue_times[-1] == end
        deltas = {
            (issue_times[i + 1] - issue_times[i]).total_seconds()
            for i in range(len(issue_times) - 1)
        }
        assert deltas == {7 * 24 * 3600.0}

    def test_daily_cadence(self) -> None:
        start = pd.Timestamp("2024-01-01T00:00:00", tz="UTC")
        end = pd.Timestamp("2024-01-04T00:00:00", tz="UTC")
        issue_times = cli._build_issue_times(
            start=start, end=end, cadence="daily"
        )
        assert len(issue_times) == 4
        assert (issue_times[1] - issue_times[0]) == pd.Timedelta(hours=24)

    def test_hourly_cadence(self) -> None:
        start = pd.Timestamp("2024-01-01T00:00:00", tz="UTC")
        end = pd.Timestamp("2024-01-01T03:00:00", tz="UTC")
        issue_times = cli._build_issue_times(
            start=start, end=end, cadence="hourly"
        )
        assert len(issue_times) == 4

    def test_end_before_start_raises(self) -> None:
        start = pd.Timestamp("2024-01-08T00:00:00", tz="UTC")
        end = pd.Timestamp("2024-01-01T00:00:00", tz="UTC")
        with pytest.raises(ValueError, match="before"):
            cli._build_issue_times(start=start, end=end, cadence="weekly")


class TestHindcastBaselineRegistry:
    def test_persistence_and_recession_registered(self) -> None:
        # If a future PR adds a baseline (linear, lgb), this test
        # surfaces the missing entry — the CLI registry is the
        # single source of truth for what's dispatchable.
        from nokken_forecasting.baselines.persistence import (
            persistence_forecast,
        )
        from nokken_forecasting.baselines.recession import recession_forecast

        assert cli._HINDCAST_BASELINES["persistence"] is persistence_forecast
        assert cli._HINDCAST_BASELINES["recession"] is recession_forecast


class TestDispatch:
    async def test_unknown_forecast_command_returns_2(self) -> None:
        # _dispatch_forecast prints to stderr and returns 2 for an
        # unknown command. argparse normally rejects unknowns at
        # parse time, but the dispatcher's own guard is the second
        # line of defence.
        args = argparse.Namespace(group="forecast", command="not_a_baseline")
        rc = await cli._dispatch_forecast(args)
        assert rc == 2

    async def test_unknown_hindcast_command_returns_2(self) -> None:
        args = argparse.Namespace(group="hindcast", command="not_a_subcommand")
        rc = await cli._dispatch_hindcast(args)
        assert rc == 2
