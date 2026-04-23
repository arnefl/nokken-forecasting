"""Integration tests for the read-only inspection CLI.

These tests require a reachable Postgres with nokken-web's migrations
applied (003/004/005 included — see SCHEMA_COMPAT.md). They skip when
``POSTGRES_DSN`` is not set (the ``postgres_dsn`` fixture handles
the skip) so CI and sibling devs without a DB stay green.

The readonly enforcement tests are this PR's core safety property:
if the inspection pool could ever issue a successful write, the
inspection CLI is not safe to ship.
"""

from __future__ import annotations

import asyncpg
import pytest

from nokken_forecasting.config import get_settings
from nokken_forecasting.db import inspect as db_inspect
from nokken_forecasting.db.postgres import close_pool, get_pool


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _teardown_pool(postgres_dsn: str):
    # Every test opens a fresh pool through get_pool(); close it
    # afterwards so the module-level _POOL singleton does not leak
    # across tests.
    yield
    await close_pool()


class TestReadonlyEnforcement:
    """The pool must issue sessions with default_transaction_read_only = on."""

    async def test_session_is_read_only(self) -> None:
        pool = await get_pool()
        value = await pool.fetchval("SHOW default_transaction_read_only")
        assert value == "on"

    async def test_write_is_rejected(self) -> None:
        # Any DDL should raise; we catch both the read-only-transaction
        # error (SQLSTATE 25006) and the role-permission error
        # (SQLSTATE 42501). Postgres checks the read-only flag before
        # the role grant, so on nokken_ro the concrete class is
        # ReadOnlySQLTransactionError. Either way: no write can land.
        pool = await get_pool()
        with pytest.raises(
            (
                asyncpg.exceptions.ReadOnlySQLTransactionError,
                asyncpg.exceptions.InsufficientPrivilegeError,
            )
        ):
            await pool.execute(
                "CREATE TABLE __nokken_forecasting_ro_probe__ (id INT)"
            )

    async def test_insert_is_rejected(self) -> None:
        pool = await get_pool()
        with pytest.raises(
            (
                asyncpg.exceptions.ReadOnlySQLTransactionError,
                asyncpg.exceptions.InsufficientPrivilegeError,
            )
        ):
            # observations is a real table; insert into a bogus row.
            await pool.execute(
                "INSERT INTO observations (time, gauge_id, value_type, value) "
                "VALUES (now(), -1, 'flow', 0)"
            )


class TestListTables:
    async def test_includes_core_tables(self) -> None:
        tables = await db_inspect.list_tables()
        names = {t["table"] for t in tables}
        # Phase 1 tables plus the Phase 2 trio.
        for required in {
            "gauges",
            "sections",
            "observations",
            "forecasts",
            "weather_observations",
            "weather_forecasts",
        }:
            assert required in names, f"missing expected table: {required}"

    async def test_flags_hypertables(self) -> None:
        tables = await db_inspect.list_tables()
        by_name = {t["table"]: t for t in tables}
        # ``observations`` is a hypertable in production (and in any
        # Postgres carrying the timescaledb extension). If the test
        # target DB doesn't have the extension, hypertable detection
        # returns False for all tables and this assertion gates the
        # rest of the suite cleanly.
        hyper = {name for name, t in by_name.items() if t["hypertable"]}
        if not hyper:
            pytest.skip("timescaledb extension not installed on target DB")
        assert "observations" in hyper
        assert "forecasts" in hyper
        assert "weather_observations" in hyper
        assert "weather_forecasts" in hyper


class TestDescribeTable:
    async def test_observations_columns(self) -> None:
        info = await db_inspect.describe_table("observations")
        col_names = [c["name"] for c in info["columns"]]
        assert col_names == ["time", "gauge_id", "value_type", "value"]
        types = {c["name"]: c["type"] for c in info["columns"]}
        assert "timestamp" in types["time"]
        assert types["gauge_id"] == "integer"
        assert types["value_type"] == "character varying(6)"
        assert types["value"] == "real"

    async def test_forecasts_columns(self) -> None:
        info = await db_inspect.describe_table("forecasts")
        cols = {c["name"]: c for c in info["columns"]}
        assert set(cols) == {
            "issue_time",
            "valid_time",
            "gauge_id",
            "value_type",
            "quantile",
            "value",
            "model_version",
        }
        # quantile must be nullable (NULL = deterministic).
        assert cols["quantile"]["nullable"] is True
        # value is NOT NULL.
        assert cols["value"]["nullable"] is False

    async def test_unknown_table_raises(self) -> None:
        with pytest.raises(ValueError, match="not found"):
            await db_inspect.describe_table("does_not_exist_xyz")


class TestQuerySafety:
    async def test_non_select_is_rejected_without_hitting_db(self) -> None:
        with pytest.raises(ValueError, match="SELECT"):
            await db_inspect.run_query("DELETE FROM observations")

    async def test_simple_select_works(self) -> None:
        rows = await db_inspect.run_query("SELECT 1 AS one")
        assert rows == [{"one": 1}]
