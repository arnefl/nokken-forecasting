"""Recession baseline.

Linear-reservoir decay from the last observation:
``Q(t) = Q_0 · exp(-k · t)`` for ``t ∈ {issue_time + 1h, …, issue_time + Nh}``,
where ``Q_0`` is the most recent observation at or before ``issue_time``
and ``k`` is fit per call from monotonically-decreasing recession
segments of the historical observation series.

Per ``docs/phase3-scoping.md`` Decisions (final): horizon is 7 days
hourly (168 rows per call); ``model_version = 'recession_v1'``;
Faukstad (gauge id 12) is the only gauge this sprint exercises but
the function is gauge-id-agnostic so PR 3's hindcast harness can
reuse it across the test window.

Pure: takes a DataFrame of observations and a few scalars, returns a
list of ``ForecastRow`` payload objects sharing the dataclass with
the persistence baseline. ``model_run_at`` is stamped by the writer,
not here, so the same row-building logic serves both the live path
and the hindcast harness without branching.

Recession-segment definition
============================
The fit uses **strictly monotonically-decreasing runs** of consecutive
observations whose total span is at least ``MIN_RECESSION_RUN_HOURS``
(24 h). A "consecutive" pair is one whose timestamps lie within
``MAX_INTRA_SEGMENT_GAP_HOURS`` (2 h) — long gaps between rows
(daily-cadence publication periods, multi-month outages,
``docs/phase3-scoping.md`` §1.2) terminate a candidate segment so the
fit never crosses an outage boundary even when the values around the
gap happen to be decreasing.

The 2 h gap threshold matches the hourly + occasional-two-hourly
cadence that dominates Faukstad's observation series (~99% of
intervals at hourly, ~1.7% at two-hourly per §1.2). 3-hourly and
longer stretches are rejected because over those intervals the
linear-reservoir approximation degrades and is more likely to capture
post-event recovery than an actual recession.

Fit method
==========
For each segment, transform to ``(t_h, log(Q / Q_seg_0))`` where
``t_h`` is hours since segment start and ``Q_seg_0`` is the segment's
first value. Concatenate all segments' transformed pairs, then fit
``log(Q / Q_seg_0) = -k · t_h`` by ordinary least squares through the
origin: ``k = -Σ(t·y) / Σ(t²)`` (closed form, exact for the linear
model). Equivalent to a single-parameter exponential fit that
respects each segment's own ``Q_seg_0`` rather than fitting one
global ``Q_0`` across heterogeneous events.

Mathematically equivalent to a maximum-likelihood fit under
multiplicative log-normal noise on the recession trajectory. Numpy's
linear algebra is exact and deterministic; no scipy dependency
needed for the one-parameter case.

Fallback contract
=================
If no recession segment satisfies the length / cadence floor (i.e. the
historical series is too noisy to identify a clean recession), the
baseline raises ``ValueError`` rather than degrading silently to
persistence. Surfaces the choice up to the caller; downstream
hindcasts log the failure per issue-time and continue. The user's
PR 3 prompt makes this an explicit choice over a silent
persistence fallback.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from nokken_forecasting.baselines.persistence import ForecastRow

MODEL_VERSION = "recession_v1"
HORIZON_HOURS = 168

# A recession segment must span at least this many hours to be eligible
# for the fit. Shorter runs are noise-dominated; an event-separation
# threshold of ~24 h is the convention in linear-reservoir hydrology and
# matches the user's PR 3 prompt default.
MIN_RECESSION_RUN_HOURS = 24

# Two consecutive observations are "in-segment" only if their timestamps
# differ by at most this much. Larger gaps terminate the candidate
# segment so the fit never crosses an outage boundary or a daily-cadence
# publication run. Set to 2 h to admit the dominant hourly + two-hourly
# Faukstad cadence and reject 3-hourly+ stretches over which the
# linear-reservoir approximation degrades.
MAX_INTRA_SEGMENT_GAP_HOURS = 2


def _identify_recession_segments(
    series: pd.DataFrame,
    *,
    min_run_hours: int = MIN_RECESSION_RUN_HOURS,
    max_gap_hours: float = MAX_INTRA_SEGMENT_GAP_HOURS,
) -> list[pd.DataFrame]:
    """Return monotonically-decreasing segments of length >= ``min_run_hours``.

    ``series`` must be sorted ascending by ``time``. Each returned
    DataFrame is a contiguous slice of ``series`` representing one
    recession segment; the returned list is non-empty only when at
    least one segment satisfies both the length floor and the gap
    floor.
    """
    if len(series) < 2:
        return []

    # tz-aware datetime64[ns, UTC] doesn't survive `.to_numpy()` as a
    # numeric datetime64 — pandas hands back an object array of
    # `Timestamp`s, which `np.diff` cannot subtract numerically. Strip
    # the tz to get a plain `datetime64[ns]` view; the read pipeline
    # already guarantees UTC semantics so no clock arithmetic shifts.
    times = series["time"].dt.tz_localize(None).to_numpy()
    values = series["value"].to_numpy(dtype=np.float64)
    time_diffs_h = np.diff(times) / np.timedelta64(1, "h")
    value_diffs = np.diff(values)
    in_segment = (value_diffs < 0) & (time_diffs_h <= max_gap_hours)

    segments: list[pd.DataFrame] = []
    i = 0
    while i < len(in_segment):
        if not in_segment[i]:
            i += 1
            continue
        j = i
        while j < len(in_segment) and in_segment[j]:
            j += 1
        # Pairs i .. j-1 are all "in-segment", so the segment indexes
        # observations [i, j] inclusive. Span is times[j] - times[i].
        seg_span_h = (times[j] - times[i]) / np.timedelta64(1, "h")
        if seg_span_h >= min_run_hours:
            segments.append(series.iloc[i : j + 1])
        i = j + 1
    return segments


def _fit_decay_constant(segments: list[pd.DataFrame]) -> float:
    """Fit a single recession constant ``k`` (1/hours) across segments.

    Each segment contributes ``(t_h_since_start, log(Q/Q_seg_0))``
    pairs; the fit is the OLS-through-origin slope on the concatenated
    pairs. Segments containing non-positive values are skipped (the log
    is undefined). Raises ``ValueError`` if no segment yields usable
    pairs or if the resulting fit is not a decay (slope >= 0).
    """
    t_pieces: list[np.ndarray] = []
    y_pieces: list[np.ndarray] = []
    for seg in segments:
        seg_times = seg["time"].dt.tz_localize(None).to_numpy()
        seg_vals = seg["value"].to_numpy(dtype=np.float64)
        if (seg_vals <= 0).any():
            continue
        t_h = (seg_times - seg_times[0]) / np.timedelta64(1, "h")
        # Skip the t=0 point: log(Q/Q_seg_0) = 0 contributes nothing to
        # an OLS-through-origin fit and only inflates the denominator's
        # sum-of-squares by zero, but excluding it keeps the geometry
        # of "segment without anchor" explicit.
        if len(t_h) <= 1:
            continue
        y = np.log(seg_vals[1:] / seg_vals[0])
        t_pieces.append(t_h[1:])
        y_pieces.append(y)

    if not t_pieces:
        raise ValueError(
            "no usable recession segments to fit (all segments contained "
            "non-positive values or had insufficient post-anchor points)"
        )

    t_all = np.concatenate(t_pieces)
    y_all = np.concatenate(y_pieces)
    denom = float(np.sum(t_all * t_all))
    if denom == 0.0:
        raise ValueError("recession-fit degenerate: zero-variance time axis")
    slope = float(np.sum(t_all * y_all) / denom)
    if slope >= 0.0:
        raise ValueError(
            f"recession-fit yielded non-decay slope {slope:.6g}; series "
            "appears too noisy for a recession baseline"
        )
    return -slope


def recession_forecast(
    observations: pd.DataFrame,
    *,
    gauge_id: int,
    issue_time: pd.Timestamp,
    value_type: str = "flow",
    horizon_hours: int = HORIZON_HOURS,
) -> list[ForecastRow]:
    """Forecast = exponential decay from the last observation.

    ``observations`` is the DataFrame returned by ``get_observations`` —
    columns ``time, gauge_id, value_type, value`` with ``time`` tz-aware
    UTC. The function:

    1. Filters to ``value_type`` rows at or before ``issue_time``.
    2. Identifies recession segments per the module-level definition.
    3. Fits a single decay constant ``k`` across those segments.
    4. Emits ``Q(t) = Q_0 · exp(-k · t)`` rows for ``t ∈ {1, …, horizon_hours}``.

    Raises ``ValueError`` if ``issue_time`` is naive, ``horizon_hours``
    is non-positive, the filtered series has fewer than 2 rows, or no
    recession segment satisfies the length / gap floor (i.e. the series
    is too noisy to identify a clean recession). The fallback contract
    is **raise**, not "degrade to persistence" — see the module
    docstring's "Fallback contract" section.
    """
    if issue_time.tzinfo is None:
        raise ValueError("issue_time must be tz-aware (UTC); got naive Timestamp")
    if horizon_hours <= 0:
        raise ValueError(f"horizon_hours must be positive; got {horizon_hours}")
    if observations.empty:
        raise ValueError(
            f"no observations available for gauge {gauge_id} at or before {issue_time}"
        )

    series = observations[observations["value_type"] == value_type]
    series = series[series["time"] <= issue_time]
    if series.empty:
        raise ValueError(
            f"no '{value_type}' observations for gauge {gauge_id} "
            f"at or before {issue_time}"
        )
    if len(series) < 2:
        raise ValueError(
            f"recession baseline needs >= 2 '{value_type}' observations to "
            f"fit decay constant; got {len(series)} for gauge {gauge_id} "
            f"at or before {issue_time}"
        )

    series = series.sort_values("time").reset_index(drop=True)
    segments = _identify_recession_segments(series)
    if not segments:
        raise ValueError(
            f"no recession segments (>= {MIN_RECESSION_RUN_HOURS} h, gaps "
            f"<= {MAX_INTRA_SEGMENT_GAP_HOURS} h) found for gauge {gauge_id} "
            f"at or before {issue_time}; series is too noisy / sparse "
            "for a recession baseline"
        )
    k = _fit_decay_constant(segments)

    seed = float(series.iloc[-1]["value"])
    return [
        ForecastRow(
            issue_time=issue_time,
            valid_time=issue_time + pd.Timedelta(hours=h),
            gauge_id=gauge_id,
            value_type=value_type,
            quantile=None,
            value=seed * float(np.exp(-k * h)),
            model_version=MODEL_VERSION,
        )
        for h in range(1, horizon_hours + 1)
    ]
