"""Smoke test for the read-only Postgres connection.

Opens a connection through `get_pool()`, runs `SELECT 1`, and tears
the pool down. Skips when `POSTGRES_DSN` is absent (handled by the
`postgres_dsn` fixture in `conftest.py`).
"""

from __future__ import annotations

import pytest

from nokken_forecasting.config import get_settings
from nokken_forecasting.db.postgres import close_pool, get_pool


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    # Settings are lru_cached at process scope; clear so the fixture
    # picks up POSTGRES_DSN from the current environment.
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_select_one(postgres_dsn: str) -> None:
    pool = await get_pool()
    try:
        result = await pool.fetchval("SELECT 1")
        assert result == 1
    finally:
        await close_pool()
