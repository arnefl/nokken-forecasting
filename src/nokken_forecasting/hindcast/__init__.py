"""Hindcast harness — runs any baseline at arbitrary historical issue-times.

The harness is the read-once-fit-once-write-once-per-issue-time loop
that lets PR 6's comparison report join hindcast rows to observations
via the existing query layer. It is single-gauge by design: the CLI /
operator-runbook layer handles multi-gauge orchestration; the harness
does one gauge over many issue-times.
"""

from __future__ import annotations

from nokken_forecasting.hindcast.harness import (
    HindcastSummary,
    IssueTimeOutcome,
    run_hindcast,
)

__all__ = [
    "HindcastSummary",
    "IssueTimeOutcome",
    "run_hindcast",
]
