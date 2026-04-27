"""Unit tests for the recession baseline.

Pure-function module — no DB, no fixtures. Each test builds a
DataFrame matching the ``get_observations`` shape and asserts on the
emitted ``ForecastRow`` list.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from nokken_forecasting.baselines.persistence import ForecastRow
from nokken_forecasting.baselines.recession import (
    HORIZON_HOURS,
    MAX_INTRA_SEGMENT_GAP_HOURS,
    MIN_RECESSION_RUN_HOURS,
    MODEL_VERSION,
    UPTICK_TOLERANCE,
    recession_forecast,
)


def _obs_frame(rows: list[tuple[pd.Timestamp, str, float]]) -> pd.DataFrame:
    """Build a DataFrame matching `get_observations`'s output shape."""
    df = pd.DataFrame(rows, columns=["time", "value_type", "value"])
    df["gauge_id"] = 12
    df = df[["time", "gauge_id", "value_type", "value"]]
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df["gauge_id"] = df["gauge_id"].astype("int64")
    df["value"] = df["value"].astype("float64")
    return df


def _synthetic_recession(
    *,
    issue_time: pd.Timestamp,
    q0: float,
    k_true: float,
    history_hours: int,
) -> pd.DataFrame:
    """Hourly history of a clean ``Q(t) = q0 · exp(-k_true · t)`` recession.

    ``history_hours`` rows ending at ``issue_time``, hourly cadence,
    one strictly-decreasing run (ergo one recession segment).
    """
    rows = [
        (
            issue_time - pd.Timedelta(hours=history_hours - 1 - h),
            "flow",
            float(q0 * math.exp(-k_true * h)),
        )
        for h in range(history_hours)
    ]
    return _obs_frame(rows)


