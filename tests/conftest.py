"""Shared fixtures: small, explicit GitHub payloads for the analysis tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from github_metrics.analyze import AnalysisOptions
from github_metrics.models import RawData

SINCE = datetime(2025, 1, 1, tzinfo=UTC)
UNTIL = datetime(2025, 4, 1, tzinfo=UTC)


def iso(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> str:
    """Build a GitHub-style timestamp."""
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:00Z"


def repo(name: str = "api", **overrides: Any) -> dict[str, Any]:
    """Build a repository payload."""
    return {
        "name": name,
        "created_at": iso(2023, 5, 1),
        "updated_at": iso(2025, 3, 1),
        "pushed_at": iso(2025, 3, 20),
        "language": "Python",
        "default_branch": "main",
        **overrides,
    }


def commit(
    sha: str, login: str | None, date: str, *, user_type: str = "User"
) -> dict[str, Any]:
    """Build a commit payload; ``login`` of None means an unlinked author."""
    author = {"login": login, "type": user_type} if login else None
    return {
        "sha": sha,
        "author": author,
        "commit": {"author": {"date": date}, "committer": {"date": date}},
    }


def pull(
    number: int,
    login: str,
    created: str,
    *,
    updated: str | None = None,
    merged: str | None = None,
    user_type: str = "User",
) -> dict[str, Any]:
    """Build a pull request payload."""
    return {
        "number": number,
        "user": {"login": login, "type": user_type},
        "created_at": created,
        "updated_at": updated or created,
        "merged_at": merged,
        "head": {"ref": f"feature-{number}"},
    }


def run(
    name: str,
    created: str,
    conclusion: str,
    *,
    updated: str | None = None,
    branch: str = "main",
) -> dict[str, Any]:
    """Build a workflow run payload."""
    return {
        "id": abs(hash((name, created))) % 100_000,
        "name": name,
        "head_branch": branch,
        "status": "completed",
        "conclusion": conclusion,
        "created_at": created,
        "run_started_at": created,
        "updated_at": updated or created,
    }


def raw_data(**overrides: Any) -> RawData:
    """Build a minimal, internally consistent RawData payload."""
    base: dict[str, Any] = {
        "org": "acme",
        "since": iso(2025, 1, 1, 0),
        "until": iso(2025, 4, 1, 0),
        "fetch_pr_details": True,
        "repos": [],
        "commits": {},
        "commit_stats": {},
        "branch_counts": {},
        "contributor_counts": {},
        "pull_requests": {},
        "pr_reviews": {},
        "pr_comments": {},
        "pr_first_commit_dates": {},
        "workflow_runs": {},
    }
    base.update(overrides)
    return base  # type: ignore[return-value]


def requested_urls(calls: Any) -> list[str]:
    """The URLs a `responses` mock actually received, as plain strings."""
    return [str(call.request.url) for call in calls]


@pytest.fixture
def options() -> AnalysisOptions:
    """Analysis options covering Q1 2025."""
    return AnalysisOptions(since=SINCE, until=UNTIL)
