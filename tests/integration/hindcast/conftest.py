"""Hindcast-test fixture overrides.

Re-exports the testcontainer + ``seeded_conn_readonly_default``
fixtures from ``tests/integration/queries/conftest.py`` so the
session-scoped Postgres container is shared across subpackages, then
layers an autouse ``forecasts``-truncate so each test starts with an
empty sink (the queries-side ``_truncate_all`` does not clear the
write-side table). Mirrors ``tests/integration/writers/conftest.py``.
"""

from __future__ import annotations

import asyncpg
import pytest

from tests.integration.queries.conftest import (  # noqa: F401
    _postgres_container,
    seeded_conn_readonly_default,
)


@pytest.fixture(autouse=True)
async def _truncate_forecasts(_postgres_container: str) -> None:  # noqa: F811
    conn = await asyncpg.connect(_postgres_container)
    try:
        await conn.execute("TRUNCATE forecasts")
    finally:
        await conn.close()