class TestRecessionForecast:
    def test_recovers_synthetic_decay_constant(self) -> None:
        # Drop a clean Q = 100 * exp(-0.01*t) recession of 48 h into the
        # function and assert the forecast curve matches the same model
        # to better than 0.1% per row. This pins the OLS-through-origin
        # fit to the analytic answer with no scipy dependency.
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        k_true = 0.01
        q0 = 100.0
        obs = _synthetic_recession(
            issue_time=issue, q0=q0, k_true=k_true, history_hours=48
        )

        rows = recession_forecast(obs, gauge_id=12, issue_time=issue)
        assert len(rows) == HORIZON_HOURS
        assert all(r.model_version == MODEL_VERSION for r in rows)
        assert all(r.quantile is None for r in rows)
        assert all(r.value_type == "flow" for r in rows)

        # Seed = last observation = q0 * exp(-k_true * (history_hours-1)).
        seed = float(obs.iloc[-1]["value"])
        for h, row in enumerate(rows, start=1):
            expected = seed * math.exp(-k_true * h)
            assert row.value == pytest.approx(expected, rel=1e-3)
            assert row.valid_time == issue + pd.Timedelta(hours=h)
            assert row.issue_time == issue
            assert row.gauge_id == 12

        assert isinstance(rows[0], ForecastRow)

    def test_horizon_hours_respected(self) -> None:
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        obs = _synthetic_recession(
            issue_time=issue, q0=50.0, k_true=0.005, history_hours=48
        )
        rows = recession_forecast(
            obs, gauge_id=12, issue_time=issue, horizon_hours=12
        )
        assert len(rows) == 12
        # Strictly decreasing forecast (k > 0).
        values = [r.value for r in rows]
        assert all(values[i + 1] < values[i] for i in range(len(values) - 1))

    def test_value_type_filters_series(self) -> None:
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        # Two interleaved series: a clean 'level' recession plus a
        # noisy 'flow' series. With value_type='level' the recession
        # baseline should ignore the flow rows and fit on the level
        # recession alone — and emit value_type='level' rows.
        level_obs = _synthetic_recession(
            issue_time=issue, q0=2.0, k_true=0.005, history_hours=48
        ).assign(value_type="level")
        flow_rows = [
            (issue - pd.Timedelta(hours=h), "flow", 100.0 if h % 2 else 80.0)
            for h in range(48)
        ]
        flow_obs = _obs_frame(flow_rows)
        obs = pd.concat([level_obs, flow_obs], ignore_index=True)

        rows = recession_forecast(
            obs, gauge_id=12, issue_time=issue, value_type="level"
        )
        assert all(r.value_type == "level" for r in rows)
        # Seed = level series' last observation; first forecast row is
        # one decay step ahead. The fit ignores the flow rows entirely.
        level_seed = float(level_obs.sort_values("time").iloc[-1]["value"])
        assert rows[0].value < level_seed
        assert rows[0].value == pytest.approx(
            level_seed * math.exp(-0.005 * 1), rel=1e-2
        )

    def test_empty_frame_raises(self) -> None:
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        with pytest.raises(ValueError, match="no observations"):
            recession_forecast(_obs_frame([]), gauge_id=12, issue_time=issue)

    def test_no_rows_at_or_before_issue_raises(self) -> None:
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        # All rows are *after* issue_time.
        obs = _obs_frame(
            [(issue + pd.Timedelta(hours=h), "flow", 10.0) for h in range(1, 4)]
        )
        with pytest.raises(ValueError, match="at or before"):
            recession_forecast(obs, gauge_id=12, issue_time=issue)

    def test_single_observation_rejected(self) -> None:
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        obs = _obs_frame([(issue - pd.Timedelta(hours=1), "flow", 7.5)])
        with pytest.raises(ValueError, match=">= 2"):
            recession_forecast(obs, gauge_id=12, issue_time=issue)

    def test_naive_issue_time_rejected(self) -> None:
        issue_naive = pd.Timestamp("2026-04-27T12:00:00")
        obs = _obs_frame(
            [
                (
                    pd.Timestamp("2026-04-27T11:00:00", tz="UTC"),
                    "flow",
                    10.0,
                ),
                (
                    pd.Timestamp("2026-04-27T12:00:00", tz="UTC"),
                    "flow",
                    9.0,
                ),
            ]
        )
        with pytest.raises(ValueError, match="tz-aware"):
            recession_forecast(obs, gauge_id=12, issue_time=issue_naive)

    def test_non_positive_horizon_rejected(self) -> None:
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        obs = _synthetic_recession(
            issue_time=issue, q0=10.0, k_true=0.01, history_hours=24
        )
        with pytest.raises(ValueError, match="horizon_hours must be positive"):
            recession_forecast(
                obs, gauge_id=12, issue_time=issue, horizon_hours=0
            )

    def test_too_noisy_series_raises(self) -> None:
        # A flat / oscillating series produces no monotonically-
        # decreasing run of >= 24 h. Per the module's fallback contract,
        # the baseline raises rather than silently degrading to a
        # persistence-equivalent flat curve.
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        rng = np.random.default_rng(seed=42)
        rows = [
            (
                issue - pd.Timedelta(hours=h),
                "flow",
                10.0 + float(rng.uniform(-1.0, 1.0)),
            )
            for h in range(48)
        ]
        obs = _obs_frame(rows)
        with pytest.raises(ValueError, match="recession segments"):
            recession_forecast(obs, gauge_id=12, issue_time=issue)

    def test_short_descending_run_below_floor_rejected(self) -> None:
        # 12-hour monotonic descent then a rise — under the 24 h floor
        # the only candidate segment is dropped, and the baseline raises.
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        rows: list[tuple[pd.Timestamp, str, float]] = []
        for h in range(12):  # descent
            rows.append((issue - pd.Timedelta(hours=23 - h), "flow", 100.0 - h))
        for h in range(12):  # rise
            rows.append((issue - pd.Timedelta(hours=11 - h), "flow", 50.0 + h))
        obs = _obs_frame(rows)
        with pytest.raises(ValueError, match="recession segments"):
            recession_forecast(obs, gauge_id=12, issue_time=issue)

    def test_outage_breaks_segment(self) -> None:
        # 30 h of clean descent followed by a 48 h outage and 30 h of
        # different-rate descent. Without the gap guard the algorithm
        # would treat the descents as one segment (both are decreasing
        # and the across-gap diff is also negative); with the guard,
        # each is its own segment and the fit blends both rates.
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        # Earlier segment: q0=200, k=0.02, 30 h.
        early_start = issue - pd.Timedelta(hours=30 + 48 + 30)
        early_rows = [
            (
                early_start + pd.Timedelta(hours=h),
                "flow",
                200.0 * math.exp(-0.02 * h),
            )
            for h in range(30)
        ]
        # Outage skipped (no rows for 48 h).
        # Later segment: q0=80, k=0.005, 30 h.
        later_start = issue - pd.Timedelta(hours=30)
        later_rows = [
            (
                later_start + pd.Timedelta(hours=h),
                "flow",
                80.0 * math.exp(-0.005 * h),
            )
            for h in range(30)
        ]
        obs = _obs_frame(early_rows + later_rows)
        rows = recession_forecast(obs, gauge_id=12, issue_time=issue)
        # Sanity: the fit produced *some* valid output.
        assert len(rows) == HORIZON_HOURS
        assert rows[0].value < float(obs.iloc[-1]["value"])
        # The fitted k should sit between the two segment rates after a
        # 1 h step. Both rate bounds are loose: just verify a decay.
        seed = float(obs.iloc[-1]["value"])
        ratio = rows[0].value / seed
        assert math.exp(-0.02 * 1) < ratio < 1.0

    def test_horizon_uses_seed_at_or_before_issue(self) -> None:
        # If the most recent observation is strictly before issue_time
        # (e.g. issue_time falls on the next hour), the seed is still
        # that most-recent row and the forecast emits H+1..H+N rows
        # spaced from issue_time.
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        last_obs_t = issue - pd.Timedelta(minutes=10)
        # Build a clean recession ending at last_obs_t. The 'time' axis
        # here is hourly, but we add the +last_obs_t observation as the
        # final row to exercise the at-or-before semantics.
        history = _synthetic_recession(
            issue_time=last_obs_t, q0=100.0, k_true=0.01, history_hours=48
        )
        rows = recession_forecast(history, gauge_id=12, issue_time=issue)
        # Seed equals the most-recent observation's value.
        seed = float(history.sort_values("time").iloc[-1]["value"])
        assert rows[0].value == pytest.approx(seed * math.exp(-0.01), rel=1e-2)
        assert rows[0].valid_time == issue + pd.Timedelta(hours=1)

    def test_constants_match_docstring(self) -> None:
        # Pin the public constants so a refactor that quietly changes
        # the segment-floor or the gap-threshold trips a unit test.
        assert MIN_RECESSION_RUN_HOURS == 24
        assert MAX_INTRA_SEGMENT_GAP_HOURS == 2
        assert UPTICK_TOLERANCE == 0.01
        assert MODEL_VERSION == "recession_v1"
        assert HORIZON_HOURS == 168


