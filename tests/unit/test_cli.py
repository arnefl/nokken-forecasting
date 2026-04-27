"""Thin CLI parser / dispatcher tests.

Exercises argument parsing and dispatch routing for the new
``forecast persistence`` subcommand without touching Postgres. The
full read → model → write path is covered by the unit and
integration tests for the components themselves; this layer just
asserts the wiring is intact.
"""

from __future__ import annotations

import argparse

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


class TestDispatch:
    async def test_unknown_forecast_command_returns_2(self) -> None:
        # _dispatch_forecast prints to stderr and returns 2 for an
        # unknown command. argparse normally rejects unknowns at
        # parse time, but the dispatcher's own guard is the second
        # line of defence.
        args = argparse.Namespace(group="forecast", command="not_a_baseline")
        rc = await cli._dispatch_forecast(args)
        assert rc == 2
