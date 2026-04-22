"""Integration-test fixtures.

Mirrors `nokken-data/tests/integration/conftest.py`. Tests skip cleanly
when `POSTGRES_DSN` is not set so CI without a DB still passes.
Locally, point `POSTGRES_DSN` at a Postgres seeded with nokken-web's
migrations.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session")
def postgres_dsn() -> str:
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        pytest.skip("POSTGRES_DSN not set — integration tests need a reachable Postgres")
    return dsn