class TestUptickTolerance:
    """PR 3.6 — pairwise monotone-descent relaxed to a 1% tolerance."""

    def _noisy_decay(
        self, *, issue: pd.Timestamp, q0: float, k_true: float,
        history_hours: int, noise: float, seed: int,
    ) -> pd.DataFrame:
        rng = np.random.default_rng(seed=seed)
        rows = [
            (
                issue - pd.Timedelta(hours=history_hours - 1 - h),
                "flow",
                float(q0 * math.exp(-k_true * h) * (1.0 + rng.uniform(-noise, noise))),
            )
            for h in range(history_hours)
        ]
        return _obs_frame(rows)

    def test_clean_decay_still_fits(self) -> None:
        # Sanity: tolerance relaxation doesn't break the noise-free path.
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        obs = _synthetic_recession(
            issue_time=issue, q0=100.0, k_true=0.01, history_hours=30
        )
        rows = recession_forecast(obs, gauge_id=12, issue_time=issue)
        assert len(rows) == HORIZON_HOURS
        seed = float(obs.iloc[-1]["value"])
        assert rows[0].value == pytest.approx(seed * math.exp(-0.01), rel=1e-3)

    def test_one_percent_noise_fits(self) -> None:
        # 30 h decay with i.i.d. ±1% multiplicative noise. Strict-monotone
        # would reject; the tolerance admits it. Recovered k is close to
        # the true rate (loose bound — noise corrupts the fit somewhat).
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        obs = self._noisy_decay(
            issue=issue, q0=100.0, k_true=0.02, history_hours=30,
            noise=0.01, seed=7,
        )
        rows = recession_forecast(obs, gauge_id=12, issue_time=issue)
        assert len(rows) == HORIZON_HOURS
        # Values are strictly decreasing (k > 0 was recovered).
        values = [r.value for r in rows]
        assert all(values[i + 1] < values[i] for i in range(len(values) - 1))

    def test_five_percent_noise_rejected(self) -> None:
        # ±5% multiplicative noise produces upticks well outside the 1%
        # tolerance — the segment detector chops it into many short runs,
        # each below the 24h floor, so the baseline raises.
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        obs = self._noisy_decay(
            issue=issue, q0=100.0, k_true=0.02, history_hours=30,
            noise=0.05, seed=7,
        )
        with pytest.raises(ValueError, match="recession segments|non-decay"):
            recession_forecast(obs, gauge_id=12, issue_time=issue)

    def test_monotone_ascent_within_tolerance_rejected(self) -> None:
        # Q[i+1] = Q[i] * 1.01 — every pair sits exactly at the upper edge
        # of the tolerance, so the relaxed in-segment mask admits the run.
        # The slope >= 0 check in _fit_decay_constant must catch it: the
        # tolerance must not invert the rule's direction.
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        rows: list[tuple[pd.Timestamp, str, float]] = []
        q = 10.0
        for h in range(30):
            rows.append((issue - pd.Timedelta(hours=29 - h), "flow", q))
            q *= 1.0 + UPTICK_TOLERANCE
        obs = _obs_frame(rows)
        with pytest.raises(ValueError, match="non-decay|recession segments"):
            recession_forecast(obs, gauge_id=12, issue_time=issue)

    def test_short_clean_fragment_still_rejected_by_length(self) -> None:
        # 8 hours of clean exponential decay is unambiguously a recession
        # but sits below the 24h length floor — relaxation must not lower
        # the floor. Sanity-check that length is still binding.
        issue = pd.Timestamp("2026-04-27T12:00:00", tz="UTC")
        obs = _synthetic_recession(
            issue_time=issue, q0=100.0, k_true=0.01, history_hours=8
        )
        with pytest.raises(ValueError, match="recession segments"):
            recession_forecast(obs, gauge_id=12, issue_time=issue)
