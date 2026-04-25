"""Mocked-connection helper for query-layer unit tests.

Each reader takes an ``asyncpg.Connection`` and calls ``conn.fetch``
exactly once. ``StubConn`` records the SQL + args of that call and
returns a caller-supplied list of dict-like rows. The tests assert
on both sides of the boundary: the SQL the reader emits, and the
shape / dtypes of the DataFrame it builds from the rows.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


class StubConn:
    def __init__(self, rows: Iterable[dict[str, Any]] | None = None) -> None:
        self._rows: list[dict[str, Any]] = list(rows or [])
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def queue(self, rows: Iterable[dict[str, Any]]) -> None:
        self._rows = list(rows)

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        return list(self._rows)

    @property
    def last_sql(self) -> str:
        return self.calls[-1][0]

    @property
    def last_args(self) -> tuple[Any, ...]:
        return self.calls[-1][1]
