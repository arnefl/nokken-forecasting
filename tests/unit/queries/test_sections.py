"""Unit tests for ``get_sections`` against a stub connection."""

from __future__ import annotations

import pandas as pd

from nokken_forecasting.queries import get_sections
from tests.unit.queries.conftest import StubConn


def _row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "section_id": 100,
        "section_name": "Sjoa — Åsengjuvet",
        "river_id": 5,
        "local_section_id": 3,
        "gauge_id": 12,
        "gauge_sub": 0,
        "gauge_default": "flow",
        "flowAbsoluteMin": 1.0,
        "flowMin": 5.0,
        "flowMax": 50.0,
        "flowAbsoluteMax": 200.0,
        "levelAbsoluteMin": "0",
        "levelMin": "10",
        "levelMax": "100",
        "levelAbsoluteMax": "300",
    }
    base.update(overrides)
    return base


class TestGetSections:
    async def test_no_filter_emits_unfiltered_select(self) -> None:
        conn = StubConn([_row()])
        df = await get_sections(conn)
        assert "WHERE" not in conn.last_sql
        assert "ORDER BY section_id" in conn.last_sql
        assert df.loc[0, "section_name"] == "Sjoa — Åsengjuvet"
        assert df.dtypes["gauge_id"] == "Int64"

    async def test_gauge_filter_binds_array(self) -> None:
        conn = StubConn([_row()])
        await get_sections(conn, gauge_ids=[12])
        assert "WHERE gauge_id = ANY($1::INTEGER[])" in conn.last_sql
        assert conn.last_args == ([12],)

    async def test_legacy_mixed_case_columns_preserved(self) -> None:
        conn = StubConn([_row()])
        df = await get_sections(conn)
        for col in (
            "flowAbsoluteMin",
            "flowMin",
            "flowMax",
            "flowAbsoluteMax",
            "levelAbsoluteMin",
            "levelMin",
            "levelMax",
            "levelAbsoluteMax",
        ):
            assert col in df.columns

    async def test_nullable_gauge_id(self) -> None:
        conn = StubConn([_row(gauge_id=None)])
        df = await get_sections(conn)
        assert pd.isna(df.loc[0, "gauge_id"])
