"""Read-only database inspection utilities.

Rides on top of the pool defined in ``postgres.py`` — every query
issued here runs in a session with ``default_transaction_read_only
= on``, so even a connection backed by a role with write privileges
would fail on an attempted write. See ``postgres.py`` for the
rationale.

The functions below back the ``nokken-forecasting inspect`` CLI
subcommands (wired in ``nokken_forecasting.cli``). Each returns a
plain structure (list of dicts, int, etc.) so callers can render it
as text or JSON without re-querying.
"""

from __future__ import annotations

import re
from typing import Any

import asyncpg

from nokken_forecasting.db.postgres import get_pool

# Explicit overrides for ``count`` MIN/MAX: these tables' time-ish
# column names don't collide with an auto-detect because each has a
# single TIMESTAMP column, but being explicit avoids picking the
# wrong column if the schema grows another one. ``statistics`` has no
# timestamp column (day/month ints) so it is deliberately excluded
# from the auto-detect fallback by mapping to None.
_TIME_COLUMNS: dict[str, str | None] = {
    "observations": "time",
    "weather_observations": "time",
    "forecasts": "valid_time",
    "weather_forecasts": "valid_time",
    "statistics": None,
}


def _strip_sql_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", "", sql)
    return sql.strip()


def assert_select_only(sql: str) -> None:
    """Reject any SQL whose first token is not SELECT (after comment strip)."""
    stripped = _strip_sql_comments(sql)
    if not stripped:
        raise ValueError("empty query")
    if not re.match(r"(?i)select\b", stripped):
        head = stripped.split("\n", 1)[0][:80]
        raise ValueError(f"query must begin with SELECT; got: {head!r}")


def assert_where_safe(where: str) -> None:
    """Cheap SQL-injection guard for `sample --where`. Real safety is the RO session."""
    if ";" in where:
        raise ValueError("semicolons are not allowed in --where")
    if "--" in where or "/*" in where or "*/" in where:
        raise ValueError("SQL comments are not allowed in --where")


def assert_identifier(name: str) -> None:
    """Only allow plain identifiers (letters, digits, underscore) in table names."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"invalid table identifier: {name!r}")


async def list_tables() -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT table_name
              FROM information_schema.tables
             WHERE table_schema = 'public'
               AND table_type   = 'BASE TABLE'
             ORDER BY table_name
            """
        )
        table_names = [r["table_name"] for r in rows]
        hypertables = await _hypertable_names(conn)
    return [
        {"table": name, "hypertable": name in hypertables}
        for name in table_names
    ]


async def describe_table(table: str) -> dict[str, Any]:
    assert_identifier(table)
    pool = await get_pool()
    async with pool.acquire() as conn:
        columns = await conn.fetch(
            """
            SELECT column_name,
                   data_type,
                   is_nullable,
                   character_maximum_length,
                   numeric_precision,
                   numeric_scale,
                   column_default
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name   = $1
             ORDER BY ordinal_position
            """,
            table,
        )
        if not columns:
            raise ValueError(f"table not found in public schema: {table}")
        pk = await conn.fetch(
            """
            SELECT kcu.column_name
              FROM information_schema.table_constraints tc
              JOIN information_schema.key_column_usage kcu
                ON kcu.constraint_name = tc.constraint_name
               AND kcu.table_schema    = tc.table_schema
             WHERE tc.table_schema     = 'public'
               AND tc.table_name       = $1
               AND tc.constraint_type  = 'PRIMARY KEY'
             ORDER BY kcu.ordinal_position
            """,
            table,
        )
        indexes = await conn.fetch(
            """
            SELECT indexname, indexdef
              FROM pg_indexes
             WHERE schemaname = 'public'
               AND tablename  = $1
             ORDER BY indexname
            """,
            table,
        )
        hypertable = await _hypertable_info(conn, table)
    return {
        "table": table,
        "columns": [
            {
                "name": c["column_name"],
                "type": _format_type(c),
                "nullable": c["is_nullable"] == "YES",
                "default": c["column_default"],
            }
            for c in columns
        ],
        "primary_key": [r["column_name"] for r in pk],
        "indexes": [
            {"name": r["indexname"], "definition": r["indexdef"]} for r in indexes
        ],
        "hypertable": hypertable,
    }


async def count_table(table: str) -> dict[str, Any]:
    assert_identifier(table)
    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM {table}")
        time_column = _resolve_time_column(table)
        if time_column is None and table not in _TIME_COLUMNS:
            time_column = await conn.fetchval(
                """
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_schema = 'public'
                   AND table_name   = $1
                   AND data_type LIKE 'timestamp%'
                 ORDER BY ordinal_position
                 LIMIT 1
                """,
                table,
            )
        time_min: Any = None
        time_max: Any = None
        if time_column:
            row = await conn.fetchrow(
                f"SELECT MIN({time_column}) AS min_t, MAX({time_column}) AS max_t FROM {table}"
            )
            time_min = row["min_t"]
            time_max = row["max_t"]
    return {
        "table": table,
        "row_count": total,
        "time_column": time_column,
        "time_min": time_min,
        "time_max": time_max,
    }


async def sample_table(
    table: str, limit: int = 10, where: str | None = None
) -> list[dict[str, Any]]:
    assert_identifier(table)
    if limit <= 0:
        raise ValueError("--limit must be positive")
    sql = f"SELECT * FROM {table}"
    if where:
        assert_where_safe(where)
        sql += f" WHERE {where}"
    sql += f" LIMIT {int(limit)}"
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql)
    return [dict(r) for r in rows]


async def run_query(sql: str) -> list[dict[str, Any]]:
    assert_select_only(sql)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql)
    return [dict(r) for r in rows]


def _resolve_time_column(table: str) -> str | None:
    return _TIME_COLUMNS.get(table)


async def _hypertable_names(conn: asyncpg.Connection) -> set[str]:
    if not await _timescaledb_installed(conn):
        return set()
    rows = await conn.fetch(
        """
        SELECT hypertable_name
          FROM timescaledb_information.hypertables
         WHERE hypertable_schema = 'public'
        """
    )
    return {r["hypertable_name"] for r in rows}


async def _hypertable_info(
    conn: asyncpg.Connection, table: str
) -> dict[str, Any] | None:
    if not await _timescaledb_installed(conn):
        return None
    dim_rows = await conn.fetch(
        """
        SELECT column_name, column_type, time_interval
          FROM timescaledb_information.dimensions
         WHERE hypertable_schema = 'public'
           AND hypertable_name   = $1
         ORDER BY dimension_number
        """,
        table,
    )
    if not dim_rows:
        return None
    return {
        "dimensions": [
            {
                "column": r["column_name"],
                "type": r["column_type"],
                "interval": str(r["time_interval"]) if r["time_interval"] else None,
            }
            for r in dim_rows
        ],
    }


async def _timescaledb_installed(conn: asyncpg.Connection) -> bool:
    return bool(
        await conn.fetchval(
            "SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'"
        )
    )


def _format_type(col: dict[str, Any]) -> str:
    dt = col["data_type"]
    if col["character_maximum_length"] is not None:
        return f"{dt}({col['character_maximum_length']})"
    if dt == "numeric" and col["numeric_precision"] is not None:
        scale = col["numeric_scale"] or 0
        return f"{dt}({col['numeric_precision']},{scale})"
    return dt
