"""Scheduled job entry points.

Each module under :mod:`nokken_forecasting.jobs` is a unit of work the
production deploy invokes on a timer. Today there is one — the daily
persistence-baseline tick wired into ``deploy/nokken-forecasting-forecast.{service,timer}``.

Job modules are thin wrappers: they reuse the ``baselines/`` /
``writers/`` / ``queries/`` building blocks the manual ``forecast``
CLI subcommands already exercise. No business logic lives here that
isn't reachable from a CLI subcommand somewhere else, so an operator
debugging a job tick can always reproduce it manually.
"""

from __future__ import annotations

from nokken_forecasting.jobs.forecast_job import (
    FORECAST_GAUGES,
    JobOutcome,
    JobSummary,
    run_forecast_job,
)

__all__ = [
    "FORECAST_GAUGES",
    "JobOutcome",
    "JobSummary",
    "run_forecast_job",
]
