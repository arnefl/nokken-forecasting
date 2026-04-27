"""CLI dispatcher for ``nokken-forecasting``.

Four sub-apps today:

* ``inspect``  — read-only tour of the shared Postgres schema
  (Phase 2).
* ``query``    — typed-DataFrame readers backing the Phase 3
  baselines exposed for ad-hoc operator spot-checks (Phase 3b).
  Output mirrors ``inspect``'s aligned-text / ``--json`` shape.
* ``forecast`` — run a baseline against ``forecasts``. Two flavours:

  * ``forecast persistence`` — manual operator path against one
    gauge + issue time. Useful for ad-hoc backfills and
    incident-response replays.
  * ``forecast run`` — the unattended path the systemd timer at
    ``deploy/nokken-forecasting-forecast.timer`` invokes. Iterates
    every gauge in ``jobs.forecast_job.FORECAST_GAUGES`` and writes
    one tick's rows in a single connection. Idempotent on the
    writer's deterministic uniqueness key.
* ``hindcast`` — replay a baseline at historical issue-times so
  PR 6's comparison report has rows to score against:

  * ``hindcast run`` — manual operator path. Builds an issue-time
    list from ``--start``/``--end``/``--cadence`` and dispatches
    via :func:`nokken_forecasting.hindcast.run_hindcast`. Every row
    written by one invocation shares one ``model_run_at`` stamp;
    rerunning is a no-op on the writer's uniqueness key.

All four ride the single pool from
``nokken_forecasting.db.postgres.get_pool``. The pool sets
``default_transaction_read_only = on`` at session init so reads are
defended in depth; the ``forecast`` and ``hindcast`` writers opt into
a read-write transaction inside ``insert_forecasts`` so writes land
while adjacent reads on the same connection stay defended.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from nokken_forecasting.baselines.persistence import (
    HORIZON_HOURS,
    persistence_forecast,
)
from nokken_forecasting.baselines.recession import recession_forecast
from nokken_forecasting.config import get_settings
from nokken_forecasting.db import inspect as db_inspect
from nokken_forecasting.db.postgres import close_pool
from nokken_forecasting.hindcast import run_hindcast
from nokken_forecasting.jobs.forecast_job import run_forecast_job
from nokken_forecasting.logging import configure_logging
from nokken_forecasting.queries import (
    connect,
    get_gauges,
    get_observations,
    get_sections,
    get_weather_forecast_at_lead,
    get_weather_forecast_latest_as_of,
    get_weather_observations,
)
from nokken_forecasting.writers import insert_forecasts

_LOG = logging.getLogger("nokken_forecasting.cli")

# Look-back window over which we fetch observations to seed the
# persistence baseline. 7 days is generous: any cadence covered by
# §1.2 of the scoping doc (hourly / daily / two-hourly) yields at
# least one row inside this window. Keeps the read narrow without
# risking an "empty series" failure for a recently-published gauge.
_PERSISTENCE_LOOKBACK_HOURS = 24 * 7

# Look-back window for the hindcast harness. Wider than the live path
# because the recession baseline needs enough history to identify a
# >= 24 h monotonic-decay segment, and Faukstad's mixed-cadence series
# (§1.2 — dominant hourly with occasional 2 h / daily / outage stretches)
# can leave most of a 30-day window with no usable segment. 90 days is
# the safe floor; recession's fit through-origin OLS is closed-form so
# the extra rows have negligible cost. Persistence ignores the bulk of
# the window — it only seeds from the most-recent row.
_HINDCAST_LOOKBACK_HOURS = 24 * 90

# Mapping from --cadence flag to a pandas frequency alias. ``W-MON``
# anchors weekly steps to Mondays so a weekly run starting on a Monday
# emits issue-times exactly 7 days apart; the unanchored ``W`` would
# silently shift to Sundays.
_CADENCE_FREQ: dict[str, str] = {
    "hourly": "h",
    "daily": "D",
    "weekly": "7D",
}

# Map from --baseline flag to the pure baseline function. Adding a new
# baseline is one line here plus a choice in ``_build_hindcast_parser``.
_HINDCAST_BASELINES = {
    "persistence": persistence_forecast,
    "recession": recession_forecast,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nokken-forecasting",
        description="nokken-forecasting CLI — read-only DB inspection (Phase 2).",
    )
    sub = parser.add_subparsers(dest="group", required=True)

    inspect = sub.add_parser("inspect", help="Read-only schema / data inspection.")
    inspect.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human-readable text.",
    )
    inspect_sub = inspect.add_subparsers(dest="command", required=True)

    inspect_sub.add_parser(
        "tables", help="List public-schema tables; flag Timescale hypertables."
    )

    describe = inspect_sub.add_parser(
        "describe", help="Columns, types, PK, indexes, hypertable info."
    )
    describe.add_argument("table")

    count = inspect_sub.add_parser(
        "count", help="Row count + MIN/MAX of the time column where applicable."
    )
    count.add_argument("table")

    sample = inspect_sub.add_parser("sample", help="Sample rows from a table.")
    sample.add_argument("table")
    sample.add_argument("--limit", type=int, default=10, help="Row cap (default 10).")
    sample.add_argument(
        "--where",
        default=None,
        help=(
            "Parameterised WHERE fragment (no trailing ';', no SQL comments). "
            "The real safety is the read-only session."
        ),
    )

    query = inspect_sub.add_parser("query", help="Run an arbitrary SELECT.")
    query.add_argument("sql", help="SQL statement; must begin with SELECT.")

    _build_query_parser(sub)
    _build_forecast_parser(sub)
    _build_hindcast_parser(sub)
    return parser


def _build_query_parser(sub: argparse._SubParsersAction) -> None:
    query = sub.add_parser(
        "query",
        help="Typed-DataFrame readers (gauges / sections / observations / weather).",
    )
    query.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON-lines (NDJSON) instead of an aligned table.",
    )
    query.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap output rows after the read (post-DataFrame slice).",
    )
    query_sub = query.add_subparsers(dest="command", required=True)

    gauges = query_sub.add_parser("gauges", help="List gauge metadata.")
    gauges.add_argument(
        "--gauge-id",
        type=int,
        action="append",
        default=None,
        help="Filter to one or more gauges (repeat the flag).",
    )

    sections = query_sub.add_parser("sections", help="List paddling sections.")
    sections.add_argument(
        "--gauge-id",
        type=int,
        action="append",
        default=None,
        help="Filter to sections whose gauge is in this list (repeat).",
    )

    obs = query_sub.add_parser(
        "observations", help="Historical gauge readings over a window."
    )
    obs.add_argument("--gauge-id", type=int, required=True)
    obs.add_argument("--start", required=True, help="ISO-8601 UTC, e.g. 2025-04-01.")
    obs.add_argument("--end", required=True, help="ISO-8601 UTC, exclusive bound.")
    obs.add_argument(
        "--value-type",
        choices=["flow", "level"],
        default=None,
        help="Optional filter (default: both).",
    )

    wobs = query_sub.add_parser(
        "weather-observations", help="Historical hourly forcing for one gauge."
    )
    wobs.add_argument("--gauge-id", type=int, required=True)
    wobs.add_argument("--start", required=True)
    wobs.add_argument("--end", required=True)
    wobs.add_argument(
        "--variable",
        action="append",
        default=None,
        help="Filter to one or more variables (repeat the flag).",
    )
    wobs.add_argument("--source", default=None, help="Filter to a single source.")

    wfl = query_sub.add_parser(
        "weather-forecast-latest",
        help="Latest forecast issued at or before --as-of (per source).",
    )
    wfl.add_argument("--gauge-id", type=int, required=True)
    wfl.add_argument(
        "--as-of",
        required=True,
        help="ISO-8601 UTC; ``now`` for the current wall-clock UTC.",
    )
    wfl.add_argument(
        "--horizon",
        type=int,
        default=None,
        help="Truncate to valid_time <= issue_time + N hours.",
    )
    wfl.add_argument("--variable", action="append", default=None)
    wfl.add_argument("--source", default=None)

    wal = query_sub.add_parser(
        "weather-forecast-at-lead",
        help="Forecast available --lead-hours before --target-time (per source).",
    )
    wal.add_argument("--gauge-id", type=int, required=True)
    wal.add_argument("--target-time", required=True, help="ISO-8601 UTC.")
    wal.add_argument("--lead-hours", type=int, required=True)
    wal.add_argument(
        "--tolerance-hours",
        type=int,
        default=0,
        help="Allow valid_time within ±N hours of target_time (default 0).",
    )
    wal.add_argument("--variable", action="append", default=None)
    wal.add_argument("--source", default=None)


def _build_forecast_parser(sub: argparse._SubParsersAction) -> None:
    forecast = sub.add_parser(
        "forecast",
        help="Run a baseline and write the rows into `forecasts`.",
    )
    forecast_sub = forecast.add_subparsers(dest="command", required=True)

    persistence = forecast_sub.add_parser(
        "persistence",
        help="Persistence baseline (`persistence_v1`): last obs held flat.",
    )
    persistence.add_argument("--gauge-id", type=int, required=True)
    # Default to wall-clock now() so manual operator runs don't need
    # to compute the timestamp; the scheduled job (PR 2) will pass
    # `--issue-time` explicitly aligned to its cron cadence (§4.1
    # leaves cadence open beyond "daily" for PR 2's systemd timer).
    persistence.add_argument(
        "--issue-time",
        default=None,
        help=(
            "ISO-8601 UTC issue time, e.g. 2026-04-27T12:00:00Z. "
            "Naive inputs are interpreted as UTC. "
            "Default: current wall-clock UTC."
        ),
    )
    persistence.add_argument(
        "--value-type",
        choices=["flow", "level"],
        default="flow",
        help="Observation column to persist (default: flow).",
    )
    persistence.add_argument(
        "--horizon-hours",
        type=int,
        default=HORIZON_HOURS,
        help=f"Forecast horizon in hours (default: {HORIZON_HOURS} = 7 days).",
    )

    run = forecast_sub.add_parser(
        "run",
        help=(
            "Unattended scheduled-job tick: persistence baseline "
            "against every gauge in FORECAST_GAUGES."
        ),
    )
    # `--issue-time` is optional and defaults to wall-clock now floored
    # to top-of-hour inside the job module. The systemd timer fires at
    # 00:00 UTC daily so the floor is a no-op for the production path;
    # it matters for `Persistent=true` catch-up runs after a host
    # outage and for ad-hoc operator invocations.
    run.add_argument(
        "--issue-time",
        default=None,
        help=(
            "ISO-8601 UTC issue time. Naive inputs raise. "
            "Default: current wall-clock UTC floored to the top of the hour."
        ),
    )
    run.add_argument(
        "--value-type",
        choices=["flow", "level"],
        default="flow",
        help="Observation column to persist (default: flow).",
    )
    run.add_argument(
        "--horizon-hours",
        type=int,
        default=HORIZON_HOURS,
        help=f"Forecast horizon in hours (default: {HORIZON_HOURS} = 7 days).",
    )


def _build_hindcast_parser(sub: argparse._SubParsersAction) -> None:
    hindcast = sub.add_parser(
        "hindcast",
        help=(
            "Replay a baseline at historical issue-times and write rows "
            "to `forecasts`."
        ),
    )
    hindcast_sub = hindcast.add_subparsers(dest="command", required=True)

    run = hindcast_sub.add_parser(
        "run",
        help=(
            "Build an issue-time list from --start/--end/--cadence and "
            "dispatch the named baseline through the harness. Every row "
            "written by one invocation shares one model_run_at stamp."
        ),
    )
    run.add_argument(
        "--baseline",
        choices=sorted(_HINDCAST_BASELINES),
        required=True,
        help="Which baseline to replay (persistence | recession).",
    )
    run.add_argument(
        "--gauge-id",
        type=int,
        required=True,
        help="Single gauge to hindcast against — multi-gauge is post-Phase-3.",
    )
    run.add_argument(
        "--start",
        required=True,
        help=(
            "ISO-8601 UTC start of the issue-time window (inclusive). "
            "Naive inputs interpreted as UTC."
        ),
    )
    run.add_argument(
        "--end",
        required=True,
        help=(
            "ISO-8601 UTC end of the issue-time window (inclusive). "
            "Naive inputs interpreted as UTC."
        ),
    )
    run.add_argument(
        "--cadence",
        choices=sorted(_CADENCE_FREQ),
        default="weekly",
        help=(
            "Spacing between consecutive issue-times (default: weekly). "
            "Weekly produces ~52 issue-times per year; 'daily' and "
            "'hourly' are available for tighter windows."
        ),
    )
    run.add_argument(
        "--value-type",
        choices=["flow", "level"],
        default="flow",
        help="Observation column the baseline projects (default: flow).",
    )
    run.add_argument(
        "--horizon-hours",
        type=int,
        default=HORIZON_HOURS,
        help=f"Forecast horizon in hours (default: {HORIZON_HOURS} = 7 days).",
    )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.group not in {"inspect", "query", "forecast", "hindcast"}:
        parser.error(f"unknown group: {args.group}")
    configure_logging(get_settings().log_level)
    return asyncio.run(_run(args))


async def _run(args: argparse.Namespace) -> int:
    # The pool is bound to the active event loop, so opening and
    # closing must happen inside the same asyncio.run() invocation.
    try:
        if args.group == "inspect":
            return await _dispatch_inspect(args)
        if args.group == "query":
            return await _dispatch_query(args)
        if args.group == "forecast":
            return await _dispatch_forecast(args)
        return await _dispatch_hindcast(args)
    finally:
        await close_pool()


async def _dispatch_inspect(args: argparse.Namespace) -> int:
    try:
        if args.command == "tables":
            result = await db_inspect.list_tables()
            _emit_tables(result, as_json=args.json)
        elif args.command == "describe":
            result = await db_inspect.describe_table(args.table)
            _emit_describe(result, as_json=args.json)
        elif args.command == "count":
            result = await db_inspect.count_table(args.table)
            _emit_count(result, as_json=args.json)
        elif args.command == "sample":
            result = await db_inspect.sample_table(
                args.table, limit=args.limit, where=args.where
            )
            _emit_rows(result, as_json=args.json)
        elif args.command == "query":
            result = await db_inspect.run_query(args.sql)
            _emit_rows(result, as_json=args.json)
        else:
            print(f"unknown command: {args.command}", file=sys.stderr)
            return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


async def _dispatch_query(args: argparse.Namespace) -> int:
    try:
        async with connect() as conn:
            df = await _run_query_command(conn, args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.limit is not None and args.limit >= 0:
        df = df.head(args.limit)
    _emit_dataframe(df, as_json=args.json)
    return 0


async def _dispatch_forecast(args: argparse.Namespace) -> int:
    if args.command == "persistence":
        return await _dispatch_forecast_persistence(args)
    if args.command == "run":
        return await _dispatch_forecast_run(args)
    print(f"unknown command: {args.command}", file=sys.stderr)
    return 2


async def _dispatch_forecast_persistence(args: argparse.Namespace) -> int:
    try:
        issue_time = (
            pd.Timestamp.now(tz="UTC")
            if args.issue_time is None
            else _parse_ts(args.issue_time)
        )
    except ValueError as exc:
        print(f"error: invalid --issue-time: {exc}", file=sys.stderr)
        return 2
    lookback_start = issue_time - pd.Timedelta(hours=_PERSISTENCE_LOOKBACK_HOURS)
    # Half-open window upper bound is exclusive — push past `issue_time`
    # by one second to keep an observation stamped exactly at
    # `issue_time` inside the read window.
    lookback_end = issue_time + pd.Timedelta(seconds=1)

    try:
        async with connect() as conn:
            obs = await get_observations(
                conn,
                gauge_id=args.gauge_id,
                start=lookback_start,
                end=lookback_end,
                value_type=args.value_type,
            )
            rows = persistence_forecast(
                obs,
                gauge_id=args.gauge_id,
                issue_time=issue_time,
                value_type=args.value_type,
                horizon_hours=args.horizon_hours,
            )
            model_run_at = datetime.now(UTC)
            inserted = await insert_forecasts(
                conn, rows, model_run_at=model_run_at
            )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _LOG.info(
        "persistence forecast written",
        extra={
            "event": "forecast.persistence",
            "gauge_id": args.gauge_id,
            "issue_time": issue_time.isoformat(),
            "value_type": args.value_type,
            "horizon_hours": args.horizon_hours,
            "rows": len(rows),
            "inserted": inserted,
            "model_run_at": model_run_at.isoformat(),
        },
    )
    return 0


async def _dispatch_forecast_run(args: argparse.Namespace) -> int:
    try:
        issue_time = (
            None if args.issue_time is None else _parse_ts(args.issue_time)
        )
    except ValueError as exc:
        print(f"error: invalid --issue-time: {exc}", file=sys.stderr)
        return 2
    summary = await run_forecast_job(
        issue_time=issue_time,
        value_type=args.value_type,
        horizon_hours=args.horizon_hours,
    )
    return summary.exit_code


async def _dispatch_hindcast(args: argparse.Namespace) -> int:
    if args.command == "run":
        return await _dispatch_hindcast_run(args)
    print(f"unknown command: {args.command}", file=sys.stderr)
    return 2


def _build_issue_times(
    *, start: pd.Timestamp, end: pd.Timestamp, cadence: str
) -> list[pd.Timestamp]:
    """Inclusive [start, end] range stepped by ``cadence``.

    ``pd.date_range`` with a frequency alias gives us a deterministic
    list per (start, end, cadence) tuple — same input, same output, no
    drift across reruns. Both bounds are honoured: ``end`` lands in
    the list when it sits exactly on a step.
    """
    if end < start:
        raise ValueError(f"--end {end.isoformat()} is before --start {start.isoformat()}")
    freq = _CADENCE_FREQ[cadence]
    issue_times = pd.date_range(start=start, end=end, freq=freq, tz="UTC")
    return list(issue_times)


async def _dispatch_hindcast_run(args: argparse.Namespace) -> int:
    try:
        start = _parse_ts(args.start)
        end = _parse_ts(args.end)
    except ValueError as exc:
        print(f"error: invalid --start/--end: {exc}", file=sys.stderr)
        return 2
    try:
        issue_times = _build_issue_times(start=start, end=end, cadence=args.cadence)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not issue_times:
        print(
            "error: --start/--end/--cadence produced an empty issue-time list",
            file=sys.stderr,
        )
        return 2

    baseline_fn = _HINDCAST_BASELINES[args.baseline]
    model_run_at = datetime.now(UTC)

    _LOG.info(
        "hindcast.start",
        extra={
            "event": "hindcast.start",
            "baseline": args.baseline,
            "gauge_id": args.gauge_id,
            "value_type": args.value_type,
            "horizon_hours": args.horizon_hours,
            "cadence": args.cadence,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "issue_times": len(issue_times),
            "model_run_at": model_run_at.isoformat(),
        },
    )

    try:
        async with connect() as conn:
            async def _read(it: pd.Timestamp) -> pd.DataFrame:
                lookback_start = it - pd.Timedelta(hours=_HINDCAST_LOOKBACK_HOURS)
                # Half-open [start, end) — push past `it` by one second
                # so an observation stamped exactly at the issue-time
                # falls inside the read window. Mirrors the live path.
                lookback_end = it + pd.Timedelta(seconds=1)
                return await get_observations(
                    conn,
                    gauge_id=args.gauge_id,
                    start=lookback_start,
                    end=lookback_end,
                    value_type=args.value_type,
                )

            async def _write(rows: Any, run_at: datetime) -> int:
                return await insert_forecasts(conn, rows, model_run_at=run_at)

            summary = await run_hindcast(
                baseline_fn,
                gauge_id=args.gauge_id,
                issue_times=issue_times,
                observations_reader=_read,
                writer=_write,
                model_run_at=model_run_at,
                horizon_hours=args.horizon_hours,
                value_type=args.value_type,
            )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    _LOG.info(
        "hindcast.done",
        extra={
            "event": "hindcast.done",
            "baseline": args.baseline,
            "gauge_id": args.gauge_id,
            "model_run_at": model_run_at.isoformat(),
            "issue_times": len(issue_times),
            "succeeded": summary.succeeded,
            "failed": summary.failed,
            "rows_attempted": summary.rows_attempted,
            "rows_inserted": summary.rows_inserted,
        },
    )
    # Exit 0 unless every issue-time errored — single-bad-issue-time
    # in a long backfill should not fail the whole invocation.
    return 1 if summary.outcomes and summary.failed == len(summary.outcomes) else 0


async def _run_query_command(
    conn: Any, args: argparse.Namespace
) -> pd.DataFrame:
    if args.command == "gauges":
        return await get_gauges(conn, gauge_ids=args.gauge_id)
    if args.command == "sections":
        return await get_sections(conn, gauge_ids=args.gauge_id)
    if args.command == "observations":
        return await get_observations(
            conn,
            gauge_id=args.gauge_id,
            start=_parse_ts(args.start),
            end=_parse_ts(args.end),
            value_type=args.value_type,
        )
    if args.command == "weather-observations":
        return await get_weather_observations(
            conn,
            gauge_id=args.gauge_id,
            start=_parse_ts(args.start),
            end=_parse_ts(args.end),
            variables=args.variable,
            source=args.source,
        )
    if args.command == "weather-forecast-latest":
        as_of = _parse_now_or_ts(args.as_of)
        return await get_weather_forecast_latest_as_of(
            conn,
            gauge_id=args.gauge_id,
            as_of=as_of,
            horizon_hours=args.horizon,
            variables=args.variable,
            source=args.source,
        )
    if args.command == "weather-forecast-at-lead":
        return await get_weather_forecast_at_lead(
            conn,
            gauge_id=args.gauge_id,
            target_time=_parse_ts(args.target_time),
            lead_hours=args.lead_hours,
            tolerance_hours=args.tolerance_hours,
            variables=args.variable,
            source=args.source,
        )
    raise ValueError(f"unknown query command: {args.command}")


def _parse_ts(value: str) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _parse_now_or_ts(value: str) -> pd.Timestamp:
    if value.lower() == "now":
        return pd.Timestamp.now(tz="UTC")
    return _parse_ts(value)


def _emit_dataframe(df: pd.DataFrame, *, as_json: bool) -> None:
    if as_json:
        for record in df.to_dict(orient="records"):
            print(json.dumps(record, default=_json_default))
        return
    if df.empty:
        print("(no rows)")
        return
    columns = list(df.columns)
    rendered: list[dict[str, str]] = []
    widths = {c: len(c) for c in columns}
    for _, row in df.iterrows():
        rendered_row: dict[str, str] = {}
        for c in columns:
            val = row[c]
            text = "" if pd.isna(val) else str(val)
            rendered_row[c] = text
            widths[c] = max(widths[c], len(text))
        rendered.append(rendered_row)
    header = "  ".join(c.ljust(widths[c]) for c in columns)
    divider = "  ".join("-" * widths[c] for c in columns)
    print(header)
    print(divider)
    for row in rendered:
        print("  ".join(row[c].ljust(widths[c]) for c in columns))


def _emit_tables(tables: list[dict[str, Any]], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(tables, indent=2, default=_json_default))
        return
    if not tables:
        print("(no tables in public schema)")
        return
    width = max(len(t["table"]) for t in tables)
    print(f"{'TABLE'.ljust(width)}  HYPERTABLE")
    print(f"{'-' * width}  ----------")
    for t in tables:
        print(f"{t['table'].ljust(width)}  {'yes' if t['hypertable'] else 'no'}")


def _emit_describe(info: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(info, indent=2, default=_json_default))
        return
    print(f"Table: {info['table']}")
    cols = info["columns"]
    name_w = max((len(c["name"]) for c in cols), default=4)
    type_w = max((len(c["type"]) for c in cols), default=4)
    print(
        f"  {'COLUMN'.ljust(name_w)}  {'TYPE'.ljust(type_w)}  "
        f"{'NULL'.ljust(5)}  DEFAULT"
    )
    print(f"  {'-' * name_w}  {'-' * type_w}  -----  -------")
    for c in cols:
        null = "yes" if c["nullable"] else "no"
        default = c["default"] if c["default"] is not None else ""
        print(
            f"  {c['name'].ljust(name_w)}  {c['type'].ljust(type_w)}  "
            f"{null.ljust(5)}  {default}"
        )
    if info["primary_key"]:
        print(f"Primary key: ({', '.join(info['primary_key'])})")
    else:
        print("Primary key: (none)")
    if info["indexes"]:
        print("Indexes:")
        for idx in info["indexes"]:
            print(f"  {idx['name']}: {idx['definition']}")
    else:
        print("Indexes: (none)")
    if info["hypertable"]:
        print("Hypertable dimensions:")
        for dim in info["hypertable"]["dimensions"]:
            interval = dim["interval"] if dim["interval"] else "(not time-partitioned)"
            print(f"  {dim['column']} ({dim['type']}): chunk interval {interval}")
    else:
        print("Hypertable: no")


def _emit_count(info: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(info, indent=2, default=_json_default))
        return
    print(f"Table: {info['table']}")
    print(f"  rows:       {info['row_count']:,}")
    if info["time_column"]:
        print(f"  time col:   {info['time_column']}")
        print(f"  time min:   {info['time_min']}")
        print(f"  time max:   {info['time_max']}")
    else:
        print("  time col:   (none)")


def _emit_rows(rows: list[dict[str, Any]], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(rows, indent=2, default=_json_default))
        return
    if not rows:
        print("(no rows)")
        return
    columns = list(rows[0].keys())
    widths = {c: len(c) for c in columns}
    rendered: list[dict[str, str]] = []
    for row in rows:
        rendered_row: dict[str, str] = {}
        for c in columns:
            val = row.get(c)
            text = "" if val is None else str(val)
            rendered_row[c] = text
            widths[c] = max(widths[c], len(text))
        rendered.append(rendered_row)
    header = "  ".join(c.ljust(widths[c]) for c in columns)
    divider = "  ".join("-" * widths[c] for c in columns)
    print(header)
    print(divider)
    for row in rendered:
        print("  ".join(row[c].ljust(widths[c]) for c in columns))


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if obj is pd.NaT or (isinstance(obj, float) and pd.isna(obj)):
        return None
    raise TypeError(f"not JSON serialisable: {type(obj).__name__}")


if __name__ == "__main__":
    raise SystemExit(main())
