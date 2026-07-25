"""Date helpers for GitHub API timestamps.

GitHub returns ISO-8601 timestamps, usually as ``2025-01-31T09:15:00Z`` but
occasionally with an explicit offset or fractional seconds. Parsing here is
deliberately tolerant: a malformed or missing timestamp yields ``None`` rather
than raising, so a single odd payload cannot abort a whole run.
"""

from __future__ import annotations

import calendar
from datetime import UTC, datetime

__all__ = [
    "format_date_for_display",
    "months_before",
    "parse_github_date",
    "to_github_date",
]


def parse_github_date(value: str | None) -> datetime | None:
    """Parse a GitHub timestamp into a timezone-aware UTC datetime.

    Args:
        value: An ISO-8601 timestamp, or None.

    Returns:
        The timestamp as a UTC datetime, or None if it is missing or unparsable.
    """
    if not value:
        return None

    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def to_github_date(value: datetime) -> str:
    """Render a datetime in the format the GitHub API expects for filters."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_date_for_display(value: str | None) -> str:
    """Format a GitHub timestamp for a report column, e.g. ``31/01/25``."""
    parsed = parse_github_date(value)
    return parsed.strftime("%d/%m/%y") if parsed else "-"


def months_before(reference: datetime, months: int) -> datetime:
    """Subtract calendar months from a datetime.

    Unlike ``timedelta(days=30 * months)`` this lands on the same day-of-month,
    clamped to the length of the target month, so "3 months" means what a reader
    expects regardless of which months are involved.

    Args:
        reference: The datetime to count back from.
        months: The number of whole calendar months to subtract.

    Returns:
        The shifted datetime, preserving the time of day and tzinfo.
    """
    if months < 0:
        message = "months must be non-negative"
        raise ValueError(message)

    zero_based_month = reference.month - 1 - months
    year = reference.year + zero_based_month // 12
    month = zero_based_month % 12 + 1
    day = min(reference.day, calendar.monthrange(year, month)[1])
    return reference.replace(year=year, month=month, day=day)
