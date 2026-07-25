"""Data structures shared by collection, analysis, and reporting."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypedDict

__all__ = [
    "DeveloperMetrics",
    "DoraRating",
    "DoraSummary",
    "RawData",
    "Report",
    "RepositoryMetrics",
]


class RawData(TypedDict):
    """The raw GitHub payloads an analysis run needs.

    Every mapping is keyed by repository name. Nested mappings are keyed by
    pull request number *as a string*, so the structure survives a JSON cache
    round-trip unchanged.
    """

    org: str
    since: str
    until: str
    fetch_pr_details: bool
    complete: bool
    """False while a run is still in progress, so it can be resumed."""

    repos: list[dict[str, Any]]
    commits: dict[str, list[dict[str, Any]]]
    commit_stats: dict[str, dict[str, dict[str, int]]]
    branch_counts: dict[str, int]
    contributor_counts: dict[str, int]
    pull_requests: dict[str, list[dict[str, Any]]]
    pr_reviews: dict[str, dict[str, list[dict[str, Any]]]]
    pr_comments: dict[str, dict[str, list[dict[str, Any]]]]
    pr_first_commit_dates: dict[str, dict[str, str]]
    workflow_runs: dict[str, list[dict[str, Any]]]


@dataclass
class DeveloperMetrics:
    """Contribution totals for one developer over the analysis window."""

    name: str
    commits: int = 0
    lines_added: int = 0
    lines_deleted: int = 0
    prs_opened: int = 0
    prs_reviewed: int = 0
    pr_comments: int = 0
    repositories: Counter[str] = field(default_factory=Counter)

    @property
    def touched_code(self) -> bool:
        """Whether this developer added or removed any lines in the window."""
        return self.lines_added > 0 or self.lines_deleted > 0

    def top_repositories(self, limit: int = 5) -> str:
        """Render the busiest repositories for this developer as a summary."""
        ranked = [name for name, _ in self.repositories.most_common()]
        if len(ranked) <= limit:
            return ", ".join(ranked)
        return f"{', '.join(ranked[:limit])} +{len(ranked) - limit} more"


@dataclass
class RepositoryMetrics:
    """Activity and delivery metrics for one repository."""

    name: str
    created_at: str = "-"
    updated_at: str = "-"
    language: str = "N/A"
    branch_count: int = 0
    contributor_count: int = 0
    commit_count: int = 0
    pr_count: int = 0
    # DORA inputs
    deployment_workflow: str | None = None
    deployment_count: int = 0
    deployment_failures: int = 0
    deployment_durations: list[float] = field(default_factory=list)
    recovery_times: list[float] = field(default_factory=list)
    lead_times: list[float] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        """Whether the repository saw any commits or pull requests in the window."""
        return self.commit_count > 0 or self.pr_count > 0

    @property
    def failure_rate(self) -> float:
        """Percentage of deployments that failed."""
        if self.deployment_count == 0:
            return 0.0
        return self.deployment_failures / self.deployment_count * 100

    @property
    def avg_lead_time(self) -> float:
        """Mean hours from a branch's first commit to its merge."""
        return _mean(self.lead_times)

    @property
    def avg_deployment_duration(self) -> float:
        """Mean minutes a successful deployment run takes."""
        return _mean(self.deployment_durations)

    @property
    def avg_recovery_time(self) -> float:
        """Mean hours from a failed deployment to the next successful one."""
        return _mean(self.recovery_times)


@dataclass(frozen=True)
class DoraRating:
    """A DORA performance band with the threshold that produced it."""

    label: str
    detail: str


@dataclass(frozen=True)
class DoraSummary:
    """Organization-wide DORA metrics for the analysis window."""

    lead_time_mean: float
    lead_time_median: float
    lead_time_samples: int
    deploys_total: int
    deploys_per_day: float
    change_failure_rate: float
    recovery_time_mean: float
    recovery_time_samples: int
    deployment_duration_mean: float


@dataclass(frozen=True)
class Report:
    """Everything a run produces, ready to render or export."""

    org: str
    since: datetime
    until: datetime
    developers: list[DeveloperMetrics]
    outliers: list[DeveloperMetrics]
    repositories: list[RepositoryMetrics]
    dora: DoraSummary
    has_pr_details: bool

    @property
    def window_days(self) -> float:
        """Length of the analysis window in days."""
        return max((self.until - self.since).total_seconds() / 86400, 1.0)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
