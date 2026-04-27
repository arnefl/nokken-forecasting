"""Writer-test fixture overrides.

Reuses the testcontainer + ``seeded_conn`` fixtures defined in
``tests/integration/queries/conftest.py`` by importing them here:
pytest exposes any fixture function found as an attribute of a
``conftest.py`` module, and importing the function preserves its
identity so the session-scoped ``_postgres_container`` is shared
across both subpackages rather than spun up twice.

The only delta beyond re-exposure is a per-test ``forecasts``
truncate. The queries-side ``_truncate_all`` clears the read-side
tables only; writer tests need ``forecasts`` cleared too, so we
layer the cleanup here rather than mutate the queries-side fixture.
"""

from __future__ import annotations

import asyncpg
import pytest

from tests.integration.queries.conftest import (  # noqa: F401
    _postgres_container,
    seeded_conn,
)


@pytest.fixture(autouse=True)
async def _truncate_forecasts(_postgres_container: str) -> None:  # noqa: F811
    # `_postgres_container` is the imported fixture function above
    # (kept around for pytest discovery); the parameter here is the
    # resolved value pytest injects per request.
    conn = await asyncpg.connect(_postgres_container)
    try:
        await conn.execute("TRUNCATE forecasts")
    finally:
        await conn.close()
