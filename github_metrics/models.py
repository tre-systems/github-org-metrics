"""Dataclasses and date helpers shared across the package."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta


def parse_github_date(date_str: str) -> datetime:
    """Parse an ISO-8601 date string to a UTC-aware `datetime`.

    Accepts GitHub's second-precision format (`...Z`) and the
    microsecond-precision strings produced by our own `iso_since()`
    (which come from `datetime.now(UTC).isoformat()`).
    """
    return datetime.fromisoformat(date_str.replace("Z", "+00:00"))


def format_date_for_display(date_str: str) -> str:
    """Format a GitHub date string as `dd/mm/yy` for human-readable reports."""
    return parse_github_date(date_str).strftime("%d/%m/%y")


def iso_since(months: int, *, now: datetime | None = None) -> str:
    """Return the ISO-8601 `since` cutoff for a window of `months` ago."""
    end = now or datetime.now(UTC)
    start = end - timedelta(days=30 * months)
    return start.isoformat().replace("+00:00", "Z")


@dataclass
class DeveloperMetrics:
    """Aggregated contribution metrics for a single developer in the window."""

    name: str
    commits: int = 0
    lines_added: int = 0
    lines_deleted: int = 0
    prs_opened: int = 0
    prs_reviewed: int = 0
    pr_comments: int = 0
    repositories: dict[str, int] = field(default_factory=dict)


@dataclass
class RepositoryMetrics:
    """Aggregated activity and DORA metrics for a single repository."""

    name: str
    created_at: str = ""
    updated_at: str = ""
    language: str = "N/A"
    branch_count: int = 0
    contributor_count: int = 0
    activity: int = 0
    pr_count: int = 0
    # DORA metrics
    deployment_count: int = 0
    deployment_failures: int = 0
    failure_rate: float = 0.0
    avg_deployment_duration: float = 0.0
    avg_branch_to_merge_time: float = 0.0
    branch_merges_count: int = 0
    mttr_hours: float = 0.0
    recoveries_count: int = 0
