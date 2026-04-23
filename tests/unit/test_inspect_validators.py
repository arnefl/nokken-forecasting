"""SQL-validation tests for the inspection CLI. No DB required."""

from __future__ import annotations

import pytest

from nokken_forecasting.db.inspect import (
    assert_identifier,
    assert_select_only,
    assert_where_safe,
)


class TestAssertSelectOnly:
    def test_accepts_plain_select(self) -> None:
        assert_select_only("SELECT 1")

    def test_accepts_lowercase(self) -> None:
        assert_select_only("select 1")

    def test_accepts_leading_whitespace(self) -> None:
        assert_select_only("   \n  SELECT gauge_id FROM gauges")

    def test_accepts_leading_line_comment(self) -> None:
        assert_select_only("-- a note\nSELECT 1")

    def test_accepts_leading_block_comment(self) -> None:
        assert_select_only("/* a note */ SELECT 1")

    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO observations VALUES (1)",
            "UPDATE observations SET value = 0",
            "DELETE FROM observations",
            "DROP TABLE observations",
            "TRUNCATE observations",
            "CREATE TABLE x (id INT)",
            "ALTER TABLE observations ADD COLUMN x INT",
            "-- SELECT is in a comment\nINSERT INTO x VALUES(1)",
        ],
    )
    def test_rejects_non_select(self, sql: str) -> None:
        with pytest.raises(ValueError, match="SELECT"):
            assert_select_only(sql)

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            assert_select_only("")

    def test_rejects_whitespace_only(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            assert_select_only("   \n  /* comments only */  ")


class TestAssertWhereSafe:
    def test_accepts_simple_predicate(self) -> None:
        assert_where_safe("gauge_id = 12")

    def test_accepts_boolean_combination(self) -> None:
        assert_where_safe("gauge_id = 12 AND value_type = 'flow'")

    def test_rejects_semicolon(self) -> None:
        with pytest.raises(ValueError, match="semicolons"):
            assert_where_safe("gauge_id = 12; DROP TABLE x")

    def test_rejects_line_comment(self) -> None:
        with pytest.raises(ValueError, match="comments"):
            assert_where_safe("gauge_id = 12 -- sneaky")

    def test_rejects_block_comment_open(self) -> None:
        with pytest.raises(ValueError, match="comments"):
            assert_where_safe("gauge_id = 12 /* sneaky")

    def test_rejects_block_comment_close(self) -> None:
        with pytest.raises(ValueError, match="comments"):
            assert_where_safe("gauge_id = 12 */ sneaky")


class TestAssertIdentifier:
    def test_accepts_simple_name(self) -> None:
        assert_identifier("observations")

    def test_accepts_underscore_and_digits(self) -> None:
        assert_identifier("weather_forecasts_2024")

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "1_starts_with_digit",
            "has spaces",
            "has;semicolon",
            "observations; DROP TABLE x",
            'has"quote',
            "mixed-dash",
        ],
    )
    def test_rejects_invalid(self, name: str) -> None:
        with pytest.raises(ValueError, match="invalid table identifier"):
            assert_identifier(name)
