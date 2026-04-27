"""Job-test fixture overrides.

Re-exposes ``_postgres_container`` and ``seeded_conn_readonly_default``
from the queries conftest so the testcontainer is shared across both
subpackages, and adds a per-test ``forecasts`` truncate (mirroring the
writers conftest — the queries-side ``_truncate_all`` clears the read
tables only).
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
