"""One-shot diagnostic for the recession baseline's segment detector.

Reports *why* the recession baseline rejects an issue-time. Identifies
every peak-to-trough excursion in the observation series (regardless of
length, gap, or intra-run upticks) and prints the rejection reason each
would incur against the baseline's `>=24h, gaps <=2h, strict monotone`
rule. If any excursion would have fed the detector, also fits ``k`` and
prints Q(t) at horizon for sanity. Recession baseline itself unmodified.

    uv run python scripts/recession_diagnose.py --gauge-id 12 \\
        --issue-time 2020-06-03T00:00:00Z
"""

from __future__ import annotations

import argparse
import asyncio

import numpy as np
import pandas as pd

from nokken_forecasting.baselines.recession import (
    HORIZON_HOURS,
    MAX_INTRA_SEGMENT_GAP_HOURS,
    MIN_RECESSION_RUN_HOURS,
    _fit_decay_constant,
)
from nokken_forecasting.queries import connect, get_observations

GAP_H = MAX_INTRA_SEGMENT_GAP_HOURS
MIN_H = MIN_RECESSION_RUN_HOURS


def find_candidate_runs(series: pd.DataFrame) -> list[dict]:
    """Maximal peak-to-trough excursions; internal upticks/gaps allowed."""
    times = series["time"].dt.tz_localize(None).to_numpy()
    values = series["value"].to_numpy(dtype=np.float64)
    n = len(values)
    runs: list[dict] = []
    i = 0
    while i + 1 < n:
        if values[i + 1] >= values[i]:
            i += 1
            continue
        peak, trough, j = i, i + 1, i + 2
        while j < n and values[j] < values[peak]:
            if values[j] < values[trough]:
                trough = j
            j += 1
        seg = series.iloc[peak : trough + 1].reset_index(drop=True)
        diffs_h = np.diff(times[peak : trough + 1]) / np.timedelta64(1, "h")
        runs.append({
            "start": pd.Timestamp(seg.iloc[0]["time"]),
            "end": pd.Timestamp(seg.iloc[-1]["time"]),
            "dur_h": float(diffs_h.sum()),
            "gap_h": float(diffs_h.max()),
            "viol": int(np.sum(np.diff(values[peak : trough + 1]) >= 0)),
            "slice": seg,
        })
        i = trough + 1
    return runs


def _reasons(r: dict) -> list[str]:
    out: list[str] = []
    if r["dur_h"] < MIN_H:
        out.append(f"too short ({r['dur_h']:.1f}h < {MIN_H}h)")
    if r["gap_h"] > GAP_H:
        out.append(f"gap too large ({r['gap_h']:.1f}h > {GAP_H}h)")
    if r["viol"]:
        out.append(f"broken monotone ({r['viol']} non-descent pair(s))")
    return out


def _emit(runs: list[dict], *, seed: float, horizon_hours: int) -> None:
    print("\nCandidate runs (peak-to-trough; gaps and upticks allowed):")
    if not runs:
        print("  (none)")
    for n, r in enumerate(runs, start=1):
        rs = _reasons(r)
        print(f"  #{n:>3}  {r['start']} -> {r['end']}  dur={r['dur_h']:6.1f}h  "
              f"max_gap={r['gap_h']:5.1f}h  violations={r['viol']:>3}  "
              f"[{'PASS' if not rs else '; '.join(rs)}]")
    pass_len = sum(1 for r in runs if r["dur_h"] >= MIN_H)
    pass_gap = sum(1 for r in runs if r["gap_h"] <= GAP_H)
    pass_both = sum(1 for r in runs if r["dur_h"] >= MIN_H and r["gap_h"] <= GAP_H)
    detector = [r for r in runs if r["dur_h"] >= MIN_H and r["gap_h"] <= GAP_H and r["viol"] == 0]
    print(f"\nSummary: {len(runs)} candidate run(s)")
    print(f"  pass length (>= {MIN_H}h, ignore gap):    {pass_len}")
    print(f"  pass gap   (<= {GAP_H}h, ignore length):  {pass_gap}")
    print(f"  pass both length + gap (upticks ok):    {pass_both}")
    print(f"  feed detector (+ strict monotone):      {len(detector)}")
    if not detector:
        return
    try:
        k = _fit_decay_constant([r["slice"] for r in detector])
    except ValueError as exc:
        print(f"\nFit error: {exc}")
        return
    print(f"\nFit: k = {k:.6g} (1/h) across {len(detector)} segment(s)")
    for h in (24, 72, horizon_hours):
        print(f"  Q({h:>3}h) = {seed * float(np.exp(-k * h)):.4g}")


async def _run(args: argparse.Namespace) -> int:
    issue = pd.Timestamp(args.issue_time)
    issue = issue.tz_localize("UTC") if issue.tzinfo is None else issue.tz_convert("UTC")
    async with connect(close=True) as conn:
        obs = await get_observations(
            conn, gauge_id=args.gauge_id,
            start=issue - pd.Timedelta(days=args.lookback_days),
            end=issue + pd.Timedelta(seconds=1),
            value_type=args.value_type,
        )
    series = obs.sort_values("time").reset_index(drop=True)
    print(f"gauge_id={args.gauge_id}  issue_time={issue.isoformat()}  "
          f"lookback={args.lookback_days}d  value_type={args.value_type}  rows={len(series)}")
    if series.empty:
        print("no observations in lookback window")
        return 0
    print(f"series spans {series['time'].iloc[0]} -> {series['time'].iloc[-1]} "
          f"(min={series['value'].min():.4g}, max={series['value'].max():.4g})")
    _emit(find_candidate_runs(series), seed=float(series.iloc[-1]["value"]),
          horizon_hours=args.horizon_hours)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--gauge-id", type=int, default=12)
    p.add_argument("--issue-time", required=True, help="ISO-8601 UTC; naive treated as UTC.")
    p.add_argument("--lookback-days", type=int, default=90)
    p.add_argument("--value-type", choices=["flow", "level"], default="flow")
    p.add_argument("--horizon-hours", type=int, default=HORIZON_HOURS)
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
