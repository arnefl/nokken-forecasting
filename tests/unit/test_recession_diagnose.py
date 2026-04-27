"""Smoke test for ``scripts/recession_diagnose.py``.

The script lives outside ``src/`` (it is a one-shot diagnostic, not a
package surface), so we load it via ``importlib`` to keep ``sys.path``
unpolluted for other tests. The test exercises only the pure functions
— DB-touching code is not covered by design; the diagnostic's value is
the three live runs captured in the PR description.
"""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import pandas as pd

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "recession_diagnose.py"
_SPEC = importlib.util.spec_from_file_location("recession_diagnose", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
diag = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(diag)


def _series(rows: list[tuple[str, float]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["time", "value"])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df["gauge_id"] = 12
    df["value_type"] = "flow"
    return df[["time", "gauge_id", "value_type", "value"]]


def test_clean_descent_passes() -> None:
    # 30 hourly samples decaying exp(-0.01 t) — one peak-to-trough run,
    # >= 24 h, hourly cadence, strict monotone.
    base = pd.Timestamp("2026-01-01T00:00:00Z")
    rows = [
        ((base + pd.Timedelta(hours=h)).isoformat(), 100.0 * math.exp(-0.01 * h))
        for h in range(30)
    ]
    runs = diag.find_candidate_runs(_series(rows))
    assert len(runs) == 1
    r = runs[0]
    assert r["dur_h"] == 29.0
    assert r["gap_h"] == 1.0
    assert r["viol"] == 0
    assert diag._reasons(r) == []


def test_short_run_rejected_for_length() -> None:
    base = pd.Timestamp("2026-01-01T00:00:00Z")
    rows = [
        ((base + pd.Timedelta(hours=h)).isoformat(), 100.0 - h)
        for h in range(6)  # 5h descent — under MIN_H
    ]
    runs = diag.find_candidate_runs(_series(rows))
    assert len(runs) == 1
    reasons = diag._reasons(runs[0])
    assert any("too short" in r for r in reasons)


def test_large_gap_rejected() -> None:
    # A descent spanning 30 h but with a 4h interior gap — fails gap rule.
    rows = [
        ("2026-01-01T00:00:00Z", 100.0),
        ("2026-01-01T01:00:00Z", 99.0),
        ("2026-01-01T05:00:00Z", 95.0),  # 4h gap from previous
        ("2026-01-02T06:00:00Z", 70.0),
    ]
    runs = diag.find_candidate_runs(_series(rows))
    assert len(runs) == 1
    reasons = diag._reasons(runs[0])
    assert any("gap too large" in r for r in reasons)


def test_internal_uptick_counts_as_violation() -> None:
    # Peak -> deeper trough but with one uptick mid-run.
    rows = [
        ("2026-01-01T00:00:00Z", 100.0),
        ("2026-01-01T01:00:00Z", 90.0),
        ("2026-01-01T02:00:00Z", 95.0),  # uptick (still below peak 100)
        ("2026-01-01T03:00:00Z", 80.0),
        ("2026-01-02T00:00:00Z", 50.0),
    ]
    runs = diag.find_candidate_runs(_series(rows))
    assert len(runs) == 1
    assert runs[0]["viol"] == 1
    reasons = diag._reasons(runs[0])
    assert any("broken monotone" in r for r in reasons)
