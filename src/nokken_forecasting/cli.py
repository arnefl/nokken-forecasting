"""CLI dispatcher for ``nokken-forecasting``.

Phase-2 scope: the ``inspect`` sub-app — a read-only tour of the
shared Postgres schema. Phase 6 will add ``run`` for the scheduled
forecast job; the top-level dispatcher is kept argparse-simple so
adding that doesn't need a dependency change.

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

from nokken_forecasting.db import inspect as db_inspect
from nokken_forecasting.db.postgres import close_pool


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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.group != "inspect":
        parser.error(f"unknown group: {args.group}")
    return asyncio.run(_run(args))


async def _run(args: argparse.Namespace) -> int:
    # The pool is bound to the active event loop, so opening and
    # closing must happen inside the same asyncio.run() invocation.
    try:
        return await _dispatch(args)
    finally:
        await close_pool()


async def _dispatch(args: argparse.Namespace) -> int:
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
    raise TypeError(f"not JSON serialisable: {type(obj).__name__}")


if __name__ == "__main__":
    raise SystemExit(main())
