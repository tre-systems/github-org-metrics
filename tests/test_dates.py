"""Tests for timestamp parsing and window arithmetic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from github_metrics.dates import (
    format_date_for_display,
    months_before,
    parse_github_date,
    to_github_date,
)


class TestParsing:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("2025-01-31T09:15:00Z", datetime(2025, 1, 31, 9, 15, tzinfo=UTC)),
            (
                "2025-01-31T09:15:00.123Z",
                datetime(2025, 1, 31, 9, 15, 0, 123000, tzinfo=UTC),
            ),
            ("2025-01-31T09:15:00+00:00", datetime(2025, 1, 31, 9, 15, tzinfo=UTC)),
            ("2025-01-31T11:15:00+02:00", datetime(2025, 1, 31, 9, 15, tzinfo=UTC)),
            ("2025-01-31", datetime(2025, 1, 31, tzinfo=UTC)),
        ],
    )
    def test_parses_the_shapes_github_returns(self, value, expected):
        assert parse_github_date(value) == expected

    @pytest.mark.parametrize(
        "value", [None, "", "   ", "not-a-date", "2025-13-01T00:00:00Z"]
    )
    def test_returns_none_rather_than_raising(self, value):
        assert parse_github_date(value) is None

    def test_round_trips_through_the_api_format(self):
        moment = datetime(2025, 6, 1, 8, 30, tzinfo=UTC)

        assert parse_github_date(to_github_date(moment)) == moment

    def test_converts_to_utc_before_formatting(self):
        east = datetime(2025, 6, 1, 10, 0, tzinfo=timezone(timedelta(hours=2)))

        assert to_github_date(east) == "2025-06-01T08:00:00Z"


class TestDisplay:
    def test_formats_as_day_month_year(self):
        assert format_date_for_display("2025-01-31T09:15:00Z") == "31/01/25"

    def test_missing_dates_render_as_a_dash(self):
        assert format_date_for_display(None) == "-"


class TestMonthsBefore:
    def test_subtracts_calendar_months(self):
        assert months_before(datetime(2025, 6, 15, tzinfo=UTC), 3) == datetime(
            2025, 3, 15, tzinfo=UTC
        )

    def test_crosses_a_year_boundary(self):
        assert months_before(datetime(2025, 2, 10, tzinfo=UTC), 4) == datetime(
            2024, 10, 10, tzinfo=UTC
        )

    def test_clamps_to_the_end_of_a_shorter_month(self):
        assert months_before(datetime(2025, 3, 31, tzinfo=UTC), 1) == datetime(
            2025, 2, 28, tzinfo=UTC
        )

    def test_handles_leap_years(self):
        assert months_before(datetime(2024, 3, 31, tzinfo=UTC), 1) == datetime(
            2024, 2, 29, tzinfo=UTC
        )

    def test_preserves_time_of_day_and_timezone(self):
        reference = datetime(2025, 6, 15, 13, 45, tzinfo=UTC)

        assert months_before(reference, 1).timetz() == reference.timetz()

    def test_rejects_negative_months(self):
        with pytest.raises(ValueError, match="non-negative"):
            months_before(datetime(2025, 1, 1, tzinfo=UTC), -1)
