"""Tests for date helpers and dataclasses."""

from __future__ import annotations

from datetime import UTC, datetime

from github_metrics.models import (
    DeveloperMetrics,
    RepositoryMetrics,
    format_date_for_display,
    iso_since,
    parse_github_date,
)


def test_parse_github_date_returns_utc_aware():
    dt = parse_github_date("2025-04-20T12:34:56Z")
    assert dt == datetime(2025, 4, 20, 12, 34, 56, tzinfo=UTC)
    assert dt.tzinfo is not None


def test_parse_github_date_accepts_microseconds():
    """Our own `iso_since()` emits microsecond-precision strings; must parse."""
    dt = parse_github_date("2025-04-20T12:34:56.789012Z")
    assert dt.microsecond == 789012
    assert dt.tzinfo is not None


def test_format_date_for_display_uses_ddmmyy():
    assert format_date_for_display("2025-04-20T00:00:00Z") == "20/04/25"


def test_iso_since_produces_z_suffixed_offset():
    now = datetime(2025, 4, 20, 0, 0, 0, tzinfo=UTC)
    since = iso_since(3, now=now)
    assert since.endswith("Z")
    assert parse_github_date(since) < now


def test_iso_since_zero_months_is_now():
    now = datetime(2025, 4, 20, 12, 0, 0, tzinfo=UTC)
    since = iso_since(0, now=now)
    assert parse_github_date(since) == now


def test_developer_metrics_defaults():
    dev = DeveloperMetrics(name="alice")
    assert dev.commits == 0
    assert dev.repositories == {}


def test_repository_metrics_defaults_for_dora_fields():
    repo = RepositoryMetrics(name="r")
    assert repo.deployment_count == 0
    assert repo.mttr_hours == 0.0
    assert repo.recoveries_count == 0
