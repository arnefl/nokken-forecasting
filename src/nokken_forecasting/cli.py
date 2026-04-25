"""CLI dispatcher for ``nokken-forecasting``.

Two sub-apps today:

* ``inspect`` — read-only tour of the shared Postgres schema
  (Phase 2).
* ``query``   — typed-DataFrame readers backing the Phase 3 baselines
  exposed for ad-hoc operator spot-checks (Phase 3b). Output mirrors
  ``inspect``'s aligned-text / ``--json`` shape.

Phase 6 will add ``run`` for the scheduled forecast job; the
top-level dispatcher is kept argparse-simple so adding that doesn't
need a dependency change.

Every query issued through these subcommands rides the pool from
``nokken_forecasting.db.postgres.get_pool``, which enforces
``default_transaction_read_only = on`` at the session level.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from typing import Any

import pandas as pd

from nokken_forecasting.db import inspect as db_inspect
from nokken_forecasting.db.postgres import close_pool
from nokken_forecasting.queries import (
    connect,
    get_gauges,
    get_observations,
    get_sections,
    get_weather_forecast_at_lead,
    get_weather_forecast_latest_as_of,
    get_weather_observations,
)


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


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.group not in {"inspect", "query"}:
        parser.error(f"unknown group: {args.group}")
    return asyncio.run(_run(args))


async def _run(args: argparse.Namespace) -> int:
    # The pool is bound to the active event loop, so opening and
    # closing must happen inside the same asyncio.run() invocation.
    try:
        if args.group == "inspect":
            return await _dispatch_inspect(args)
        return await _dispatch_query(args)
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
